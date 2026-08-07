# app/kiro_gateway_tray/network_watch.py
"""Detect network changes from OS-native routing events and notify on change.

Why native events instead of polling: after a Wi-Fi/VPN/Ethernet switch the old
QUIC/TLS connections are half-dead, and every second spent noticing that is a
second of broken tunnel. The kernel already knows the moment a route or address
changes, so we subscribe to that instead of waiting for a poll tick:

* macOS/BSD: ``socket.AF_ROUTE`` + ``SOCK_RAW`` (readable by a normal user, no
  root needed) multiplexed with a self-pipe through ``select``.
* Linux: ``socket.AF_NETLINK`` + ``NETLINK_ROUTE``, bound to
  ``RTMGRP_LINK | RTMGRP_IPV4_IFADDR | RTMGRP_IPV4_ROUTE``.
* Windows: ``iphlpapi.NotifyRouteChange2`` and ``NotifyIpInterfaceChange``
  ctypes callbacks signalling a Win32 event.

Routing messages are treated as an untyped *hint*: we deliberately do not parse
``rt_msghdr`` / ``nlmsghdr`` to filter on ``RTM_*`` types. The authoritative
filter is :func:`outbound_fingerprint` — the callback only fires when the
address the kernel would actually use for outbound traffic changed *and* the
machine currently has a route (see :meth:`NetworkWatcher._maybe_fire`). That
keeps this module free of per-platform struct layouts while still being quiet:
a storm of unrelated routing chatter costs one cheap local socket call.

Every native event source degrades to plain polling on failure (unsupported
platform, sandbox, missing symbol), mirroring the ``notify -> poll`` structure
of :mod:`kiro_gateway_tray.theme_watcher`. All ctypes / ``AF_ROUTE`` /
``AF_NETLINK`` usage is resolved INSIDE methods (never at import time) so this
module imports cleanly on all three platforms.
"""
from __future__ import annotations

import os
import socket
import sys
import threading
from typing import Callable

from .log import logger

# Fingerprint probe target. Any routable address works; nothing is ever sent to
# it, we only ask the kernel which local address it would use to get there.
_FINGERPRINT_HOST = "1.1.1.1"
_FINGERPRINT_PORT = 443
_FINGERPRINT_TIMEOUT = 1.0

# Linux netlink constants (not exposed by the socket module).
_NETLINK_ROUTE = 0
_RTMGRP_LINK = 0x0001
_RTMGRP_IPV4_IFADDR = 0x0010
_RTMGRP_IPV4_ROUTE = 0x0040
_NETLINK_GROUPS = _RTMGRP_LINK | _RTMGRP_IPV4_IFADDR | _RTMGRP_IPV4_ROUTE

# Win32 / iphlpapi constants.
_AF_UNSPEC = 0
_NO_ERROR = 0
_WAIT_OBJECT_0 = 0

_READ_CHUNK = 8192


