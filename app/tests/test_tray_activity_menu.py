# app/tests/test_tray_activity_menu.py
"""Tray menu surfaces in-flight / recent request activity."""
from __future__ import annotations

import sys
import types
import threading
import time

import pytest

from kiro_gateway_tray.request_activity import (
    ActiveRequest,
    ActivitySnapshot,
    RecentRequest,
)


@pytest.fixture(autouse=True)
def _stub_pystray(monkeypatch):
    if "pystray" not in sys.modules:
        mod = types.ModuleType("pystray")

        class _MenuItem:
            def __init__(self, text, action=None, **kwargs):
                self.text = text
                self.action = action
                self.kwargs = kwargs

        class _Menu:
            SEPARATOR = object()

            def __init__(self, *items):
                self.items = items

        mod.MenuItem = _MenuItem
        mod.Menu = _Menu
        monkeypatch.setitem(sys.modules, "pystray", mod)


def _make_app():
    from kiro_gateway_tray.tray import TrayApp
    return TrayApp()


def test_activity_active_line_idle_and_busy(monkeypatch):
    app = _make_app()
    monkeypatch.setattr(app.sup, "status", lambda: {"gateway": "running"})

    idle = ActivitySnapshot(active=[], recent=[])
    monkeypatch.setattr(app._activity_cache, "get", lambda: idle)
    monkeypatch.setattr(app._activity_cache, "refresh", lambda **kw: None)
    assert "空闲" in app._activity_active_line(None)

    busy = ActivitySnapshot(active=[
        ActiveRequest(
            id="1",
            started_at=time.time() - 47,
            model="claude",
            path="/v1/messages",
            phase="streaming",
            question_preview="慢吗",
        ),
    ])
    monkeypatch.setattr(app._activity_cache, "get", lambda: busy)
    line = app._activity_active_line(None)
    assert "进行中 (1)" in line
    assert "最长" in line


def test_activity_submenus_render_rows(monkeypatch):
    app = _make_app()
    monkeypatch.setattr(app.sup, "status", lambda: {"gateway": "running"})
    snap = ActivitySnapshot(
        active=[
            ActiveRequest(
                id="1",
                started_at=time.time() - 5,
                model="m",
                path="/v1/messages",
                phase="waiting",
                question_preview="hello",
            ),
        ],
        recent=[
            RecentRequest(
                id="2",
                started_at=1,
                finished_at=2,
                model="m",
                path="/v1/messages",
                ok=True,
                duration_ms=800,
                question_preview="q",
                answer_preview="a",
            ),
        ],
    )
    monkeypatch.setattr(app._activity_cache, "get", lambda: snap)
    monkeypatch.setattr(app._activity_cache, "refresh", lambda **kw: None)

    active_items = app._activity_active_submenu()
    assert len(active_items) == 1
    assert "等待首包" in active_items[0].text

    recent_items = app._activity_recent_submenu()
    assert len(recent_items) == 1
    assert "✓" in recent_items[0].text


def test_activity_refresh_loop_ticks_while_running(monkeypatch):
    app = _make_app()
    monkeypatch.setattr(app.sup, "status", lambda: {"gateway": "running"})
    refreshed = threading.Event()
    monkeypatch.setattr(app._usage_refresh_stop, "wait", lambda _t: False)

    def _refresh_once(**_kw):
        refreshed.set()
        app._usage_refresh_stop.set()

    monkeypatch.setattr(app._activity_cache, "refresh", _refresh_once)
    app._start_activity_refresh_loop()
    assert refreshed.wait(timeout=2.0), "activity loop never refreshed"


def test_activity_fingerprint_skips_idle_noop_redraw(monkeypatch):
    app = _make_app()
    redraws = {"n": 0}
    monkeypatch.setattr(app, "_request_redraw", lambda: redraws.__setitem__("n", redraws["n"] + 1))

    snap = ActivitySnapshot(active=[], recent=[
        RecentRequest(
            id="r",
            started_at=1,
            finished_at=2,
            model="m",
            path="/v1/messages",
            ok=True,
            duration_ms=1,
            question_preview="q",
            answer_preview="a",
        ),
    ])
    monkeypatch.setattr(app._activity_cache, "get", lambda: snap)
    app._on_activity_update()
    app._on_activity_update()
    assert redraws["n"] == 1


def test_activity_fingerprint_skips_redraw_while_active_unchanged(monkeypatch):
    """Regression: redrawing every few seconds while active froze macOS input."""
    app = _make_app()
    redraws = {"n": 0}
    monkeypatch.setattr(app, "_request_redraw", lambda: redraws.__setitem__("n", redraws["n"] + 1))

    snap = ActivitySnapshot(active=[
        ActiveRequest(
            id="1",
            started_at=time.time() - 10,
            model="m",
            path="/v1/messages",
            phase="streaming",
            question_preview="slow",
        ),
    ])
    monkeypatch.setattr(app._activity_cache, "get", lambda: snap)
    app._on_activity_update()
    app._on_activity_update()
    app._on_activity_update()
    assert redraws["n"] == 1


