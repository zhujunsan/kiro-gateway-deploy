# app/tests/test_network_watch.py
"""Tests for network_watch: fingerprint probe, debounce, degradation, shutdown.

Everything here is offline by construction. ``outbound_fingerprint`` is either
monkeypatched away or exercised against a fake socket class, the routing socket
is a fake with a message queue, and ``select.select`` is replaced by a scripted
stub, so no test opens a real AF_ROUTE/AF_NETLINK socket and no test blocks on a
real kernel event.
"""
import os
import socket
import sys
import threading

import pytest

from kiro_gateway_tray import network_watch as nw
from kiro_gateway_tray.network_watch import NetworkWatcher, outbound_fingerprint


# --- helpers ----------------------------------------------------------------


class _FakeUdpSocket:
    """Stand-in for the UDP socket used by the fingerprint probe."""

    def __init__(self, local=("10.0.0.5", 54321), connect_error=None, getsockname=None):
        self._local = local
        self._connect_error = connect_error
        self._getsockname = getsockname
        self.connected_to = None
        self.timeout = None
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.closed = True
        return False

    def settimeout(self, value):
        self.timeout = value

    def connect(self, address):
        if self._connect_error is not None:
            raise self._connect_error
        self.connected_to = address

    def getsockname(self):
        if self._getsockname is not None:
            return self._getsockname()
        return self._local


class _FakeEventSocket:
    """Fake routing/netlink socket backed by an explicit message queue."""

    def __init__(self, messages=None, on_recv=None):
        self.messages = list(messages or [])
        self._on_recv = on_recv
        self.blocking = True
        self.closed = False
        self.recv_calls = 0

    @property
    def readable(self):
        return bool(self.messages)

    def setblocking(self, flag):
        self.blocking = flag

    def recv(self, _size):
        self.recv_calls += 1
        if self._on_recv is not None:
            self._on_recv(self)
        if not self.messages:
            raise BlockingIOError("would block")
        return self.messages.pop(0)

    def close(self):
        self.closed = True


def _script_select(monkeypatch, steps):
    """Replace ``select.select`` with a scripted sequence of ready-lists.

    Args:
        monkeypatch: pytest monkeypatch fixture.
        steps: Callables taking ``(rlist, timeout)`` and returning the ready
            list. Exhausting the script returns an empty ready list forever
            (i.e. a timeout), which keeps a buggy loop from hanging the suite.
    """
    calls = []

    def _fake_select(rlist, wlist, xlist, timeout=None):
        calls.append((list(rlist), timeout))
        index = len(calls) - 1
        if index < len(steps):
            return steps[index](rlist, timeout), [], []
        return [], [], []

    monkeypatch.setattr("select.select", _fake_select)
    return calls


def _fingerprints(monkeypatch, values):
    """Make ``outbound_fingerprint`` yield ``values``, repeating the last one."""
    remaining = list(values)
    last = {"value": remaining[-1] if remaining else ""}

    def _fake():
        if remaining:
            last["value"] = remaining.pop(0)
        return last["value"]

    monkeypatch.setattr(nw, "outbound_fingerprint", _fake)


# --- outbound_fingerprint ---------------------------------------------------


def test_fingerprint_returns_local_address_without_sending(monkeypatch):
    created = []

    def _factory(family, kind):
        assert family == socket.AF_INET
        assert kind == socket.SOCK_DGRAM
        sock = _FakeUdpSocket()
        created.append(sock)
        return sock

    monkeypatch.setattr(nw.socket, "socket", _factory)
    assert outbound_fingerprint() == "10.0.0.5"
    # connect() on a UDP socket is a route lookup only; nothing is ever sent.
    assert created[0].connected_to == ("1.1.1.1", 443)
    assert created[0].timeout == pytest.approx(1.0)
    assert created[0].closed is True


@pytest.mark.parametrize(
    "error",
    [
        OSError("network is unreachable"),
        socket.gaierror("name resolution failed"),
        TimeoutError("timed out"),
    ],
)
def test_fingerprint_returns_empty_on_socket_failure(monkeypatch, error):
    monkeypatch.setattr(
        nw.socket, "socket", lambda *a, **k: _FakeUdpSocket(connect_error=error)
    )
    assert outbound_fingerprint() == ""


