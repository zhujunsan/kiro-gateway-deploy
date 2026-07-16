# app/kiro_gateway_tray/tray_live.py
"""Windows / Linux live tray-menu refresh for pystray.

macOS already has ``macos_menu.install_live_status_menu`` (AppKit delegates +
in-place ``setTitle:``). Off macOS:

  1. Open hooks rebuild the menu *before* the popup is shown (label callables
     are otherwise frozen until the next ``update_menu``).
  2. While the popup is open, a 1s tick patches item titles **in place**
     (Win32 ``SetMenuItemInfo``, GTK ``set_label``) — full ``update_menu`` /
     ``DestroyMenu`` during tracking is unsafe.
  3. On close we flush any deferred structural redraw.
"""
from __future__ import annotations

import sys
from typing import Any, Callable

_WillOpen = Callable[[], None]
_DidClose = Callable[[], None]
_Tick = Callable[[], None]

_hooks: dict[str, Any] = {
    "will_open": None,
    "did_close": None,
    "tick": None,
}
_installed = False
_in_will_open = False
_tick_interval_ms = 1000
_active_icon = None
_gtk_tick_source = None

# Win32
_WM_TIMER = 0x0113
_TIMER_ID = 0x4B470001  # 'KG\x00\x01'


def install_open_refresh(
    *,
    on_will_open: _WillOpen | None = None,
    on_did_close: _DidClose | None = None,
    on_tick: _Tick | None = None,
    tick_interval: float = 1.0,
) -> None:
    """Install open/close/tick hooks on the active non-macOS pystray backend.

    No-op on macOS (use ``macos_menu.install_live_status_menu``) and idempotent.
    """
    global _installed, _tick_interval_ms
    if sys.platform == "darwin":
        return
    _hooks["will_open"] = on_will_open
    _hooks["did_close"] = on_did_close
    _hooks["tick"] = on_tick
    _tick_interval_ms = max(250, int(float(tick_interval) * 1000))
    if _installed:
        return
    if sys.platform == "win32":
        ok = _install_win32()
    else:
        ok = _install_linux()
    _installed = bool(ok)


def sync_rebuild_menu(icon) -> None:
    """Rebuild ``icon``'s menu immediately (bypass GTK ``@mainloop`` idle).

    Win32 ``update_menu`` is already synchronous. GTK / AppIndicator wrap
    ``_update_menu`` in ``GObject.idle_add``, so calling ``update_menu()`` right
    before a popup would rebuild *after* the stale menu is shown — we must
    recreate the Gtk.Menu on the current stack instead.
    """
    if icon is None:
        return
    try:
        if sys.platform == "win32":
            icon.update_menu()
            return
        create = getattr(icon, "_create_menu", None)
        if create is None:
            icon.update_menu()
            return
        menu = create(icon.menu)
        appindicator = getattr(icon, "_appindicator", None)
        if appindicator is not None:
            if menu is None:
                default = getattr(icon, "_create_default_menu", None)
                menu = default() if default is not None else None
            icon._menu_handle = menu
            if menu is not None:
                appindicator.set_menu(menu)
            return
        icon._menu_handle = menu
    except Exception:
        try:
            icon.update_menu()
        except Exception:
            pass


def apply_live_titles(
    icon,
    *,
    top_level_prefixes: list[tuple[str, str]] | None = None,
    submenu_by_parent_prefix: dict[str, list[str | tuple[str, bool]]] | None = None,
    submenu_by_parent_exact: dict[str, list[str | tuple[str, bool]]] | None = None,
) -> None:
    """In-place title updates for an already-open tray popup.

    Safe during Win32 ``TrackPopupMenuEx`` / GTK menu grab — does not destroy
    or replace the menu. Only existing rows are updated (by index); structural
    add/remove is deferred to the next full rebuild after close. Submenu rows
    may be ``title``, ``(title, enabled)``, or ``(title, enabled, visible)``
    for stable dynamic slots (blank spare slots stay hidden).
    """
    if icon is None:
        return
    top_level_prefixes = top_level_prefixes or []
    submenu_by_parent_prefix = submenu_by_parent_prefix or {}
    submenu_by_parent_exact = submenu_by_parent_exact or {}
    try:
        if sys.platform == "win32":
            _apply_live_titles_win32(
                icon,
                top_level_prefixes,
                submenu_by_parent_prefix,
                submenu_by_parent_exact,
            )
        else:
            _apply_live_titles_gtk(
                icon,
                top_level_prefixes,
                submenu_by_parent_prefix,
                submenu_by_parent_exact,
            )
    except Exception:
        pass


