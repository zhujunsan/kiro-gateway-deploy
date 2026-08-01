# app/tests/test_tray_live.py
"""Windows/Linux menu-open refresh hooks + in-place live titles."""
from __future__ import annotations

import sys
import types

import pytest


@pytest.fixture(autouse=True)
def _reset_tray_live(monkeypatch):
    from kiro_gateway_tray import tray_live

    monkeypatch.setattr(tray_live, "_installed", False)
    monkeypatch.setattr(tray_live, "_in_will_open", False)
    monkeypatch.setattr(tray_live, "_active_icon", None)
    monkeypatch.setattr(tray_live, "_gtk_tick_source", None)
    monkeypatch.setattr(
        tray_live,
        "_hooks",
        {"will_open": None, "did_close": None, "tick": None},
    )
    yield


def test_install_open_refresh_noop_on_darwin(monkeypatch):
    from kiro_gateway_tray import tray_live

    monkeypatch.setattr(tray_live.sys, "platform", "darwin")
    called = {"n": 0}
    tray_live.install_open_refresh(on_will_open=lambda: called.__setitem__("n", 1))
    assert tray_live._installed is False
    assert called["n"] == 0


def test_install_win32_wraps_notify_and_fires_hooks(monkeypatch):
    from kiro_gateway_tray import tray_live

    monkeypatch.setattr(tray_live.sys, "platform", "win32")
    monkeypatch.setattr(tray_live, "_start_win32_tick", lambda _icon: None)
    monkeypatch.setattr(tray_live, "_stop_win32_tick", lambda _icon: None)

    events = []

    class _FakeWin32Util:
        WM_LBUTTONUP = 0x0202
        WM_RBUTTONUP = 0x0205
        WM_NOTIFY = 0x0400

    class _FakeIcon:
        def __init__(self):
            self._menu_handle = ("hmenu", [])
            self._hwnd = 1
            # Match pystray: Icon.__init__ caches the then-current bound
            # method. The production hook is installed after construction.
            self._message_handlers = {
                _FakeWin32Util.WM_NOTIFY: self._on_notify,
            }
            self.calls = []

        def _on_notify(self, wparam, lparam):
            self.calls.append(lparam)
            return 0

    fake_mod = types.ModuleType("pystray._win32")
    fake_mod.Icon = _FakeIcon
    fake_util = types.ModuleType("pystray._util.win32")
    for k, v in _FakeWin32Util.__dict__.items():
        if not k.startswith("_"):
            setattr(fake_util, k, v)

    pystray_mod = types.ModuleType("pystray")
    util_mod = types.ModuleType("pystray._util")
    monkeypatch.setitem(sys.modules, "pystray", pystray_mod)
    monkeypatch.setitem(sys.modules, "pystray._util", util_mod)
    monkeypatch.setitem(sys.modules, "pystray._win32", fake_mod)
    monkeypatch.setitem(sys.modules, "pystray._util.win32", fake_util)
    util_mod.win32 = fake_util

    icon = _FakeIcon()
    cached_before_install = icon._message_handlers[_FakeWin32Util.WM_NOTIFY]

    tray_live.install_open_refresh(
        icon=icon,
        on_will_open=lambda: events.append("open"),
        on_did_close=lambda: events.append("close"),
        on_tick=lambda: events.append("tick"),
    )
    assert tray_live._installed is True
    assert icon._message_handlers[_FakeWin32Util.WM_NOTIFY] != cached_before_install

    # Exercise the same cached message table used by pystray's real WndProc,
    # rather than calling the monkey-patched class method directly.
    icon._message_handlers[_FakeWin32Util.WM_NOTIFY](
        0, _FakeWin32Util.WM_RBUTTONUP
    )
    assert events == ["open", "close"]
    assert icon.calls == [_FakeWin32Util.WM_RBUTTONUP]

    events.clear()
    icon.calls.clear()
    icon._message_handlers[_FakeWin32Util.WM_NOTIFY](
        0, _FakeWin32Util.WM_LBUTTONUP
    )
    assert events == []
    assert icon.calls == [_FakeWin32Util.WM_LBUTTONUP]


