# app/tests/test_tray_main_thread_ui.py
"""AppKit menu-bar updates must be marshaled onto the main thread.

On macOS 27+, ``NSStatusItem.setMenu:`` asserts the main-queue barrier. Calling
it from a pystray/worker thread hard-crashes with SIGTRAP and no Python
traceback (seen 2026-07-14 on 26A5378n). These tests pin that every UI refresh
path goes through ``macos_menu.run_on_main_thread``.
"""
from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock

import pytest


@pytest.fixture(autouse=True)
def _stub_pystray(monkeypatch):
    if "pystray" not in sys.modules:
        monkeypatch.setitem(sys.modules, "pystray", types.ModuleType("pystray"))


def _make_app(monkeypatch):
    from kiro_gateway_tray import macos_menu, tray as tray_mod

    app = tray_mod.TrayApp()
    icon = MagicMock()
    app._icon = icon

    marshaled = []

    def _capture(fn):
        marshaled.append(fn)
        fn()

    monkeypatch.setattr(macos_menu, "run_on_main_thread", _capture)
    monkeypatch.setattr(tray_mod, "make_icon", lambda _running: "fake-icon")
    monkeypatch.setattr(app.sup, "status", lambda: {"gateway": "running"})
    return app, icon, marshaled


def test_request_redraw_marshals_update_menu(monkeypatch):
    app, icon, marshaled = _make_app(monkeypatch)
    monkeypatch.setattr(
        "kiro_gateway_tray.macos_menu.is_status_menu_open", lambda _ic: False
    )
    app._request_redraw()
    assert len(marshaled) == 1
    icon.update_menu.assert_called_once_with()


def test_request_redraw_skips_update_menu_while_menu_open(monkeypatch):
    app, icon, marshaled = _make_app(monkeypatch)
    monkeypatch.setattr(
        "kiro_gateway_tray.macos_menu.is_status_menu_open", lambda _ic: True
    )
    app._request_redraw()
    assert len(marshaled) == 1
    icon.update_menu.assert_not_called()
    assert app._redraw_deferred is True


def test_refresh_icon_marshals_set_icon(monkeypatch):
    app, icon, marshaled = _make_app(monkeypatch)
    app._refresh_icon()
    assert len(marshaled) == 1
    assert icon.icon == "fake-icon"


def test_macos_reopen_event_calls_existing_instance_handler(monkeypatch):
    from kiro_gateway_tray import macos_menu

    class _NSObject:
        @classmethod
        def alloc(cls):
            return cls()

        def init(self):
            return self

    class _FakeApplication:
        def __init__(self):
            self.delegate = None

        def setDelegate_(self, delegate):
            self.delegate = delegate

    fake_appkit = types.ModuleType("AppKit")
    fake_appkit.NSObject = _NSObject
    fake_appkit.NSApplication = types.SimpleNamespace(
        sharedApplication=lambda: _FakeApplication()
    )
    monkeypatch.setitem(sys.modules, "AppKit", fake_appkit)
    monkeypatch.setitem(sys.modules, "objc", types.ModuleType("objc"))
    monkeypatch.setattr(macos_menu.sys, "platform", "darwin")

    app = _FakeApplication()
    icon = types.SimpleNamespace(_app=app)
    calls = []
    macos_menu.install_reopen_handler(icon, lambda: calls.append("reopen"))

    assert app.delegate is icon._kg_reopen_delegate
    handled = app.delegate.applicationShouldHandleReopen_hasVisibleWindows_(
        app, False
    )
    assert handled is False
    assert calls == ["reopen"]


def test_start_or_restart_worker_does_not_call_update_menu_directly(monkeypatch):
    """Regression: daemon thread used to call icon.update_menu() → SIGTRAP on macOS 27."""
    import threading

    from kiro_gateway_tray import tray as tray_mod

    app, icon, marshaled = _make_app(monkeypatch)
    done = threading.Event()

    monkeypatch.setattr(app.sup, "status", lambda: {"gateway": "stopped"})
    monkeypatch.setattr(app.sup, "start", lambda: None)
    monkeypatch.setattr(app, "_notify", lambda *_a, **_k: None)
    monkeypatch.setattr(tray_mod.appconfig, "load", lambda: MagicMock(
        cloudflare=MagicMock(hostname="x.example"),
    ))
    monkeypatch.setattr(tray_mod, "_tunnel_url", lambda _cfg: "https://x.example")

    orig_redraw = app._request_redraw

    def _redraw_and_signal():
        orig_redraw()
        done.set()

    monkeypatch.setattr(app, "_request_redraw", _redraw_and_signal)

    app._on_start_or_restart(icon, None)
    assert done.wait(timeout=2.0)

    assert icon.update_menu.call_count == 1  # only via _request_redraw → marshal
    assert len(marshaled) >= 2  # _refresh_icon + _request_redraw
    assert icon.icon == "fake-icon"