def test_fingerprint_returns_empty_when_getsockname_is_malformed(monkeypatch):
    monkeypatch.setattr(
        nw.socket,
        "socket",
        lambda *a, **k: _FakeUdpSocket(getsockname=lambda: ()),
    )
    assert outbound_fingerprint() == ""


def test_fingerprint_socket_construction_failure_is_contained(monkeypatch):
    def _boom(*_a, **_k):
        raise OSError("no file descriptors left")

    monkeypatch.setattr(nw.socket, "socket", _boom)
    assert outbound_fingerprint() == ""


# --- change detection -------------------------------------------------------


def test_fires_only_when_fingerprint_changes(monkeypatch):
    fired = []
    _fingerprints(monkeypatch, ["10.0.0.5", "10.0.0.5", "192.168.1.9"])
    watcher = NetworkWatcher(lambda: fired.append(1))
    watcher._last_fingerprint = "10.0.0.5"

    watcher._maybe_fire()
    assert fired == []
    watcher._maybe_fire()
    assert fired == []
    watcher._maybe_fire()
    assert fired == [1]
    assert watcher._last_fingerprint == "192.168.1.9"


def test_going_offline_records_state_without_firing(monkeypatch):
    """Mid-switch the box is briefly offline: reconnecting then is pointless."""
    fired = []
    _fingerprints(monkeypatch, ["", "", "172.16.0.2"])
    watcher = NetworkWatcher(lambda: fired.append(1))
    watcher._last_fingerprint = "10.0.0.5"

    watcher._maybe_fire()
    watcher._maybe_fire()
    assert fired == []
    # "Offline" is recorded, not ignored: that is what makes the return
    # observable even when the address comes back identical.
    assert watcher._last_fingerprint == ""
    watcher._maybe_fire()
    assert fired == [1]


def test_returning_online_with_the_same_address_fires(monkeypatch):
    """Unplug/replug, sleep/wake, same DHCP lease: the tunnel still needs a kick."""
    fired = []
    _fingerprints(monkeypatch, ["", "10.0.0.5"])
    watcher = NetworkWatcher(lambda: fired.append(1))
    watcher._last_fingerprint = "10.0.0.5"

    watcher._maybe_fire()
    assert fired == []
    watcher._maybe_fire()
    assert fired == [1]
    assert watcher._last_fingerprint == "10.0.0.5"


def test_staying_offline_fires_at_most_once_per_outage(monkeypatch):
    """A long outage must not produce a callback per poll tick."""
    fired = []
    _fingerprints(monkeypatch, ["", "", "", "10.0.0.5", "10.0.0.5"])
    watcher = NetworkWatcher(lambda: fired.append(1))
    watcher._last_fingerprint = "10.0.0.5"

    for _ in range(5):
        watcher._maybe_fire()

    # Three offline polls: silent. Then one recovery callback, and the repeat
    # of the same address afterwards is not a change.
    assert fired == [1]


def test_offline_at_startup_then_online_fires(monkeypatch):
    """A watcher seeded while offline must notify once a route appears."""
    fired = []
    _fingerprints(monkeypatch, ["192.168.0.2"])
    watcher = NetworkWatcher(lambda: fired.append(1))
    # start() seeds the baseline; offline at that moment means "".
    watcher._last_fingerprint = ""

    watcher._maybe_fire()
    assert fired == [1]
    assert watcher._last_fingerprint == "192.168.0.2"


def test_offline_start_stays_quiet_while_still_offline(monkeypatch):
    fired = []
    _fingerprints(monkeypatch, ["", ""])
    watcher = NetworkWatcher(lambda: fired.append(1))
    watcher._last_fingerprint = ""

    watcher._maybe_fire()
    watcher._maybe_fire()
    assert fired == []


def test_callback_exception_does_not_break_the_watcher(monkeypatch):
    calls = []

    def _bad_callback():
        calls.append(1)
        raise RuntimeError("callback blew up")

    _fingerprints(monkeypatch, ["10.0.0.5", "10.0.0.6"])
    watcher = NetworkWatcher(_bad_callback)
    watcher._last_fingerprint = ""

    watcher._maybe_fire()
    watcher._maybe_fire()
    # Both attempts ran: a raising callback must not poison later notifications.
    assert calls == [1, 1]


# --- debounce ---------------------------------------------------------------