def test_install_gtk_wraps_popup_menu(monkeypatch):
    from kiro_gateway_tray import tray_live

    monkeypatch.setattr(tray_live.sys, "platform", "linux")
    monkeypatch.setattr(tray_live, "_start_glib_tick", lambda: None)
    monkeypatch.setattr(tray_live, "_stop_glib_tick", lambda: None)
    monkeypatch.setattr(tray_live, "_install_appindicator_show", lambda: False)

    events = []

    class _FakeGtkIcon:
        def _on_status_icon_popup_menu(self, status_icon, button, activate_time):
            events.append("popup")

    fake_gtk = types.ModuleType("pystray._gtk")
    fake_gtk.Icon = _FakeGtkIcon
    monkeypatch.setitem(sys.modules, "pystray._gtk", fake_gtk)

    tray_live.install_open_refresh(
        on_will_open=lambda: events.append("open"),
        on_did_close=lambda: events.append("close"),
        on_tick=lambda: events.append("tick"),
    )
    assert tray_live._installed is True

    icon = _FakeGtkIcon()
    _FakeGtkIcon._on_status_icon_popup_menu(icon, None, 0, 0)
    assert events == ["open", "popup", "close"]


def test_fire_will_open_is_reentrant_safe(monkeypatch):
    from kiro_gateway_tray import tray_live

    monkeypatch.setattr(tray_live, "_start_tick", lambda: None)
    depth = {"n": 0}

    def _nested():
        depth["n"] += 1
        tray_live._fire_will_open()

    tray_live._hooks["will_open"] = _nested
    tray_live._fire_will_open()
    assert depth["n"] == 1


def test_sync_rebuild_menu_win32_calls_update_menu(monkeypatch):
    from kiro_gateway_tray import tray_live

    monkeypatch.setattr(tray_live.sys, "platform", "win32")
    styled = []
    monkeypatch.setattr(
        tray_live, "_apply_win32_no_check_style", lambda icon: styled.append(icon)
    )

    class _Icon:
        def __init__(self):
            self.n = 0

        def update_menu(self):
            self.n += 1

    icon = _Icon()
    tray_live.sync_rebuild_menu(icon)
    assert icon.n == 1
    assert styled == [icon]


def test_apply_win32_no_check_style_preserves_other_styles(monkeypatch):
    import ctypes

    from kiro_gateway_tray import tray_live

    captured = {}

    class _FakeFunction:
        def __init__(self, impl):
            self.impl = impl
            self.argtypes = None
            self.restype = None

        def __call__(self, *args):
            return self.impl(*args)

    def _get_menu_info(hmenu, info_ptr):
        captured["get_hmenu"] = hmenu
        info_ptr._obj.dwStyle = 0x20
        return 1

    def _set_menu_info(hmenu, info_ptr):
        captured["set_hmenu"] = hmenu
        captured["mask"] = info_ptr._obj.fMask
        captured["style"] = info_ptr._obj.dwStyle
        return 1

    class _FakeUser32:
        GetMenuInfo = _FakeFunction(_get_menu_info)
        SetMenuInfo = _FakeFunction(_set_menu_info)

    monkeypatch.setattr(
        ctypes,
        "WinDLL",
        lambda name, use_last_error=True: _FakeUser32(),
        raising=False,
    )

    class _Icon:
        _menu_handle = (0x1234, [])

    tray_live._apply_win32_no_check_style(_Icon())

    assert captured == {
        "get_hmenu": 0x1234,
        "set_hmenu": 0x1234,
        "mask": 0x80000010,  # MIM_APPLYTOSUBMENUS | MIM_STYLE
        "style": 0x80000020,  # existing style | MNS_NOCHECK
    }


def test_sync_rebuild_menu_gtk_creates_synchronously(monkeypatch):
    from kiro_gateway_tray import tray_live

    monkeypatch.setattr(tray_live.sys, "platform", "linux")

    class _Icon:
        def __init__(self):
            self.menu = object()
            self._menu_handle = None
            self.updated = 0

        def _create_menu(self, menu):
            return f"menu-for-{id(menu)}"

        def update_menu(self):
            self.updated += 1

    icon = _Icon()
    tray_live.sync_rebuild_menu(icon)
    assert icon._menu_handle == f"menu-for-{id(icon.menu)}"
    assert icon.updated == 0