def _parse_live_row(row: str | tuple) -> tuple[str, bool | None, bool | None]:
    """Normalize a live submenu row to ``(title, enabled, visible)``."""
    if isinstance(row, tuple):
        if len(row) >= 3:
            return str(row[0]), bool(row[1]), bool(row[2])
        if len(row) == 2:
            title, enabled = str(row[0]), bool(row[1])
            return title, enabled, bool(enabled) or bool(title)
        return str(row[0]) if row else "", None, None
    title = str(row)
    return title, None, bool(title)


# --- open/close/tick firing -------------------------------------------------

def _fire_will_open() -> None:
    global _in_will_open
    if _in_will_open:
        return
    cb = _hooks.get("will_open")
    _in_will_open = True
    try:
        if cb is not None:
            try:
                cb()
            except Exception:
                pass
        _start_tick()
    finally:
        _in_will_open = False


def _fire_did_close() -> None:
    _stop_tick()
    cb = _hooks.get("did_close")
    if cb is None:
        return
    try:
        cb()
    except Exception:
        pass


def _fire_tick() -> None:
    cb = _hooks.get("tick")
    if cb is None:
        return
    try:
        cb()
    except Exception:
        pass


def _start_tick() -> None:
    icon = _active_icon
    if icon is None or _hooks.get("tick") is None:
        return
    if sys.platform == "win32":
        _start_win32_tick(icon)
    else:
        _start_glib_tick()


def _stop_tick() -> None:
    icon = _active_icon
    if sys.platform == "win32":
        if icon is not None:
            _stop_win32_tick(icon)
    else:
        _stop_glib_tick()


# --- Win32 ------------------------------------------------------------------

def _install_win32() -> bool:
    global _active_icon
    try:
        from pystray import _win32
        from pystray._util import win32 as win32_util
    except Exception:
        return False

    orig = _win32.Icon._on_notify

    def _patched(self, wparam, lparam):
        global _active_icon
        if lparam == win32_util.WM_RBUTTONUP and getattr(self, "_menu_handle", None):
            _active_icon = self
            _fire_will_open()
            try:
                return orig(self, wparam, lparam)
            finally:
                _fire_did_close()
                _active_icon = None
        return orig(self, wparam, lparam)

    _win32.Icon._on_notify = _patched
    return True


def _start_win32_tick(icon) -> None:
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:
        return
    hwnd = getattr(icon, "_hwnd", None)
    if not hwnd:
        return
    handlers = getattr(icon, "_message_handlers", None)
    if isinstance(handlers, dict) and _WM_TIMER not in handlers:
        handlers[_WM_TIMER] = lambda w, l: _on_win32_timer(w, l)
    try:
        ctypes.windll.user32.SetTimer(
            wintypes.HWND(hwnd), _TIMER_ID, _tick_interval_ms, None
        )
    except Exception:
        pass


def _stop_win32_tick(icon) -> None:
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:
        return
    hwnd = getattr(icon, "_hwnd", None)
    if not hwnd:
        return
    try:
        ctypes.windll.user32.KillTimer(wintypes.HWND(hwnd), _TIMER_ID)
    except Exception:
        pass


def _on_win32_timer(wparam, _lparam):
    if int(wparam) != _TIMER_ID:
        return 0
    _fire_tick()
    return 0


