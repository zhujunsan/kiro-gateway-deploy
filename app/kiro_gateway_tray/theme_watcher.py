# app/kiro_gateway_tray/theme_watcher.py
"""Watch the Windows taskbar light/dark theme and notify on change.

Windows stores the taskbar theme under::

    HKEY_CURRENT_USER\\Software\\Microsoft\\Windows\\CurrentVersion\\Themes\\Personalize
        SystemUsesLightTheme  (DWORD: 0 = dark taskbar, 1 = light taskbar)

``ThemeWatcher`` reacts immediately to a registry change via
``RegNotifyChangeKeyValue`` (asynchronous mode, signalling a Win32 event), with
a 5-second poll as a fallback. The whole feature is Windows-only: on every other
platform ``start()`` is a no-op, so callers never need a platform branch.

All ctypes/winreg usage is guarded INSIDE methods (never at import time) so this
module imports cleanly on macOS and Linux.
"""
from __future__ import annotations

import sys
import threading
from typing import Callable

from .icon import windows_uses_light_theme
from .log import logger

# Win32 constants.
_HKEY_CURRENT_USER = 0x80000001
_KEY_NOTIFY = 0x0010
_REG_NOTIFY_CHANGE_LAST_SET = 0x00000004
_WAIT_OBJECT_0 = 0
_INFINITE = 0xFFFFFFFF
_PERSONALIZE_SUBKEY = (
    r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
)


