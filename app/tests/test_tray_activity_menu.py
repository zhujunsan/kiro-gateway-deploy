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
    assert "等待响应" in active_items[0].text

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
    monkeypatch.setattr(app.sup, "status", lambda: {
        "gateway": "running",
        "tunnel": "connecting",
    })
    monkeypatch.setattr(app, "_on_menu_open", lambda: None)
    redraws = {"n": 0}
    monkeypatch.setattr(app, "_request_redraw", lambda: redraws.__setitem__("n", redraws["n"] + 1))

    path = tmp_path / "request_activity.json"
    store = ra.RequestActivityStore(path)
    rid = store.begin(model="m", path="/v1/messages", question_preview="live?")
    store.set_phase(rid, "streaming")
    store.finish(
        store.begin(model="m", path="/v1/messages", question_preview="old q"),
        ok=True,
        answer_preview="old a",
    )

    orig_load = ra.load_snapshot
    monkeypatch.setattr(
        "kiro_gateway_tray.tray.request_activity.load_snapshot",
        lambda: orig_load(path),
    )

    class _Item:
        def __init__(self, title, submenu=None):
            self._title = title
            self._submenu = submenu
            self._enabled = True

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

        def setEnabled_(self, enabled):
            self._enabled = bool(enabled)

        def isEnabled(self):
            return self._enabled

        def setTarget_(self, _t):
            pass

        def setAction_(self, _a):
            pass

        def setTag_(self, _tag):
            pass

    class _Sub:
        def __init__(self, items):
            self._items = list(items)

        def numberOfItems(self):
            return len(self._items)

        def itemAtIndex_(self, i):
            return self._items[i]

        def removeAllItems(self):
            self._items.clear()

        def addItem_(self, item):
            self._items.append(item)

    class _Root:
        def __init__(self, items):
            self._items = items

        def numberOfItems(self):
            return len(self._items)

        def itemAtIndex_(self, i):
            return self._items[i]

    active_sub = _Sub([_Item("当前无进行中的请求")])
    recent_sub = _Sub([_Item("暂无最近对话")])
    gateway = _Item("🖥 网关: 本地 Kiro Gateway\t启动中")
    tunnel = _Item("🌐 隧道: Cloudflare Tunnel\t已停止")
    active = _Item("📡 进行中\t空闲", submenu=active_sub)
    recent = _Item("💬 最近对话", submenu=recent_sub)
    root = _Root([gateway, tunnel, active, recent])

    for sub in (active_sub, recent_sub):
        sub.setDelegate_ = lambda _d: None

    # Bypass AppKit attributed path in tests
    monkeypatch.setattr(macos_menu, "apply_menu_item_title", lambda item, title: item.setTitle_(title))
    monkeypatch.setattr(macos_menu, "make_live_click_delegate", lambda: None)
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
    assert gateway._title.endswith("运行中")
    assert tunnel._title.endswith("连接中")
    assert "进行中 (1)" in active._title
    assert "最长" in active._title
    assert active_sub.numberOfItems() == 1
    assert "生成中" in active_sub.itemAtIndex_(0).title()
    assert recent_sub.numberOfItems() == 1
    recent_title = recent_sub.itemAtIndex_(0).title()
    assert "old q" in recent_title
    assert "old a" in recent_title
    assert redraws["n"] == 0

    app._redraw_deferred = True
    app._on_status_menu_did_close(root)
    assert app._menu_session_open is False
    assert redraws["n"] == 1


def test_supervisor_status_change_live_patches_while_menu_open(monkeypatch):
    from kiro_gateway_tray import macos_menu

    app = _make_app()
    app._menu_session_open = True
    patched = {"n": 0}
    redraws = {"n": 0}
    # darwin uses AppKit title patch; Win/Linux use cross-platform in-place patch.
    monkeypatch.setattr(
        app,
        "_live_patch_status_titles",
        lambda nsmenu=None: patched.__setitem__("n", patched["n"] + 1),
    )
    monkeypatch.setattr(
        app,
        "_live_patch_open_menu_crossplatform",
        lambda snap: patched.__setitem__("n", patched["n"] + 1),
    )
    monkeypatch.setattr(app, "_request_redraw", lambda: redraws.__setitem__("n", redraws["n"] + 1))
    monkeypatch.setattr(macos_menu, "run_on_main_thread", lambda fn: fn())

    app._on_supervisor_status_change()
    assert patched["n"] == 1
    assert redraws["n"] == 0
    assert app._redraw_deferred is True

    app._menu_session_open = False
    monkeypatch.setattr(macos_menu, "is_status_menu_open", lambda _ic: False)
    app._on_supervisor_status_change()
    assert redraws["n"] == 1