def outbound_fingerprint() -> str:
    """Return the local IP the kernel would use for outbound traffic.

    Uses an unconnected UDP socket and ``connect()``, which only performs a
    route lookup: no packet leaves the machine, so this is safe to call on every
    routing event and works offline.

    Returns:
        The local IPv4/IPv6 address as a string, or ``""`` when no route exists
        (offline, or the probe was rejected by the OS).
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(_FINGERPRINT_TIMEOUT)
            sock.connect((_FINGERPRINT_HOST, _FINGERPRINT_PORT))
            return str(sock.getsockname()[0])
    except (OSError, IndexError):
        logger.debug("NetworkWatcher: outbound fingerprint probe failed", exc_info=True)
        return ""


class NetworkWatcher:
    """Notify a callback when the machine's outbound network path changes.

    Args:
        on_change: Called with no arguments from the watcher's daemon thread
            whenever the outbound fingerprint changes to a usable address —
            including a return from "no route at all" to the same address as
            before. Must marshal any UI work itself. Exceptions raised by it
            are logged and swallowed so the watcher survives a bad callback.
        debounce: Seconds to coalesce a routing-event storm before re-reading
            the fingerprint. A single switch produces many kernel messages
            (link down, address removed, address added, default route added);
            one callback per switch is what callers want.
        poll_interval: Fallback poll cadence in seconds. Also used as the
            blocking wait timeout on the native path, so a missed event still
            gets noticed.
    """

    def __init__(
        self,
        on_change: Callable[[], None],
        debounce: float = 0.8,
        poll_interval: float = 5.0,
    ) -> None:
        self._on_change = on_change
        self._debounce = debounce
        self._poll_interval = poll_interval
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._last_fingerprint = ""
        # Self-pipe write end (POSIX) / Win32 manual-reset event (Windows) used
        # by stop() to break the thread out of its blocking wait.
        self._wake_w: int | None = None
        self._win_stop_handle: object | None = None
        # ctypes callback must stay referenced for as long as iphlpapi holds it.
        self._win_callback: object | None = None

    def start(self) -> None:
        """Start watching in a daemon thread.

        Never raises: an unsupported platform or a failing native event source
        degrades to polling inside the thread, and a second ``start()`` is a
        no-op.
        """
        if self._thread is not None:
            return
        self._stop_event.clear()
        self._last_fingerprint = outbound_fingerprint()
        self._thread = threading.Thread(
            target=self._run, name="network-watcher", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        """Signal shutdown, unblock the wait, and join the thread briefly."""
        self._stop_event.set()
        self._wake()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=2.0)
            self._thread = None

    # --- internals -------------------------------------------------------

    def _wake(self) -> None:
        """Unblock the watcher thread's blocking wait on any platform."""
        fd = self._wake_w
        if fd is not None:
            try:
                os.write(fd, b"\x01")
            except OSError:
                logger.debug("NetworkWatcher: wake pipe write failed", exc_info=True)
        handle = self._win_stop_handle
        if handle is not None:
            try:
                import ctypes

                ctypes.windll.kernel32.SetEvent(handle)
            except (AttributeError, OSError):
                logger.debug("NetworkWatcher: SetEvent on stop failed", exc_info=True)

    def _maybe_fire(self) -> None:
        """Re-read the fingerprint and invoke the callback if it changed.

        The empty fingerprint (no route at all) is a *recorded* state, not an
        ignored one, but it never fires the callback: mid switch the machine is
        briefly offline and reconnecting a tunnel with no route is pointless.
        Recording it is what makes the offline -> online edge observable even
        when the address is unchanged — unplug/replug, sleep/wake, and a router
        reboot that hands back the same DHCP lease all end on an identical
        fingerprint, so skipping the empty state entirely would collapse them
        into "nothing happened" and leave the tunnel wedged.

        Transitions: ``address -> empty`` records only, ``empty -> address``
        notifies (even for the same address as before), ``address A ->
        address B`` notifies.
        """
        fingerprint = outbound_fingerprint()
        if fingerprint == self._last_fingerprint:
            return
        previous = self._last_fingerprint
        self._last_fingerprint = fingerprint
        if not fingerprint:
            logger.info(
                "NetworkWatcher: outbound route lost (was {}); "
                "deferring notification until a route returns",
                previous,
            )
            return
        logger.info(
            "NetworkWatcher: outbound address changed {} -> {}",
            previous or "(none)",
            fingerprint,
        )
        try:
            self._on_change()
        except Exception as exc:  # noqa: BLE001 — callback is caller code; a
            # broken callback must not kill the watcher thread.
            logger.warning("NetworkWatcher: on_change callback failed: {}", exc)

    def _debounce_and_check(self, drain: Callable[[], None] | None = None) -> None:
        """Wait out the debounce window, discard the burst, then check once.

        Args:
            drain: Optional platform hook that consumes/acknowledges events
                which piled up during the window, so the event loop does not
                wake immediately for changes already accounted for.
        """
        if self._stop_event.wait(self._debounce):
            return
        if drain is not None:
            drain()
        self._maybe_fire()

    def _run(self) -> None:
        """Thread body: prefer native events, fall back to polling."""
        try:
            self._run_with_events()
            return
        except Exception as exc:  # noqa: BLE001 — native event sources fail in
            # platform-specific ways (OSError, ctypes AttributeError, missing
            # socket constants); any of them must only cost us the fast path.
            logger.debug(
                "NetworkWatcher: native event loop unavailable ({}), "
                "falling back to polling",
                exc,
                exc_info=True,
            )
        self._run_polling()

    def _run_polling(self) -> None:
        """Pure polling loop (graceful degradation), honoring the stop event."""
        while not self._stop_event.wait(self._poll_interval):
            self._maybe_fire()

    def _run_with_events(self) -> None:
        """Dispatch to the platform's native routing-event loop.

        Raises:
            OSError: If the platform has no supported native event source, or
                the source could not be set up.
        """
        platform = sys.platform
        if platform == "win32":
            self._run_windows_notify()
        elif platform.startswith("linux"):
            self._run_netlink()
        elif platform == "darwin" or "bsd" in platform:
            self._run_route_socket()
        else:
            raise OSError(f"no native network event source for platform {platform!r}")

    # --- macOS / BSD ------------------------------------------------------

    def _run_route_socket(self) -> None:
        """Watch the BSD routing socket (``AF_ROUTE``) for any routing message.

        Raises:
            OSError: If ``AF_ROUTE`` is unavailable or the socket cannot open.
        """
        af_route = getattr(socket, "AF_ROUTE", None)
        if af_route is None:
            raise OSError("socket.AF_ROUTE is unavailable on this platform")
        sock = socket.socket(af_route, socket.SOCK_RAW)
        self._serve_socket(sock)

    # --- Linux ------------------------------------------------------------

    def _run_netlink(self) -> None:
        """Watch ``NETLINK_ROUTE`` for link, address and route changes.

        Raises:
            OSError: If ``AF_NETLINK`` is unavailable or bind fails.
        """
        af_netlink = getattr(socket, "AF_NETLINK", None)
        if af_netlink is None:
            raise OSError("socket.AF_NETLINK is unavailable on this platform")
        sock = socket.socket(af_netlink, socket.SOCK_RAW, _NETLINK_ROUTE)
        try:
            # pid 0 lets the kernel assign one; groups is the multicast mask.
            sock.bind((0, _NETLINK_GROUPS))
        except OSError:
            sock.close()
            raise
        self._serve_socket(sock)

    # --- POSIX shared -----------------------------------------------------

    def _serve_socket(self, sock: socket.socket) -> None:
        """Own ``sock`` plus a self-pipe and run the select loop until stopped.

        Args:
            sock: An open event socket. Closed before returning, whatever
                happens.
        """
        wake_r, wake_w = os.pipe()
        self._wake_w = wake_w
        try:
            sock.setblocking(False)
            if self._stop_event.is_set():
                return
            self._run_select_loop(sock, wake_r)
        finally:
            self._wake_w = None
            sock.close()
            for fd in (wake_r, wake_w):
                try:
                    os.close(fd)
                except OSError:
                    logger.debug(
                        "NetworkWatcher: closing wake pipe failed", exc_info=True
                    )

    def _run_select_loop(self, sock: socket.socket, wake_fd: int) -> None:
        """Block on [event socket, wake pipe] with the poll interval as timeout.

        Args:
            sock: Non-blocking routing/netlink socket.
            wake_fd: Read end of the self-pipe written by :meth:`stop`.
        """
        import select

        while not self._stop_event.is_set():
            ready, _, _ = select.select([sock, wake_fd], [], [], self._poll_interval)
            if self._stop_event.is_set() or wake_fd in ready:
                return
            if sock in ready:
                self._drain_socket(sock)
                self._debounce_and_check(lambda: self._drain_socket(sock))
            else:
                # select timed out: the polling safety net for missed events.
                self._maybe_fire()

    @staticmethod
    def _drain_socket(sock: socket.socket) -> None:
        """Read and discard every pending message on a non-blocking socket."""
        while True:
            try:
                if not sock.recv(_READ_CHUNK):
                    return
            except BlockingIOError:
                return
            except OSError:
                logger.debug("NetworkWatcher: draining events failed", exc_info=True)
                return

    # --- Windows ----------------------------------------------------------

    def _run_windows_notify(self) -> None:
        """Watch route and IP-interface changes via iphlpapi callbacks.

        ``NotifyRouteChange2`` / ``NotifyIpInterfaceChange`` invoke our callback
        on an OS-owned thread; it only signals a Win32 event so the real work
        stays on the watcher thread. ``WaitForMultipleObjects`` waits on that
        event plus our stop event, with a timeout providing the poll fallback.

        Raises:
            OSError: If any Win32/iphlpapi call fails.
        """
        import ctypes
        from ctypes import wintypes

        iphlpapi = ctypes.windll.iphlpapi
        kernel32 = ctypes.windll.kernel32

        # void (*)(PVOID CallerContext, PVOID Row, MIB_NOTIFICATION_TYPE Type)
        callback_t = ctypes.WINFUNCTYPE(
            None, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int
        )
        notify_argtypes = [
            ctypes.c_ushort,  # ADDRESS_FAMILY
            callback_t,
            ctypes.c_void_p,
            wintypes.BOOL,  # InitialNotification
            ctypes.POINTER(wintypes.HANDLE),
        ]
        iphlpapi.NotifyRouteChange2.argtypes = notify_argtypes
        iphlpapi.NotifyRouteChange2.restype = wintypes.DWORD
        iphlpapi.NotifyIpInterfaceChange.argtypes = notify_argtypes
        iphlpapi.NotifyIpInterfaceChange.restype = wintypes.DWORD
        iphlpapi.CancelMibChangeNotify2.argtypes = [wintypes.HANDLE]
        iphlpapi.CancelMibChangeNotify2.restype = wintypes.DWORD

        kernel32.CreateEventW.argtypes = [
            wintypes.LPVOID,
            wintypes.BOOL,
            wintypes.BOOL,
            wintypes.LPCWSTR,
        ]
        kernel32.CreateEventW.restype = wintypes.HANDLE
        kernel32.WaitForMultipleObjects.argtypes = [
            wintypes.DWORD,
            ctypes.POINTER(wintypes.HANDLE),
            wintypes.BOOL,
            wintypes.DWORD,
        ]
        kernel32.WaitForMultipleObjects.restype = wintypes.DWORD
        kernel32.SetEvent.argtypes = [wintypes.HANDLE]
        kernel32.SetEvent.restype = wintypes.BOOL
        kernel32.ResetEvent.argtypes = [wintypes.HANDLE]
        kernel32.ResetEvent.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL

        change_event = kernel32.CreateEventW(None, True, False, None)
        stop_handle = kernel32.CreateEventW(None, True, False, None)
        if not change_event or not stop_handle:
            raise OSError("CreateEventW failed")

        def _on_net_change(caller_context: int, row: int, notification_type: int) -> None:
            kernel32.SetEvent(change_event)

        callback = callback_t(_on_net_change)
        self._win_callback = callback
        route_handle = wintypes.HANDLE()
        iface_handle = wintypes.HANDLE()
        try:
            self._win_stop_handle = stop_handle
            if self._stop_event.is_set():
                return

            rc = iphlpapi.NotifyRouteChange2(
                _AF_UNSPEC, callback, None, False, ctypes.byref(route_handle)
            )
            if rc != _NO_ERROR:
                raise OSError(f"NotifyRouteChange2 failed: {rc}")
            rc = iphlpapi.NotifyIpInterfaceChange(
                _AF_UNSPEC, callback, None, False, ctypes.byref(iface_handle)
            )
            if rc != _NO_ERROR:
                raise OSError(f"NotifyIpInterfaceChange failed: {rc}")

            timeout_ms = max(1, int(self._poll_interval * 1000))
            handles = (wintypes.HANDLE * 2)(change_event, stop_handle)
            while not self._stop_event.is_set():
                wait = kernel32.WaitForMultipleObjects(2, handles, False, timeout_ms)
                if self._stop_event.is_set() or wait == _WAIT_OBJECT_0 + 1:
                    return
                if wait == _WAIT_OBJECT_0:
                    kernel32.ResetEvent(change_event)
                    self._debounce_and_check(
                        lambda: kernel32.ResetEvent(change_event)
                    )
                else:
                    # Timeout: the polling safety net for missed callbacks.
                    self._maybe_fire()
        finally:
            self._win_stop_handle = None
            for handle in (route_handle, iface_handle):
                if handle:
                    try:
                        iphlpapi.CancelMibChangeNotify2(handle)
                    except OSError:
                        logger.debug(
                            "NetworkWatcher: CancelMibChangeNotify2 failed",
                            exc_info=True,
                        )
            self._win_callback = None
            for handle in (change_event, stop_handle):
                try:
                    kernel32.CloseHandle(handle)
                except OSError:
                    logger.debug("NetworkWatcher: CloseHandle failed", exc_info=True)


__all__ = ["NetworkWatcher", "outbound_fingerprint"]