class ThemeWatcher:
    """Notify a callback when the Windows taskbar light/dark theme changes.

    Parameters
    ----------
    on_change:
        Called with the new ``light_theme`` bool whenever the value changes.
        Invoked from the watcher's daemon thread, so the callback must marshal
        any UI work appropriately.
    poll_interval:
        Fallback poll cadence in seconds. The registry-change notification
        usually fires first; the timeout is a safety net.
    """

    def __init__(
        self, on_change: Callable[[bool], None], poll_interval: float = 5.0
    ) -> None:
        self._on_change = on_change
        self._poll_interval = poll_interval
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        # Win32 manual-reset event handle used to unblock WaitForMultipleObjects
        # on stop(). Created inside the thread; closed on shutdown.
        self._win_stop_handle = None
        self._last_value: bool | None = None

    def start(self) -> None:
        """Start watching. No-op on non-Windows platforms."""
        if sys.platform != "win32":
            return
        if self._thread is not None:
            return
        self._last_value = windows_uses_light_theme()
        self._thread = threading.Thread(
            target=self._run, name="theme-watcher", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        """Signal shutdown, unblock the wait, and join the thread briefly."""
        self._stop_event.set()
        # Unblock WaitForMultipleObjects so the thread sees the stop event.
        handle = self._win_stop_handle
        if handle is not None:
            try:
                import ctypes

                ctypes.windll.kernel32.SetEvent(handle)
            except Exception:
                logger.debug("ThemeWatcher: SetEvent on stop failed", exc_info=True)
        thread = self._thread
        if thread is not None:
            thread.join(timeout=2.0)
            self._thread = None

    # --- internals -------------------------------------------------------

    def _maybe_fire(self) -> None:
        """Re-read the theme and fire the callback if it changed."""
        try:
            new_value = windows_uses_light_theme()
        except Exception:
            logger.debug("ThemeWatcher: read theme failed", exc_info=True)
            return
        if new_value != self._last_value:
            self._last_value = new_value
            try:
                self._on_change(new_value)
            except Exception:
                logger.debug("ThemeWatcher: on_change callback failed", exc_info=True)

    def _run(self) -> None:
        """Thread body: prefer the registry-notify path, fall back to polling."""
        try:
            self._run_with_notify()
        except Exception:
            logger.debug(
                "ThemeWatcher: notify loop failed, falling back to polling",
                exc_info=True,
            )
            self._run_polling()

    def _run_polling(self) -> None:
        """Pure 5s polling loop (graceful degradation), honoring the stop event."""
        while not self._stop_event.wait(self._poll_interval):
            self._maybe_fire()

    def _run_with_notify(self) -> None:
        """Block on [registry-change event, stop event] with a poll timeout.

        Uses ``RegNotifyChangeKeyValue`` in asynchronous mode: it signals the
        manual-reset ``reg_event`` once when the key changes, then must be
        re-armed. ``WaitForMultipleObjects`` waits on both that event and our
        own stop event, with a timeout providing the poll fallback.
        """
        import ctypes
        from ctypes import wintypes

        advapi32 = ctypes.windll.advapi32
        kernel32 = ctypes.windll.kernel32

        # Signatures (defensive: keeps ctypes from truncating 64-bit handles).
        advapi32.RegOpenKeyExW.argtypes = [
            wintypes.HKEY,
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.REGSAM,
            ctypes.POINTER(wintypes.HKEY),
        ]
        advapi32.RegOpenKeyExW.restype = wintypes.LONG
        advapi32.RegNotifyChangeKeyValue.argtypes = [
            wintypes.HKEY,
            wintypes.BOOL,
            wintypes.DWORD,
            wintypes.HANDLE,
            wintypes.BOOL,
        ]
        advapi32.RegNotifyChangeKeyValue.restype = wintypes.LONG
        advapi32.RegCloseKey.argtypes = [wintypes.HKEY]
        advapi32.RegCloseKey.restype = wintypes.LONG

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

        hkey = wintypes.HKEY()
        reg_event = None
        stop_event = None
        try:
            rc = advapi32.RegOpenKeyExW(
                wintypes.HKEY(_HKEY_CURRENT_USER),
                _PERSONALIZE_SUBKEY,
                0,
                _KEY_NOTIFY,
                ctypes.byref(hkey),
            )
            if rc != 0:
                raise OSError(f"RegOpenKeyExW failed: {rc}")

            # Manual-reset events (auto-reset=False) so a signal stays latched
            # until we explicitly handle/re-arm it.
            reg_event = kernel32.CreateEventW(None, True, False, None)
            stop_event = kernel32.CreateEventW(None, True, False, None)
            if not reg_event or not stop_event:
                raise OSError("CreateEventW failed")
            self._win_stop_handle = stop_event

            # If stop() was already called before the handle existed, bail out.
            if self._stop_event.is_set():
                return

            timeout_ms = max(1, int(self._poll_interval * 1000))
            handles = (wintypes.HANDLE * 2)(reg_event, stop_event)

            armed = False
            while not self._stop_event.is_set():
                if not armed:
                    rc = advapi32.RegNotifyChangeKeyValue(
                        hkey,
                        False,  # bWatchSubtree
                        _REG_NOTIFY_CHANGE_LAST_SET,
                        reg_event,
                        True,  # fAsynchronous
                    )
                    if rc != 0:
                        raise OSError(f"RegNotifyChangeKeyValue failed: {rc}")
                    armed = True

                wait = kernel32.WaitForMultipleObjects(
                    2, handles, False, timeout_ms
                )
                if self._stop_event.is_set():
                    break
                if wait == _WAIT_OBJECT_0 + 1:
                    # stop event signaled
                    break
                if wait == _WAIT_OBJECT_0:
                    # registry changed: reset the (manual-reset) event and
                    # re-arm the one-shot notification on the next loop.
                    kernel32.ResetEvent(reg_event)
                    armed = False
                # else: timeout (poll fallback) — armed stays True, just re-read.
                self._maybe_fire()
        finally:
            if reg_event:
                try:
                    kernel32.CloseHandle(reg_event)
                except Exception:
                    pass
            if stop_event:
                self._win_stop_handle = None
                try:
                    kernel32.CloseHandle(stop_event)
                except Exception:
                    pass
            if hkey:
                try:
                    advapi32.RegCloseKey(hkey)
                except Exception:
                    pass
