# app/tests/test_tray_usage_refresh.py
"""The usage menu line must stay live without the user opening the menu.

On macOS the tray NSMenu is static, so label callables are not re-evaluated on
open; a background loop is the only thing that keeps the displayed quota fresh.
These tests pin that behavior so the regression (frozen quota for hours) can't
silently come back.
"""
import sys
import types
import threading
import time

import pytest


@pytest.fixture(autouse=True)
def _stub_pystray(monkeypatch):
    if "pystray" not in sys.modules:
        monkeypatch.setitem(sys.modules, "pystray", types.ModuleType("pystray"))


def _make_app():
    from kiro_gateway_tray.tray import TrayApp
    return TrayApp()


def test_refresh_loop_ticks_cache_while_running(monkeypatch):
    app = _make_app()
    monkeypatch.setattr(app.sup, "status", lambda: {"gateway": "running"})

    refreshed = threading.Event()
    # Tick immediately instead of waiting the real 60s.
    monkeypatch.setattr(app._usage_refresh_stop, "wait", lambda _t: False)

    # Stop the loop the moment it has refreshed once, so the test doesn't spin.
    def _refresh_once():
        refreshed.set()
        app._usage_refresh_stop.set()

    monkeypatch.setattr(app._usage_cache, "refresh", _refresh_once)
    app._start_usage_refresh_loop()
    assert refreshed.wait(timeout=2.0), "background loop never refreshed usage"


def test_refresh_loop_skips_when_not_running(monkeypatch):
    app = _make_app()
    monkeypatch.setattr(app.sup, "status", lambda: {"gateway": "stopped"})

    called = threading.Event()
    monkeypatch.setattr(app._usage_cache, "refresh", lambda: called.set())

    # One tick, then stop.
    ticks = {"n": 0}

    def _wait(_t):
        ticks["n"] += 1
        if ticks["n"] > 1:
            return True  # stop
        return False  # one iteration

    monkeypatch.setattr(app._usage_refresh_stop, "wait", _wait)
    app._start_usage_refresh_loop()
    # Give the daemon thread a moment; it must NOT refresh while stopped.
    assert not called.wait(timeout=0.5)


# --- signed-out Kiro: stop polling and prompt for re-login -------------------
#
# Sentry KIRO-GATEWAY-TRAY-D: a signed-out account was polled every 60s forever,
# and the gateway reported each failure as an "upstream outage".

def _signed_out_state(code: str = "account_auth_required"):
    from kiro_gateway_tray.login_state import LoginState
    return LoginState(login_required=True, code=code, message="expired")


def test_refresh_loop_stops_polling_usage_while_signed_out(monkeypatch):
    from kiro_gateway_tray import usage

    app = _make_app()
    monkeypatch.setattr(app.sup, "status", lambda: {"gateway": "running"})
    monkeypatch.setattr(usage, "fetch_health", lambda **_k: _signed_out_state())

    refreshed = threading.Event()
    monkeypatch.setattr(app._usage_cache, "refresh", lambda: refreshed.set())

    ticks = {"n": 0}

    def _wait(_t):
        ticks["n"] += 1
        return ticks["n"] > 1

    monkeypatch.setattr(app._usage_refresh_stop, "wait", _wait)
    app._start_usage_refresh_loop()

    assert not refreshed.wait(timeout=0.5), "polled /usage while signed out"
    assert app._login_gate.login_required is True


def test_refresh_loop_resumes_usage_after_signing_back_in(monkeypatch):
    from kiro_gateway_tray import usage
    from kiro_gateway_tray.login_state import LoginState

    app = _make_app()
    monkeypatch.setattr(app.sup, "status", lambda: {"gateway": "running"})
    app._login_gate.note_login_required(_signed_out_state())
    monkeypatch.setattr(usage, "fetch_health", lambda **_k: LoginState())

    refreshed = threading.Event()

    def _refresh_once():
        refreshed.set()
        app._usage_refresh_stop.set()

    monkeypatch.setattr(app._usage_cache, "refresh", _refresh_once)
    monkeypatch.setattr(app._usage_refresh_stop, "wait", lambda _t: False)
    app._start_usage_refresh_loop()

    assert refreshed.wait(timeout=2.0), "did not resume polling after sign-in"
    assert app._login_gate.login_required is False


def test_usage_line_shows_login_hint_and_does_not_refresh(monkeypatch):
    app = _make_app()
    monkeypatch.setattr(app.sup, "status", lambda: {"gateway": "running"})
    app._login_gate.note_login_required(_signed_out_state())

    called = threading.Event()
    monkeypatch.setattr(app._usage_cache, "refresh", lambda *_a: called.set())

    line = app._usage_line(None)

    assert "登录" in line
    assert not called.is_set(), "menu line triggered a poll while signed out"


def test_usage_row_is_clickable_only_when_signed_out(monkeypatch):
    app = _make_app()
    assert app._usage_row_enabled(None) is False

    app._login_gate.note_login_required(_signed_out_state())
    assert app._usage_row_enabled(None) is True


def test_clicking_usage_row_explains_and_forces_recheck(monkeypatch):
    from kiro_gateway_tray import dialogs

    app = _make_app()
    app._login_gate.note_login_required(_signed_out_state())

    alerts: list[tuple[str, str]] = []
    monkeypatch.setattr(dialogs, "alert", lambda t, b: alerts.append((t, b)))
    rechecked = threading.Event()
    monkeypatch.setattr(app._usage_cache, "recheck", rechecked.set)
    monkeypatch.setattr(app, "_request_redraw", lambda: None)

    app._on_usage_row(None, None)

    assert alerts and "Kiro" in alerts[0][0]
    assert "打开 Kiro" in alerts[0][1]
    assert rechecked.is_set()