def test_activity_update_while_menu_open_defers_and_patches(monkeypatch):
    app = _make_app()
    monkeypatch.setattr(app.sup, "status", lambda: {"gateway": "running", "tunnel": "running"})
    app._menu_session_open = True
    redraws = {"n": 0}
    patches = {"n": 0}
    monkeypatch.setattr(app, "_request_redraw", lambda: redraws.__setitem__("n", redraws["n"] + 1))
    monkeypatch.setattr(
        app,
        "_live_patch_open_menu",
        lambda snap, nsmenu=None, force_rebuild=False: patches.__setitem__("n", patches["n"] + 1),
    )
    monkeypatch.setattr(
        app,
        "_live_patch_open_menu_crossplatform",
        lambda snap: patches.__setitem__("n", patches["n"] + 1),
    )

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


def test_inplace_patch_flips_finished_rows_to_completed(monkeypatch):
    """Regression: finished slots must not freeze on 「生成中」 while menu is open."""
    from kiro_gateway_tray import macos_menu

    app = _make_app()
    monkeypatch.setattr(app.sup, "status", lambda: {"gateway": "running"})
    monkeypatch.setattr(macos_menu, "apply_menu_item_title", lambda item, title: item.setTitle_(title))

    class _Item:
        def __init__(self, title):
            self._title = title

        def title(self):
            return self._title

        def setTitle_(self, t):
            self._title = t

        def isSeparatorItem(self):
            return False

    class _Sub:
        def __init__(self, items):
            self._items = list(items)

        def numberOfItems(self):
            return len(self._items)

        def itemAtIndex_(self, i):
            return self._items[i]

    class _Parent:
        def __init__(self, submenu):
            self._submenu = submenu

        def submenu(self):
            return self._submenu

    t0 = time.time() - 30
    row_a = _Item("生成中 · 30s · glm-5\n⬆ 10 · ⬇ 2\n问: old-a")
    row_b = _Item("生成中 · 16s · glm-5\n⬆ 8 · ⬇ 1\n问: old-b")
    parent = _Parent(_Sub([row_a, row_b]))
    app._live_active_ids = ("a", "b")

    # One finished successfully, one still streaming.
    snap = ActivitySnapshot(
        active=[
            ActiveRequest(
                id="b",
                started_at=t0 + 14,
                model="glm-5",
                path="/v1/messages",
                phase="streaming",
                question_preview="still going",
            ),
        ],
        recent=[
            RecentRequest(
                id="a",
                started_at=t0,
                finished_at=t0 + 30,
                model="glm-5",
                path="/v1/messages",
                ok=True,
                duration_ms=30_000,
                question_preview="done q",
                answer_preview="done a",
            ),
        ],
    )
    app._inplace_patch_active_submenu(parent, snap)
    assert row_a._title.startswith("已完成 ·")
    assert "done q" in row_a._title
    assert "生成中" in row_b._title
    assert "still going" in row_b._title
    # Slot membership stays stable while the submenu is displayed.
    assert app._live_active_ids == ("a", "b")

    # Both finished (one failed): titles flip, count of rows unchanged.
    snap_done = ActivitySnapshot(
        active=[],
        recent=[
            RecentRequest(
                id="b",
                started_at=t0 + 14,
                finished_at=t0 + 40,
                model="glm-5",
                path="/v1/messages",
                ok=False,
                duration_ms=26_000,
                question_preview="still going",
                answer_preview="",
                error_preview="HTTP 500",
            ),
            RecentRequest(
                id="a",
                started_at=t0,
                finished_at=t0 + 30,
                model="glm-5",
                path="/v1/messages",
                ok=True,
                duration_ms=30_000,
                question_preview="done q",
                answer_preview="done a",
            ),
        ],
    )
    app._inplace_patch_active_submenu(parent, snap_done)
    assert row_a._title.startswith("已完成 ·")
    assert row_b._title.startswith("失败 ·")
    assert app._live_active_ids == ("a", "b")


