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