def test_debounce_window_coalesces_an_event_storm(monkeypatch):
    """One switch emits many kernel messages; the caller must see one callback."""
    fired = []
    checks = []
    _fingerprints(monkeypatch, ["192.168.1.9"])

    burst = {"refilled": False}

    def _on_recv(sock):
        # Simulate more routing messages landing while we debounce.
        if not burst["refilled"] and not sock.messages:
            burst["refilled"] = True
            sock.messages.extend([b"link-down", b"addr-del", b"route-add"])

    sock = _FakeEventSocket([b"addr-add", b"route-del"], on_recv=_on_recv)
    watcher = NetworkWatcher(lambda: fired.append(1), debounce=0.01, poll_interval=0.01)
    watcher._last_fingerprint = "10.0.0.5"

    real_maybe_fire = watcher._maybe_fire

    def _counting_maybe_fire():
        checks.append(1)
        real_maybe_fire()

    monkeypatch.setattr(watcher, "_maybe_fire", _counting_maybe_fire)

    wake_r, wake_w = os.pipe()
    try:
        _script_select(
            monkeypatch,
            [
                lambda rlist, _t: [sock],
                lambda rlist, _t: [sock] if sock.readable else [wake_r],
                lambda rlist, _t: [wake_r],
            ],
        )
        watcher._run_select_loop(sock, wake_r)
    finally:
        os.close(wake_r)
        os.close(wake_w)

    # Five routing messages, one fingerprint check, one callback.
    assert checks == [1]
    assert fired == [1]
    assert sock.messages == []


def test_debounce_aborts_when_stopped_mid_window(monkeypatch):
    fired = []
    _fingerprints(monkeypatch, ["203.0.113.7"])
    watcher = NetworkWatcher(lambda: fired.append(1), debounce=5.0)
    watcher._last_fingerprint = "10.0.0.5"
    watcher._stop_event.set()

    drained = []
    watcher._debounce_and_check(lambda: drained.append(1))

    # No callback, no drain, and no 5-second wait: stop() wins the window.
    assert fired == []
    assert drained == []


# --- select loop ------------------------------------------------------------

def test_select_timeout_polls_as_a_safety_net(monkeypatch):
    """A missed kernel event must still be caught by the timeout branch."""
    fired = []
    _fingerprints(monkeypatch, ["10.0.0.5", "10.0.0.5", "198.51.100.4"])
    sock = _FakeEventSocket()
    watcher = NetworkWatcher(lambda: fired.append(1), poll_interval=0.01)
    watcher._last_fingerprint = "10.0.0.5"

    wake_r, wake_w = os.pipe()
    try:
        calls = _script_select(
            monkeypatch,
            [
                lambda rlist, _t: [],
                lambda rlist, _t: [],
                lambda rlist, _t: [],
                lambda rlist, _t: [wake_r],
            ],
        )
        watcher._run_select_loop(sock, wake_r)
    finally:
        os.close(wake_r)
        os.close(wake_w)

    assert fired == [1]
    assert calls[0][1] == pytest.approx(0.01)
    # The timeout branch must not touch the event socket.
    assert sock.recv_calls == 0


def test_select_loop_exits_on_wake_pipe_without_firing(monkeypatch):
    fired = []
    _fingerprints(monkeypatch, ["203.0.113.7"])
    sock = _FakeEventSocket([b"route-add"])
    watcher = NetworkWatcher(lambda: fired.append(1))
    watcher._last_fingerprint = "10.0.0.5"

    wake_r, wake_w = os.pipe()
    try:
        _script_select(monkeypatch, [lambda rlist, _t: [wake_r]])
        watcher._run_select_loop(sock, wake_r)
    finally:
        os.close(wake_r)
        os.close(wake_w)

    assert fired == []


def test_select_loop_does_not_start_when_already_stopped(monkeypatch):
    fired = []
    _fingerprints(monkeypatch, ["203.0.113.7"])
    sock = _FakeEventSocket([b"route-add"])
    watcher = NetworkWatcher(lambda: fired.append(1))
    watcher._last_fingerprint = "10.0.0.5"
    watcher._stop_event.set()

    wake_r, wake_w = os.pipe()
    try:
        calls = _script_select(monkeypatch, [lambda rlist, _t: [sock]])
        watcher._run_select_loop(sock, wake_r)
    finally:
        os.close(wake_r)
        os.close(wake_w)

    assert fired == []
    assert calls == []