def test_inplace_patch_recent_submenu_updates_open_list(monkeypatch):
    """Regression: finishing a request must refresh an already-open 最近对话 list."""
    from kiro_gateway_tray import macos_menu

    app = _make_app()
    monkeypatch.setattr(app.sup, "status", lambda: {"gateway": "running"})
    monkeypatch.setattr(macos_menu, "apply_menu_item_title", lambda item, title: item.setTitle_(title))

    class _Item:
        def __init__(self, title, enabled=True):
            self._title = title
            self._enabled = enabled

        def title(self):
            return self._title

        def setTitle_(self, t):
            self._title = t

        def isSeparatorItem(self):
            return False

        def setEnabled_(self, enabled):
            self._enabled = bool(enabled)

        def setTarget_(self, _t):
            pass

        def setAction_(self, _a):
            pass

        def setTag_(self, _tag):
            pass

    class _Sub:
        def __init__(self, items):
            self._items = list(items)

        def numberOfItems(self):
            return len(self._items)

        def itemAtIndex_(self, i):
            return self._items[i]

    class _Parent:
        def __init__(self, submenu):
            self._submenu = submenu

        def submenu(self):
            return self._submenu

    placeholder = _Item("暂无最近对话", enabled=False)
    parent = _Parent(_Sub([placeholder]))
    app._live_recent_fp = repr(())

    snap = ActivitySnapshot(
        active=[],
        recent=[
            RecentRequest(
                id="new",
                started_at=1,
                finished_at=2,
                model="m",
                path="/v1/messages",
                ok=True,
                duration_ms=900,
                question_preview="just finished",
                answer_preview="ok",
            ),
        ],
    )
    app._inplace_patch_recent_submenu(parent, snap)
    assert "just finished" in placeholder._title
    assert placeholder._enabled is True
    assert app._live_recent_fp == app._recent_fingerprint_of(snap)


def test_live_patch_open_menu_refreshes_recent_without_force_rebuild(monkeypatch):
    """While the root menu is open, recent fingerprint changes must patch titles."""
    from kiro_gateway_tray import macos_menu

    app = _make_app()
    monkeypatch.setattr(app.sup, "status", lambda: {"gateway": "running"})
    monkeypatch.setattr(macos_menu, "apply_menu_item_title", lambda item, title: item.setTitle_(title))

    class _Item:
        def __init__(self, title, submenu=None):
            self._title = title
            self._submenu = submenu

        def title(self):
            return self._title

        def setTitle_(self, t):
            self._title = t

        def isSeparatorItem(self):
            return False

        def submenu(self):
            return self._submenu

        def setEnabled_(self, _e):
            pass

        def setTarget_(self, _t):
            pass

        def setAction_(self, _a):
            pass

        def setTag_(self, _tag):
            pass

    class _Sub:
        def __init__(self, items):
            self._items = list(items)

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

    old_row = _Item("12:00 ✓ 1.0s · m\n⬆ 1 · ⬇ 1\n问: old\n答: old")
    recent_sub = _Sub([old_row])
    recent = _Item("💬 最近对话", submenu=recent_sub)
    active = _Item("📡 进行中\t空闲", submenu=_Sub([_Item("当前无进行中的请求")]))
    root = _Root([active, recent])

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

    app._live_recent_fp = "stale"
    snap = ActivitySnapshot(
        active=[],
        recent=[
            RecentRequest(
                id="r2",
                started_at=10,
                finished_at=11,
                model="m",
                path="/v1/messages",
                ok=True,
                duration_ms=500,
                question_preview="brand new",
                answer_preview="done",
            ),
        ],
    )
    app._live_patch_open_menu(snap, root, force_rebuild=False)
    assert "brand new" in old_row._title
    assert app._live_recent_fp == app._recent_fingerprint_of(snap)