def _apply_live_titles_win32(
    icon,
    top_level_prefixes: list[tuple[str, str]],
    submenu_by_parent_prefix: dict[str, list[str | tuple[str, bool]]],
    submenu_by_parent_exact: dict[str, list[str | tuple[str, bool]]],
) -> None:
    import ctypes
    from ctypes import wintypes

    handle = getattr(icon, "_menu_handle", None)
    if not handle:
        return
    hmenu = handle[0]
    if not hmenu:
        return

    user32 = ctypes.windll.user32
    GetMenuItemCount = user32.GetMenuItemCount
    GetMenuItemInfoW = user32.GetMenuItemInfoW
    SetMenuItemInfoW = user32.SetMenuItemInfoW
    GetSubMenu = user32.GetSubMenu

    # Prefer pystray's MENUITEMINFO layout when available.
    try:
        from pystray._util import win32 as win32_util
        MENUITEMINFO = win32_util.MENUITEMINFO
        MIIM_STRING = win32_util.MIIM_STRING
        MIIM_FTYPE = win32_util.MIIM_FTYPE
        MIIM_STATE = win32_util.MIIM_STATE
        MFT_SEPARATOR = win32_util.MFT_SEPARATOR
        MFS_DISABLED = win32_util.MFS_DISABLED
        MFS_ENABLED = win32_util.MFS_ENABLED
    except Exception:
        return
    # Not always exported by older pystray builds.
    MFS_HIDDEN = getattr(win32_util, "MFS_HIDDEN", 0x00000008)

    def _read(hmenu_local, index: int) -> tuple[str | None, Any]:
        info = MENUITEMINFO()
        info.cbSize = ctypes.sizeof(MENUITEMINFO)
        info.fMask = MIIM_STRING | MIIM_FTYPE
        info.dwTypeData = None
        info.cch = 0
        if not GetMenuItemInfoW(hmenu_local, index, True, ctypes.byref(info)):
            return None, None
        if info.fType & MFT_SEPARATOR:
            return None, None
        info.cch = max(int(info.cch), 0) + 1
        buf = ctypes.create_unicode_buffer(info.cch)
        info.fMask = MIIM_STRING
        info.dwTypeData = ctypes.cast(buf, wintypes.LPWSTR)
        info.cch = len(buf)
        if not GetMenuItemInfoW(hmenu_local, index, True, ctypes.byref(info)):
            return None, None
        return buf.value, GetSubMenu(hmenu_local, index)

    def _write(hmenu_local, index: int, title: str) -> None:
        info = MENUITEMINFO()
        info.cbSize = ctypes.sizeof(MENUITEMINFO)
        info.fMask = MIIM_STRING
        info.dwTypeData = title
        SetMenuItemInfoW(hmenu_local, index, True, ctypes.byref(info))

    def _patch_rows(
        hsubmenu, rows: list
    ) -> None:
        if not hsubmenu or not rows:
            return
        n = int(GetMenuItemCount(hsubmenu))
        for i, row in enumerate(rows):
            if i >= n:
                break
            text, _sub = _read(hsubmenu, i)
            if text is None:
                continue
            title, enabled, visible = _parse_live_row(row)
            _write(hsubmenu, i, title)
            if enabled is None and visible is not False:
                continue
            info = MENUITEMINFO()
            info.cbSize = ctypes.sizeof(MENUITEMINFO)
            info.fMask = MIIM_STATE
            state = 0
            if enabled is False:
                state |= MFS_DISABLED
            elif enabled is True:
                state |= MFS_ENABLED
            if visible is False:
                state |= MFS_HIDDEN
            info.fState = state
            SetMenuItemInfoW(hsubmenu, i, True, ctypes.byref(info))

    count = int(GetMenuItemCount(hmenu))
    for i in range(count):
        text, hsub = _read(hmenu, i)
        if text is None:
            continue
        for prefix, title in top_level_prefixes:
            if text.startswith(prefix):
                _write(hmenu, i, title)
                break
        for prefix, rows in submenu_by_parent_prefix.items():
            if text.startswith(prefix):
                _patch_rows(hsub, rows)
                break
        for exact, rows in submenu_by_parent_exact.items():
            if text == exact:
                _patch_rows(hsub, rows)
                break


# --- Linux GTK / AppIndicator ----------------------------------------------

def _install_linux() -> bool:
    gtk_ok = _install_gtk_statusicon()
    app_ok = _install_appindicator_show()
    return gtk_ok or app_ok