def test_drain_socket_tolerates_eof_and_errors():
    empty = _FakeEventSocket([b""])
    NetworkWatcher._drain_socket(empty)
    assert empty.recv_calls == 1

    class _BrokenSocket(_FakeEventSocket):
        def recv(self, _size):
            self.recv_calls += 1
            raise OSError("socket torn down")

    broken = _BrokenSocket()
    NetworkWatcher._drain_socket(broken)
    assert broken.recv_calls == 1


# --- native setup + degradation --------------------------------------------


@pytest.mark.parametrize(
    "platform,method",
    [
        ("win32", "_run_windows_notify"),
        ("linux", "_run_netlink"),
        ("darwin", "_run_route_socket"),
        ("freebsd14", "_run_route_socket"),
    ],
)
def test_platform_dispatch(monkeypatch, platform, method):
    called = []
    monkeypatch.setattr(nw.sys, "platform", platform)
    watcher = NetworkWatcher(lambda: None)
    monkeypatch.setattr(watcher, method, lambda: called.append(method))

    watcher._run_with_events()
    assert called == [method]


def test_unknown_platform_has_no_native_source(monkeypatch):
    monkeypatch.setattr(nw.sys, "platform", "sunos5")
    watcher = NetworkWatcher(lambda: None)
    with pytest.raises(OSError, match="no native network event source"):
        watcher._run_with_events()


def test_route_socket_requires_af_route(monkeypatch):
    monkeypatch.delattr(nw.socket, "AF_ROUTE", raising=False)
    watcher = NetworkWatcher(lambda: None)
    with pytest.raises(OSError, match="AF_ROUTE"):
        watcher._run_route_socket()


def test_netlink_requires_af_netlink(monkeypatch):
    monkeypatch.delattr(nw.socket, "AF_NETLINK", raising=False)
    watcher = NetworkWatcher(lambda: None)
    with pytest.raises(OSError, match="AF_NETLINK"):
        watcher._run_netlink()


def test_netlink_binds_the_documented_multicast_groups(monkeypatch):
    bound = []
    served = []

    class _NetlinkSocket(_FakeEventSocket):
        def bind(self, address):
            bound.append(address)

    monkeypatch.setattr(nw.socket, "AF_NETLINK", 16, raising=False)
    monkeypatch.setattr(nw.socket, "socket", lambda *a: _NetlinkSocket())
    watcher = NetworkWatcher(lambda: None)
    monkeypatch.setattr(watcher, "_serve_socket", lambda sock: served.append(sock))

    watcher._run_netlink()
    # RTMGRP_LINK | RTMGRP_IPV4_IFADDR | RTMGRP_IPV4_ROUTE == 0x51
    assert bound == [(0, 0x0001 | 0x0010 | 0x0040)]
    assert len(served) == 1


def test_netlink_bind_failure_closes_the_socket(monkeypatch):
    created = []

    class _UnbindableSocket(_FakeEventSocket):
        def bind(self, address):
            raise OSError("operation not permitted")

    def _factory(*_a):
        sock = _UnbindableSocket()
        created.append(sock)
        return sock

    monkeypatch.setattr(nw.socket, "AF_NETLINK", 16, raising=False)
    monkeypatch.setattr(nw.socket, "socket", _factory)
    watcher = NetworkWatcher(lambda: None)

    with pytest.raises(OSError, match="not permitted"):
        watcher._run_netlink()
    assert created[0].closed is True


def test_serve_socket_releases_fds_when_the_loop_explodes(monkeypatch):
    sock = _FakeEventSocket()
    watcher = NetworkWatcher(lambda: None)
    seen_fds = []

    def _boom(_sock, wake_fd):
        seen_fds.append(wake_fd)
        assert watcher._wake_w is not None
        raise OSError("select exploded")

    monkeypatch.setattr(watcher, "_run_select_loop", _boom)
    with pytest.raises(OSError, match="select exploded"):
        watcher._serve_socket(sock)

    assert sock.closed is True
    assert sock.blocking is False
    assert watcher._wake_w is None
    # Both pipe ends are closed, so re-closing them must fail.
    with pytest.raises(OSError):
        os.close(seen_fds[0])