def test_clicking_usage_row_is_noop_when_signed_in(monkeypatch):
    from kiro_gateway_tray import dialogs

    app = _make_app()
    alerts: list[tuple[str, str]] = []
    monkeypatch.setattr(dialogs, "alert", lambda t, b: alerts.append((t, b)))

    app._on_usage_row(None, None)

    assert alerts == []


def test_failed_dialog_still_triggers_recheck(monkeypatch):
    """A dialog failure must not swallow the user's re-check intent."""
    from kiro_gateway_tray import dialogs

    app = _make_app()
    app._login_gate.note_login_required(_signed_out_state())
    monkeypatch.setattr(
        dialogs, "alert", lambda *_a: (_ for _ in ()).throw(RuntimeError("no ui"))
    )
    rechecked = threading.Event()
    monkeypatch.setattr(app._usage_cache, "recheck", rechecked.set)
    monkeypatch.setattr(app, "_request_redraw", lambda: None)

    app._on_usage_row(None, None)

    assert rechecked.is_set()


def test_login_transition_notifies_once_each_way(monkeypatch):
    from kiro_gateway_tray import usage
    from kiro_gateway_tray.login_state import LoginState

    app = _make_app()
    monkeypatch.setattr(app.sup, "status", lambda: {"gateway": "running"})
    monkeypatch.setattr(app, "_request_redraw", lambda: None)
    notes: list[tuple[str, str]] = []
    monkeypatch.setattr(app, "_notify", lambda t, b: notes.append((t, b)))

    state = {"value": _signed_out_state()}
    monkeypatch.setattr(usage, "fetch_health", lambda **_k: state["value"])

    app._poll_login_state()
    app._poll_login_state()  # still signed out: must not notify again
    assert len(notes) == 1
    assert "Kiro" in notes[0][0]

    state["value"] = LoginState()
    app._poll_login_state()
    app._poll_login_state()  # still signed in: must not notify again
    assert len(notes) == 2
    assert "恢复" in notes[1][1]


def test_login_probe_skipped_when_gateway_not_running(monkeypatch):
    from kiro_gateway_tray import usage

    app = _make_app()
    monkeypatch.setattr(app.sup, "status", lambda: {"gateway": "stopped"})
    probed = threading.Event()
    monkeypatch.setattr(usage, "fetch_health", lambda **_k: probed.set())

    app._poll_login_state()

    assert not probed.is_set()


def test_login_probe_failure_leaves_state_untouched(monkeypatch):
    from kiro_gateway_tray import usage

    app = _make_app()
    monkeypatch.setattr(app.sup, "status", lambda: {"gateway": "running"})
    monkeypatch.setattr(
        usage, "fetch_health", lambda **_k: (_ for _ in ()).throw(RuntimeError("boom"))
    )

    app._poll_login_state()  # must not raise

    assert app._login_gate.login_required is False


# --- _UsageCache: the fetch itself must latch and then stop ------------------

def _usage_cache(monkeypatch, fetch_impl):
    """Build a _UsageCache whose usage.fetch() is replaced, refreshed inline."""
    from kiro_gateway_tray import tray, usage

    monkeypatch.setattr(usage, "fetch", fetch_impl)
    cache = tray._UsageCache()

    done = threading.Event()
    monkeypatch.setattr(cache._cache, "_on_update", done.set, raising=False)
    return cache, done


def test_cache_latches_login_required_and_stops_fetching(monkeypatch):
    from kiro_gateway_tray import usage

    calls = {"n": 0}

    def _fetch(*_a, **_k):
        calls["n"] += 1
        raise usage.LoginRequiredError(_signed_out_state())

    cache, _done = _usage_cache(monkeypatch, _fetch)

    cache.refresh()
    for _ in range(50):
        if cache.gate.login_required:
            break
        time.sleep(0.01)
    assert cache.gate.login_required is True

    before = calls["n"]
    for _ in range(5):
        cache.refresh(None)
    time.sleep(0.1)
    # Subsequent refreshes are gated: the signed-out window suppresses them.
    assert calls["n"] == before
    assert "登录" in cache.display()


def test_cache_recheck_bypasses_the_gate(monkeypatch):
    from kiro_gateway_tray import usage

    calls = {"n": 0}

    def _fetch(*_a, **_k):
        calls["n"] += 1
        raise usage.LoginRequiredError(_signed_out_state())

    cache, _done = _usage_cache(monkeypatch, _fetch)
    cache.gate.note_login_required(_signed_out_state())

    cache.recheck()
    for _ in range(50):
        if calls["n"] > 0:
            break
        time.sleep(0.01)
    assert calls["n"] == 1, "manual re-check must always issue a request"


def test_cache_clears_gate_after_a_successful_fetch(monkeypatch):
    cache, done = _usage_cache(
        monkeypatch, lambda *_a, **_k: {"breakdowns": [{"used": 5, "limit": 10}]}
    )
    cache.gate.note_login_required(_signed_out_state())

    cache.recheck()
    assert done.wait(timeout=2.0)
    assert cache.gate.login_required is False
    assert cache.display() == "5 / 10"


def test_cache_keeps_generic_failure_behaviour(monkeypatch):
    """Non-auth failures must still show 获取失败 and not latch the gate."""
    cache, _done = _usage_cache(
        monkeypatch, lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("503 boom"))
    )

    cache.refresh()
    time.sleep(0.2)
    assert cache.gate.login_required is False