def test_apply_live_titles_gtk_updates_in_place(monkeypatch):
    from kiro_gateway_tray import tray_live

    monkeypatch.setattr(tray_live.sys, "platform", "linux")

    class _Item:
        def __init__(self, label, submenu=None):
            self._label = label
            self._submenu = submenu

        def get_label(self):
            return self._label

        def set_label(self, t):
            self._label = t

        def get_submenu(self):
            return self._submenu

    class _Menu:
        def __init__(self, items):
            self._items = items

        def get_children(self):
            return self._items

    row = _Item("等待响应 · 1s · m\n⬆ 0 · ⬇ 0\n问: old")
    active_sub = _Menu([row])
    gateway = _Item("🖥 网关: 本地 Kiro Gateway\t启动中")
    active = _Item("📡 进行中\t空闲", submenu=active_sub)
    root = _Menu([gateway, active])

    class _Icon:
        _menu_handle = root

    tray_live.apply_live_titles(
        _Icon(),
        top_level_prefixes=[
            ("🖥 网关:", "🖥 网关: 本地 Kiro Gateway\t运行中"),
            ("📡 进行中", "📡 进行中 (1)\t最长 5s"),
        ],
        submenu_by_parent_prefix={
            "📡 进行中": ["生成中 · 5s · m\n⬆ 12 · ⬇ 3\n问: new"],
        },
    )
    assert gateway.get_label().endswith("运行中")
    assert "进行中 (1)" in active.get_label()
    assert "生成中 · 5s" in row.get_label()
    assert "⬆ 12" in row.get_label()


def test_non_macos_menu_will_open_defers_redraw(monkeypatch):
    """Regression: mid-popup DestroyMenu on Win32 must be deferred."""
    from kiro_gateway_tray.tray import TrayApp

    if "pystray" not in sys.modules:
        mod = types.ModuleType("pystray")

        class _MenuItem:
            def __init__(self, text, action=None, **kwargs):
                self.text = text

        class _Menu:
            SEPARATOR = object()

            def __init__(self, *items):
                self.items = items

        mod.MenuItem = _MenuItem
        mod.Menu = _Menu
        monkeypatch.setitem(sys.modules, "pystray", mod)

    app = TrayApp()
    rebuilt = {"n": 0}

    class _Icon:
        def update_menu(self):
            rebuilt["n"] += 1

    app._icon = _Icon()
    monkeypatch.setattr(app, "_on_menu_open", lambda: None)
    monkeypatch.setattr(app, "_refresh_activity_cache_quiet", lambda: None)

    app._on_non_macos_menu_will_open()
    assert app._menu_session_open is True
    assert rebuilt["n"] == 1

    from kiro_gateway_tray import macos_menu

    monkeypatch.setattr(macos_menu, "is_status_menu_open", lambda _ic: False)
    monkeypatch.setattr(macos_menu, "run_on_main_thread", lambda fn: fn())
    app._request_redraw()
    assert app._redraw_deferred is True
    assert rebuilt["n"] == 1

    app._on_non_macos_menu_did_close()
    assert app._menu_session_open is False
    assert rebuilt["n"] == 2


def test_non_macos_tick_patches_while_open(monkeypatch):
    from kiro_gateway_tray.request_activity import ActivitySnapshot
    from kiro_gateway_tray.tray import TrayApp

    if "pystray" not in sys.modules:
        mod = types.ModuleType("pystray")

        class _MenuItem:
            def __init__(self, text, action=None, **kwargs):
                self.text = text

        class _Menu:
            SEPARATOR = object()

            def __init__(self, *items):
                self.items = items

        mod.MenuItem = _MenuItem
        mod.Menu = _Menu
        monkeypatch.setitem(sys.modules, "pystray", mod)

    app = TrayApp()
    app._menu_session_open = True
    patched = {"n": 0}
    monkeypatch.setattr(
        app,
        "_live_patch_open_menu_crossplatform",
        lambda snap: patched.__setitem__("n", patched["n"] + 1),
    )
    monkeypatch.setattr(
        "kiro_gateway_tray.tray.request_activity.load_snapshot",
        lambda: ActivitySnapshot(),
    )
    monkeypatch.setattr(app, "_set_activity_cache_value", lambda snap: None)

    # Force non-darwin path
    monkeypatch.setattr(sys, "platform", "linux")
    app._on_non_macos_menu_tick()
    assert patched["n"] == 1