def test_serve_socket_skips_the_loop_when_already_stopped(monkeypatch):
    sock = _FakeEventSocket()
    watcher = NetworkWatcher(lambda: None)
    watcher._stop_event.set()
    monkeypatch.setattr(
        watcher, "_run_select_loop", lambda *a: pytest.fail("loop must not run")
    )

    watcher._serve_socket(sock)
    assert sock.closed is True
    assert watcher._wake_w is None


def test_native_failure_degrades_to_polling(monkeypatch):
    polled = []
    watcher = NetworkWatcher(lambda: None, poll_interval=0.01)

    def _explode():
        raise OSError("AF_ROUTE denied by sandbox")

    def _fake_fire():
        polled.append(1)
        if len(polled) >= 2:
            watcher._stop_event.set()

    monkeypatch.setattr(watcher, "_run_with_events", _explode)
    monkeypatch.setattr(watcher, "_maybe_fire", _fake_fire)

    watcher._run()
    assert len(polled) >= 2


def test_native_failure_from_ctypes_also_degrades(monkeypatch):
    """iphlpapi/ctypes failures are AttributeError, not OSError."""
    polled = []
    watcher = NetworkWatcher(lambda: None, poll_interval=0.01)

    def _explode():
        raise AttributeError("windll has no attribute iphlpapi")

    def _fake_fire():
        polled.append(1)
        watcher._stop_event.set()

    monkeypatch.setattr(watcher, "_run_with_events", _explode)
    monkeypatch.setattr(watcher, "_maybe_fire", _fake_fire)

    watcher._run()
    assert polled == [1]


def test_run_does_not_poll_when_the_native_loop_returns_cleanly(monkeypatch):
    watcher = NetworkWatcher(lambda: None, poll_interval=0.01)
    monkeypatch.setattr(watcher, "_run_with_events", lambda: None)
    monkeypatch.setattr(
        watcher, "_run_polling", lambda: pytest.fail("must not fall back")
    )

    watcher._run()


def test_polling_loop_stops_on_stop_event(monkeypatch):
    watcher = NetworkWatcher(lambda: None, poll_interval=5.0)
    watcher._stop_event.set()
    monkeypatch.setattr(
        watcher, "_maybe_fire", lambda: pytest.fail("must not fire after stop")
    )

    watcher._run_polling()


# --- start/stop lifecycle ---------------------------------------------------


def test_start_seeds_the_baseline_and_stop_joins(monkeypatch):
    _fingerprints(monkeypatch, ["10.0.0.5"])
    entered = threading.Event()
    watcher = NetworkWatcher(lambda: pytest.fail("no change expected"))

    def _fake_run():
        entered.set()
        watcher._stop_event.wait(5.0)

    monkeypatch.setattr(watcher, "_run", _fake_run)

    watcher.start()
    assert entered.wait(2.0)
    thread = watcher._thread
    assert thread is not None and thread.daemon is True
    # A pre-seeded baseline is what keeps start() from firing spuriously.
    assert watcher._last_fingerprint == "10.0.0.5"

    watcher.stop()
    assert watcher._thread is None
    assert not thread.is_alive()


def test_start_is_idempotent(monkeypatch):
    _fingerprints(monkeypatch, ["10.0.0.5"])
    watcher = NetworkWatcher(lambda: None)
    started = []
    monkeypatch.setattr(watcher, "_run", lambda: started.append(1))

    watcher.start()
    watcher.start()
    watcher.stop()
    assert started == [1]


def test_stop_without_start_is_safe():
    watcher = NetworkWatcher(lambda: None)
    watcher.stop()
    assert watcher._thread is None


def test_wake_writes_to_the_self_pipe():
    watcher = NetworkWatcher(lambda: None)
    wake_r, wake_w = os.pipe()
    try:
        watcher._wake_w = wake_w
        watcher._wake()
        assert os.read(wake_r, 1) == b"\x01"
    finally:
        os.close(wake_r)
        os.close(wake_w)


def test_wake_survives_a_closed_pipe():
    watcher = NetworkWatcher(lambda: None)
    wake_r, wake_w = os.pipe()
    os.close(wake_r)
    os.close(wake_w)
    watcher._wake_w = wake_w

    watcher._wake()


@pytest.mark.skipif(sys.platform == "win32", reason="no windll to fake off-Windows")
def test_wake_survives_a_missing_windll():
    watcher = NetworkWatcher(lambda: None)
    watcher._win_stop_handle = object()

    watcher._wake()