def test_source_has_no_bare_update_menu_outside_request_redraw():
    """Static guard: worker paths must not call ``*.update_menu()`` directly."""
    from pathlib import Path

    src = Path(__file__).resolve().parents[1] / "kiro_gateway_tray" / "tray.py"
    text = src.read_text(encoding="utf-8")
    bare = []
    for line in text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("#"):
            continue
        if ".update_menu()" in line:
            bare.append(line)
    assert bare == ["                ic.update_menu()"], bare


def test_run_after_menu_tracking_waits_for_default_run_loop(monkeypatch):
    """A deferred rebuild must not fire inside NSEventTrackingRunLoopMode."""
    from kiro_gateway_tray import macos_menu

    scheduled = {}

    class _Timer:
        @staticmethod
        def timerWithTimeInterval_repeats_block_(interval, repeats, block):
            scheduled["interval"] = interval
            scheduled["repeats"] = repeats
            scheduled["block"] = block
            return "timer"

    class _RunLoop:
        def addTimer_forMode_(self, timer, mode):
            scheduled["timer"] = timer
            scheduled["mode"] = mode

    foundation = types.ModuleType("Foundation")
    foundation.NSTimer = _Timer
    foundation.NSRunLoop = types.SimpleNamespace(
        mainRunLoop=lambda: _RunLoop()
    )
    foundation.NSDefaultRunLoopMode = "default-mode"
    monkeypatch.setitem(sys.modules, "Foundation", foundation)
    monkeypatch.setattr(macos_menu.sys, "platform", "darwin")
    monkeypatch.setattr(macos_menu, "run_on_main_thread", lambda fn: fn())

    calls = []
    macos_menu.run_after_menu_tracking(lambda: calls.append("rebuilt"))

    assert calls == []
    assert scheduled["interval"] == 0.0
    assert scheduled["repeats"] is False
    assert scheduled["mode"] == "default-mode"
    scheduled["block"](scheduled["timer"])
    assert calls == ["rebuilt"]


def test_darwin_backend_rejects_update_menu_during_tracking(monkeypatch):
    """Defense in depth blocks setMenu: even when a caller bypasses TrayApp."""
    from kiro_gateway_tray import macos_menu

    class _NSObject:
        pass

    class _Menu:
        def __init__(self):
            self.delegate = None

        def setDelegate_(self, delegate):
            self.delegate = delegate

    class _Button:
        def __init__(self):
            self.highlighted = True

        def isHighlighted(self):
            return self.highlighted

    class _StatusItem:
        def __init__(self, button):
            self._button = button

        def button(self):
            return self._button

    calls = []

    class _Icon:
        def _update_menu(self):
            calls.append("original")

    fake_appkit = types.ModuleType("AppKit")
    fake_appkit.NSObject = _NSObject
    fake_foundation = types.ModuleType("Foundation")
    fake_objc = types.ModuleType("objc")
    fake_darwin = types.ModuleType("pystray._darwin")
    fake_darwin.Icon = _Icon
    fake_pystray = types.ModuleType("pystray")
    fake_pystray._darwin = fake_darwin

    monkeypatch.setitem(sys.modules, "AppKit", fake_appkit)
    monkeypatch.setitem(sys.modules, "Foundation", fake_foundation)
    monkeypatch.setitem(sys.modules, "objc", fake_objc)
    monkeypatch.setitem(sys.modules, "pystray", fake_pystray)
    monkeypatch.setitem(sys.modules, "pystray._darwin", fake_darwin)
    monkeypatch.setattr(macos_menu.sys, "platform", "darwin")
    monkeypatch.setattr(macos_menu, "_status_menu_session_open", False)

    macos_menu.install_live_status_menu()

    button = _Button()
    icon = _Icon()
    icon._status_item = _StatusItem(button)
    icon._menu_handle = (_Menu(),)
    icon._kg_live_menu_delegate = object()

    icon._update_menu()
    assert calls == []

    button.highlighted = False
    icon._update_menu()
    assert calls == ["original"]