def test_live_click_delegate_can_be_created_twice():
    """Regression: active + recent each need an instance; class must be cached."""
    import sys

    from kiro_gateway_tray import macos_menu

    if sys.platform != "darwin":
        assert macos_menu.make_live_click_delegate() is None
        return
    a = macos_menu.make_live_click_delegate()
    b = macos_menu.make_live_click_delegate()
    assert a is not None
    assert b is not None
    assert a is not b


def test_submenu_autorebuild_can_attach_twice():
    """Regression: both 进行中 and 最近对话 must get menuNeedsUpdate delegates."""
    import sys

    from kiro_gateway_tray import macos_menu

    if sys.platform != "darwin":
        return
    import AppKit

    calls = {"n": 0}

    def _cb(_menu):
        calls["n"] += 1

    m1 = AppKit.NSMenu.alloc().init()
    m2 = AppKit.NSMenu.alloc().init()
    d1 = macos_menu.attach_submenu_autorebuild(m1, _cb)
    d2 = macos_menu.attach_submenu_autorebuild(m2, _cb)
    assert d1 is not None
    assert d2 is not None
    assert d1 is not d2


def test_inplace_patch_replaces_finished_slot_with_new_active(monkeypatch):
    """Regression: open submenu must show the new in-flight request, not stale 已完成."""
    from kiro_gateway_tray import macos_menu

    app = _make_app()
    monkeypatch.setattr(app.sup, "status", lambda: {"gateway": "running"})
    monkeypatch.setattr(macos_menu, "apply_menu_item_title", lambda item, title: item.setTitle_(title))

    class _Item:
        def __init__(self, title):
            self._title = title

        def title(self):
            return self._title

        def setTitle_(self, t):
            self._title = t

        def isSeparatorItem(self):
            return False

    class _Sub:
        def __init__(self, items):
            self._items = list(items)

        def numberOfItems(self):
            return len(self._items)

        def itemAtIndex_(self, i):
            return self._items[i]

    class _Parent:
        def __init__(self, submenu):
            self._submenu = submenu

        def submenu(self):
            return self._submenu

    t0 = time.time() - 20
    row = _Item("已完成 · 6.2s · claude-opus-4.8\n⬆ 98.7k · ⬇ 18\n问: old")
    parent = _Parent(_Sub([row]))
    # Previous request finished while the submenu stayed open.
    app._live_active_ids = ("old",)

    snap = ActivitySnapshot(
        active=[
            ActiveRequest(
                id="new",
                started_at=t0 + 14,
                model="claude-opus-4.8",
                path="/v1/messages",
                phase="streaming",
                question_preview="new question",
                prompt_tokens=100,
                completion_tokens=5,
            ),
        ],
        recent=[
            RecentRequest(
                id="old",
                started_at=t0,
                finished_at=t0 + 6,
                model="claude-opus-4.8",
                path="/v1/messages",
                ok=True,
                duration_ms=6200,
                question_preview="old",
                answer_preview="done",
                prompt_tokens=98700,
                completion_tokens=18,
            ),
        ],
    )
    app._inplace_patch_active_submenu(parent, snap)
    assert "生成中" in row._title
    assert "new question" in row._title
    assert "已完成" not in row._title
    assert app._live_active_ids == ("new",)


def test_live_active_row_titles_crossplatform_keeps_finished_slots(monkeypatch):
    app = _make_app()
    monkeypatch.setattr(app.sup, "status", lambda: {"gateway": "running"})
    app._live_active_ids = ("x",)
    snap = ActivitySnapshot(
        active=[],
        recent=[
            RecentRequest(
                id="x",
                started_at=1,
                finished_at=2,
                model="m",
                path="/v1/messages",
                ok=True,
                duration_ms=1600,
                question_preview="q",
                answer_preview="a",
            ),
        ],
    )
    titles = app._live_active_row_titles(snap)
    assert len(titles) == 1
    assert titles[0].startswith("已完成 ·")