def test_request_redraw_defers_when_status_menu_open(monkeypatch):
    from unittest.mock import MagicMock

    from kiro_gateway_tray import macos_menu, tray as tray_mod

    app = _make_app()
    icon = MagicMock()
    app._icon = icon

    def _inline(fn):
        fn()

    monkeypatch.setattr(macos_menu, "run_on_main_thread", _inline)
    monkeypatch.setattr(macos_menu, "is_status_menu_open", lambda _ic: True)

    app._request_redraw()
    assert app._redraw_deferred is True
    icon.update_menu.assert_not_called()

    monkeypatch.setattr(macos_menu, "is_status_menu_open", lambda _ic: False)
    app._request_redraw()
    assert app._redraw_deferred is False
    icon.update_menu.assert_called_once_with()
    assert tray_mod.TrayApp is not None


def test_status_menu_will_open_patches_without_update_menu(monkeypatch, tmp_path):
    from kiro_gateway_tray import request_activity as ra
    from kiro_gateway_tray import macos_menu

    app = _make_app()
    monkeypatch.setattr(app.sup, "status", lambda: {"gateway": "running"})
    monkeypatch.setattr(app, "_on_menu_open", lambda: None)
    redraws = {"n": 0}
    monkeypatch.setattr(app, "_request_redraw", lambda: redraws.__setitem__("n", redraws["n"] + 1))

    path = tmp_path / "request_activity.json"
    store = ra.RequestActivityStore(path)
    rid = store.begin(model="m", path="/v1/messages", question_preview="live?")
    store.set_phase(rid, "streaming")

    orig_load = ra.load_snapshot
    monkeypatch.setattr(
        "kiro_gateway_tray.tray.request_activity.load_snapshot",
        lambda: orig_load(path),
    )

    class _Item:
        def __init__(self, title, submenu=None):
            self._title = title
            self._submenu = submenu

        def title(self):
            return self._title

        def isSeparatorItem(self):
            return False

        def submenu(self):
            return self._submenu

        def setTitle_(self, t):
            self._title = t

        def setAttributedTitle_(self, _attr):
            pass

    class _Sub:
        def __init__(self, items):
            self._items = items

        def numberOfItems(self):
            return len(self._items)

        def itemAtIndex_(self, i):
            return self._items[i]

    class _Root:
        def __init__(self, items):
            self._items = items

        def numberOfItems(self):
            return len(self._items)

        def itemAtIndex_(self, i):
            return self._items[i]

    row = _Item("等待首包 · 0.0s · m\thello")
    sub = _Sub([row])
    active = _Item("📡 进行中\t空闲", submenu=sub)
    recent = _Item("💬 最近对话", submenu=_Sub([]))
    root = _Root([active, recent])

    # Bypass AppKit attributed path in tests
    monkeypatch.setattr(macos_menu, "apply_menu_item_title", lambda item, title: item.setTitle_(title))
    monkeypatch.setattr(
        macos_menu,
        "find_menu_item_by_title_prefix",
        lambda menu, prefix: next(
            (menu.itemAtIndex_(i) for i in range(menu.numberOfItems())
             if str(menu.itemAtIndex_(i).title()).startswith(prefix)),
            None,
        ),
    )
    monkeypatch.setattr(
        macos_menu,
        "find_menu_item_by_exact_title",
        lambda menu, title: next(
            (menu.itemAtIndex_(i) for i in range(menu.numberOfItems())
             if str(menu.itemAtIndex_(i).title()) == title),
            None,
        ),
    )

    app._on_status_menu_will_open(root)
    assert app._menu_session_open is True
    assert "进行中 (1)" in active._title
    assert "最长" in active._title
    assert "流式中" in row._title
    assert redraws["n"] == 0

    app._redraw_deferred = True
    app._on_status_menu_did_close(root)
    assert app._menu_session_open is False
    assert redraws["n"] == 1


def test_activity_update_while_menu_open_defers_and_patches(monkeypatch):
    app = _make_app()
    monkeypatch.setattr(app.sup, "status", lambda: {"gateway": "running"})
    app._menu_session_open = True
    redraws = {"n": 0}
    patches = {"n": 0}
    monkeypatch.setattr(app, "_request_redraw", lambda: redraws.__setitem__("n", redraws["n"] + 1))
    monkeypatch.setattr(app, "_live_patch_open_menu", lambda snap, nsmenu=None: patches.__setitem__("n", patches["n"] + 1))

    snap = ActivitySnapshot(active=[
        ActiveRequest(
            id="1",
            started_at=time.time(),
            model="m",
            path="/v1/messages",
            phase="waiting",
            question_preview="x",
        ),
    ])
    monkeypatch.setattr(app._activity_cache, "get", lambda: snap)
    app._on_activity_update()
    assert redraws["n"] == 0
    assert patches["n"] == 1
    assert app._redraw_deferred is True


def test_is_status_menu_open_respects_session_flag(monkeypatch):
    from kiro_gateway_tray import macos_menu

    monkeypatch.setattr(macos_menu, "_status_menu_session_open", True)
    assert macos_menu.is_status_menu_open(None) is True
    monkeypatch.setattr(macos_menu, "_status_menu_session_open", False)
    assert macos_menu.is_status_menu_open(None) is False