def _install_gtk_statusicon() -> bool:
    global _active_icon
    try:
        from pystray import _gtk
    except Exception:
        return False

    orig = _gtk.Icon._on_status_icon_popup_menu

    def _patched(self, status_icon, button, activate_time):
        global _active_icon
        _active_icon = self
        _fire_will_open()
        try:
            return orig(self, status_icon, button, activate_time)
        finally:
            _fire_did_close()
            _active_icon = None

    _gtk.Icon._on_status_icon_popup_menu = _patched
    return True


def _install_appindicator_show() -> bool:
    """Best-effort AppIndicator open hook via Gtk.Menu ``show`` / ``hide``."""
    global _active_icon
    try:
        from pystray._util.gtk import GtkIcon
    except Exception:
        return False

    orig_create = GtkIcon._create_menu

    def _patched_create(self, descriptors):
        global _active_icon
        menu = orig_create(self, descriptors)
        if menu is None:
            return None
        if getattr(menu, "_kg_open_hooks", False):
            return menu
        try:
            menu._kg_open_hooks = True

            def _on_show(_widget):
                global _active_icon
                _active_icon = self
                _fire_will_open()

            def _on_hide(_widget):
                global _active_icon
                _fire_did_close()
                _active_icon = None

            menu.connect("show", _on_show)
            menu.connect("hide", _on_hide)
        except Exception:
            pass
        return menu

    GtkIcon._create_menu = _patched_create
    return True


def _start_glib_tick() -> None:
    global _gtk_tick_source
    if _gtk_tick_source is not None:
        return
    try:
        from gi.repository import GLib
    except Exception:
        return

    def _cb():
        _fire_tick()
        return True

    try:
        _gtk_tick_source = GLib.timeout_add(_tick_interval_ms, _cb)
    except Exception:
        _gtk_tick_source = None


def _stop_glib_tick() -> None:
    global _gtk_tick_source
    source = _gtk_tick_source
    _gtk_tick_source = None
    if source is None:
        return
    try:
        from gi.repository import GLib
        GLib.source_remove(source)
    except Exception:
        pass


def _apply_live_titles_gtk(
    icon,
    top_level_prefixes: list[tuple[str, str]],
    submenu_by_parent_prefix: dict[str, list[str | tuple[str, bool]]],
    submenu_by_parent_exact: dict[str, list[str | tuple[str, bool]]],
) -> None:
    menu = getattr(icon, "_menu_handle", None)
    if menu is None:
        return
    try:
        children = list(menu.get_children())
    except Exception:
        return

    def _label_of(item) -> str:
        try:
            return str(item.get_label() or "")
        except Exception:
            return ""

    def _set_label(item, title: str) -> None:
        try:
            item.set_label(title)
        except Exception:
            pass

    def _patch_submenu(
        parent_item, rows: list
    ) -> None:
        if not rows:
            return
        try:
            sub = parent_item.get_submenu()
        except Exception:
            return
        if sub is None:
            return
        try:
            sub_children = list(sub.get_children())
        except Exception:
            return
        for i, row in enumerate(rows):
            if i >= len(sub_children):
                break
            child = sub_children[i]
            try:
                # Skip separators
                if type(child).__name__.endswith("SeparatorMenuItem"):
                    continue
            except Exception:
                pass
            title, enabled, visible = _parse_live_row(row)
            _set_label(child, title)
            if enabled is not None:
                try:
                    child.set_sensitive(bool(enabled))
                except Exception:
                    pass
            if visible is not None:
                try:
                    child.set_visible(bool(visible))
                except Exception:
                    try:
                        if visible:
                            child.show()
                        else:
                            child.hide()
                    except Exception:
                        pass

    for item in children:
        text = _label_of(item)
        if not text:
            continue
        for prefix, title in top_level_prefixes:
            if text.startswith(prefix):
                _set_label(item, title)
                break
        for prefix, rows in submenu_by_parent_prefix.items():
            if text.startswith(prefix):
                _patch_submenu(item, rows)
                break
        for exact, rows in submenu_by_parent_exact.items():
            if text == exact:
                _patch_submenu(item, rows)
                break
