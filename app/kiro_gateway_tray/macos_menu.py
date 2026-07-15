# app/kiro_gateway_tray/macos_menu.py
"""macOS-only cosmetic / live-update patches for pystray's Cocoa backend.

All public helpers are no-ops off macOS and swallow import errors, so callers
can invoke them unconditionally. They monkey-patch ``pystray._darwin.Icon`` to:

  - right-align a smaller gray "tag" after a ``\\t`` in menu item titles
  - render the menu-bar glyph at Retina resolution so it isn't blurry
  - attach an ``NSMenuDelegate`` so the tray can refresh titles while open
    *without* calling ``setMenu:`` (which breaks AppKit menu tracking)
"""
from __future__ import annotations

import sys
from typing import Any, Callable

# Set True between menuWillOpen: and menuDidClose: for the status-item root menu.
# Prefer this over button.isHighlighted() when deciding whether setMenu: is safe.
_status_menu_session_open = False

# Gap between the left label and the right-aligned gray tag (points).
_TAG_GAP = 16.0

_LiveWillOpen = Callable[[Any], None]
_LiveDidClose = Callable[[Any], None]
_LiveTick = Callable[[], None]

_live_hooks: dict[str, Any] = {
    "will_open": None,
    "did_close": None,
    "tick": None,
}


def run_on_main_thread(fn) -> None:
    """Marshal ``fn`` onto the macOS main thread.

    AppKit (NSMenu, NSStatusItem) must only be touched from the main thread;
    calling it from a worker thread can hard-crash the process with no Python
    traceback. Off macOS, or if the bridge is unavailable, run ``fn`` inline.
    """
    if sys.platform != "darwin":
        fn()
        return
    try:
        from Foundation import NSOperationQueue
    except Exception:
        fn()
        return
    NSOperationQueue.mainQueue().addOperationWithBlock_(fn)


def install_reopen_handler(icon, on_reopen: Callable[[], None]) -> None:
    """Handle Finder/LaunchServices attempts to open an already-running app.

    macOS normally reuses the existing application process instead of launching
    a second executable, so the file-based single-instance lock is never
    reached. ``applicationShouldHandleReopen:hasVisibleWindows:`` is the event
    delivered to the existing process for that case.
    """
    if sys.platform != "darwin" or icon is None:
        return
    try:
        import AppKit
        import objc
    except Exception:
        return

    class _ReopenDelegate(AppKit.NSObject):
        def applicationShouldHandleReopen_hasVisibleWindows_(
            self, _application, _has_visible_windows
        ):
            try:
                on_reopen()
            except Exception:
                pass
            return False

    try:
        delegate = _ReopenDelegate.alloc().init()
        app = getattr(icon, "_app", None) or AppKit.NSApplication.sharedApplication()
        app.setDelegate_(delegate)
        # NSApplication's delegate is not retained. Keep it alive for the same
        # lifetime as pystray's icon.
        icon._kg_reopen_delegate = delegate
    except Exception:
        return


def is_status_menu_open(icon) -> bool:
    """True while the user has the tray status-item menu pulled down.

    Replacing the menu via ``NSStatusItem.setMenu:`` (pystray ``update_menu``)
    during AppKit menu tracking breaks the tracking session: the menu freezes
    and keyboard events can appear system-wide "hijacked" until the tray app
    exits. Off macOS, or if the status item isn't available, return False.
    """
    if _status_menu_session_open:
        return True
    if sys.platform != "darwin" or icon is None:
        return False
    try:
        status_item = getattr(icon, "_status_item", None)
        if status_item is None:
            return False
        # pystray's real NSStatusItem; ignore plain mocks without a real button.
        button = status_item.button()
        if button is None:
            return False
        highlighted = button.isHighlighted()
        # AppKit returns a real bool; refuse MagicsMock / other truthy junk.
        return highlighted is True or highlighted == 1
    except Exception:
        return False


def find_menu_item_by_title_prefix(nsmenu, prefix: str):
    """Return the first ``NSMenuItem`` whose title starts with ``prefix``, or None."""
    if nsmenu is None:
        return None
    try:
        for i in range(int(nsmenu.numberOfItems())):
            item = nsmenu.itemAtIndex_(i)
            if item is None or item.isSeparatorItem():
                continue
            title = str(item.title() or "")
            if title.startswith(prefix):
                return item
    except Exception:
        return None
    return None


def find_menu_item_by_exact_title(nsmenu, title: str):
    if nsmenu is None:
        return None
    try:
        for i in range(int(nsmenu.numberOfItems())):
            item = nsmenu.itemAtIndex_(i)
            if item is None or item.isSeparatorItem():
                continue
            if str(item.title() or "") == title:
                return item
    except Exception:
        return None
    return None


def apply_menu_item_title(item, title: str) -> None:
    """Set an item title, re-applying macOS attributed styles when needed.

    Safe to call while the menu is open (unlike ``setMenu:`` / ``update_menu``).

    - Titles with ``\\t`` get the right-aligned gray suffix treatment.
      When the item already belongs to an ``NSMenu``, all ``\\t`` siblings are
      re-aligned to a *shared* trailing tab stop (same as initial menu build).
      Per-item tab stops would leave status text mid-row after live patches.
    - Titles with ``\\n`` get a real multi-line attributed title (plain
      ``setTitle:`` collapses newlines on AppKit menus).
    """
    if item is None:
        return
    title = title or ""
    if sys.platform != "darwin":
        try:
            item.setTitle_(title)
        except Exception:
            pass
        return
    try:
        import AppKit
        import Foundation
    except Exception:
        try:
            item.setTitle_(title)
        except Exception:
            pass
        return
    try:
        if "\n" in title:
            _apply_multiline_attributed_title(item, title, AppKit, Foundation)
        elif "\t" in title:
            # Prefer menu-wide shared tab stops so live updates match the
            # initial install_menu_gray_suffix layout (label left, tag right).
            menu = None
            try:
                menu = item.menu()
            except Exception:
                menu = None
            if menu is not None:
                item.setTitle_(title)
                realign_menu_tab_suffixes(menu)
            else:
                _apply_tab_attributed_title(item, title, AppKit, Foundation)
        else:
            # Plain titles must clear any prior attributedTitle; otherwise a
            # leftover tab-styled attributed string keeps rendering (e.g. a
            # finished 进行中 row stuck on 「生成中」 after switching to idle).
            try:
                item.setAttributedTitle_(None)
            except Exception:
                pass
            item.setTitle_(title)
    except Exception:
        try:
            try:
                item.setAttributedTitle_(None)
            except Exception:
                pass
            item.setTitle_(title)
        except Exception:
            pass


def attach_submenu_autorebuild(submenu, rebuild_cb) -> Any:
    """Attach an ``NSMenuDelegate`` that refills ``submenu`` on each expand.

    AppKit calls ``menuNeedsUpdate:`` right before a submenu is displayed. That
    is the reliable hook to rebuild dynamic rows: structural edits made while a
    submenu is *already* displayed do not re-render, but a fresh
    ``menuNeedsUpdate:`` rebuild before each expand always shows current data.

    Returns the delegate (kept alive by the caller) or None off macOS.
    """
    if submenu is None:
        return None
    if sys.platform != "darwin":
        # Tests: invoke immediately so non-AppKit doubles still get filled.
        try:
            rebuild_cb(submenu)
        except Exception:
            pass
        return None
    try:
        import AppKit
        import objc
    except Exception:
        return None

    class _SubmenuAutoRebuild(AppKit.NSObject):
        def init(self):
            self = objc.super(_SubmenuAutoRebuild, self).init()
            if self is None:
                return None
            self._cb = None
            return self

        def setCallback_(self, cb):
            self._cb = cb

        def menuNeedsUpdate_(self, menu):
            if self._cb is None:
                return
            try:
                self._cb(menu)
            except Exception:
                pass

    try:
        delegate = _SubmenuAutoRebuild.alloc().init()
        delegate.setCallback_(rebuild_cb)
        submenu.setDelegate_(delegate)
        return delegate
    except Exception:
        return None


def make_live_click_delegate():
    """Return an NSObject that routes live-rebuilt submenu clicks, or None.

    pystray's callback list is frozen at ``update_menu`` time. Submenus rebuilt
    while the status menu is open must use a separate target/action pair so
    clicks (e.g. copy recent conversation) still work.
    """
    if sys.platform != "darwin":
        return None
    try:
        import AppKit
        import objc
    except Exception:
        return None

    class _LiveClickDelegate(AppKit.NSObject):
        def init(self):
            self = objc.super(_LiveClickDelegate, self).init()
            if self is None:
                return None
            self._handlers: dict[int, Callable[[], None]] = {}
            return self

        def clear(self) -> None:
            self._handlers.clear()

        def setHandler_forTag_(self, handler, tag) -> None:
            if handler is None:
                self._handlers.pop(int(tag), None)
            else:
                self._handlers[int(tag)] = handler

        def activateLiveItem_(self, sender) -> None:
            handler = self._handlers.get(int(sender.tag()))
            if handler is None:
                return
            try:
                handler()
            except Exception:
                pass

    try:
        return _LiveClickDelegate.alloc().init()
    except Exception:
        return None


def replace_submenu_rows(submenu, rows, click_delegate=None) -> None:
    """Replace all items in ``submenu`` in place (safe while the menu is open).

    ``rows`` is an iterable of ``(title, enabled, on_click_or_None)``.
    Clickable rows require a ``click_delegate`` from ``make_live_click_delegate``.

    Works with real ``NSMenu`` and with test doubles that expose
    ``removeAllItems`` / ``addItem_``.
    """
    if submenu is None:
        return
    try:
        submenu.removeAllItems()
    except Exception:
        return
    if click_delegate is not None:
        try:
            click_delegate.clear()
        except Exception:
            pass

    appkit = None
    if sys.platform == "darwin":
        try:
            import AppKit as appkit
        except Exception:
            appkit = None

    for i, (title, enabled, on_click) in enumerate(rows):
        try:
            if appkit is not None:
                item = appkit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                    title or "", None, ""
                )
            else:
                item = _StubMenuItem(title or "", enabled=bool(enabled))
            item.setEnabled_(bool(enabled))
            if on_click is not None and click_delegate is not None:
                try:
                    item.setTarget_(click_delegate)
                    item.setAction_(b"activateLiveItem:")
                    item.setTag_(i)
                except Exception:
                    pass
                click_delegate.setHandler_forTag_(on_click, i)
            apply_menu_item_title(item, title or "")
            submenu.addItem_(item)
        except Exception:
            continue
    # Items are attributed before addItem_ (menu() is nil), so finish with a
    # shared trailing tab stop across the whole submenu.
    realign_menu_tab_suffixes(submenu)


class _StubMenuItem:
    """Minimal menu-item stand-in for non-AppKit unit tests."""

    def __init__(self, title: str, *, enabled: bool = True):
        self._title = title
        self._enabled = enabled
        self._target = None
        self._action = None
        self._tag = 0

    def title(self):
        return self._title

    def setTitle_(self, title):
        self._title = title

    def setAttributedTitle_(self, _attr):
        pass

    def isSeparatorItem(self):
        return False

    def isEnabled(self):
        return self._enabled

    def setEnabled_(self, enabled):
        self._enabled = bool(enabled)

    def setTarget_(self, target):
        self._target = target

    def setAction_(self, action):
        self._action = action

    def setTag_(self, tag):
        self._tag = int(tag)

    def tag(self):
        return self._tag

    def submenu(self):
        return None


def _apply_multiline_attributed_title(item, title: str, AppKit, Foundation) -> None:
    """Render an explicit multi-line menu title via attributed string.

    AppKit's plain ``setTitle:`` ignores/collapses ``\\n``. Using
    ``setAttributedTitle:`` with a paragraph style keeps the line breaks and
    grows the menu row height. Line 0 stays the primary menu font; following
    lines are smaller secondary (gray) text for 问/答 previews.
    """
    menu_font = AppKit.NSFont.menuFontOfSize_(0)
    font_size = menu_font.pointSize()
    small_font = AppKit.NSFont.menuFontOfSize_(max(10.0, font_size - 2))
    gray = AppKit.NSColor.secondaryLabelColor()

    para = AppKit.NSMutableParagraphStyle.alloc().init()
    para.setLineBreakMode_(AppKit.NSLineBreakByTruncatingTail)
    # Slightly tighter than default so 3-line recent items stay compact.
    para.setLineSpacing_(1.0)
    para.setParagraphSpacing_(1.0)

    attr = Foundation.NSMutableAttributedString.alloc().initWithString_(title)
    full_range = Foundation.NSRange(0, attr.length())
    attr.addAttribute_value_range_(AppKit.NSFontAttributeName, menu_font, full_range)
    attr.addAttribute_value_range_(
        AppKit.NSParagraphStyleAttributeName, para, full_range
    )

    # Style everything after the first newline as secondary preview text.
    first_nl = title.find("\n")
    if first_nl >= 0:
        # NSString length matches UTF-16 length used by NSRange.
        ns_first = Foundation.NSString.stringWithString_(title[: first_nl + 1]).length()
        rest_len = attr.length() - ns_first
        if rest_len > 0:
            rest_range = Foundation.NSRange(ns_first, rest_len)
            attr.addAttribute_value_range_(
                AppKit.NSFontAttributeName, small_font, rest_range
            )
            attr.addAttribute_value_range_(
                AppKit.NSForegroundColorAttributeName, gray, rest_range
            )

    item.setAttributedTitle_(attr)


def _pad_tab_right(right: str) -> str:
    """Pad the ``复制`` badge so its background reads as a chip.

    Idempotent: titles that already include the padding (e.g. after a prior
    attributed apply) are not padded again on realign.
    """
    stripped = right.strip()
    if stripped == "复制":
        return f"   {stripped}   "
    return right


def shared_right_tab_pos(entries: list[tuple[float, float]], *, gap: float = _TAG_GAP) -> float:
    """Return a shared NSRightTabStop location for ``(left_w, right_w)`` rows.

    All trailing tags align to this x so short statuses like「运行中」sit at the
    same trailing edge as wider tags like「✓ 已开启」/「已是最新」/「复制」.
    """
    if not entries:
        return 0.0
    return max(left_w + gap + right_w for left_w, right_w in entries)


def _tab_entry_widths(
    left: str,
    right: str,
    menu_font,
    small_font,
    AppKit,
    Foundation,
) -> tuple[float, float, str]:
    padded = _pad_tab_right(right)
    left_w = _text_width(left, menu_font, AppKit, Foundation)
    right_w = _text_width(padded, small_font, AppKit, Foundation)
    return left_w, right_w, padded


def realign_menu_tab_suffixes(nsmenu) -> None:
    """Apply one shared right-aligned tab stop to every ``\\t`` title in ``nsmenu``.

    Matches the layout produced by ``install_menu_gray_suffix`` so live title
    patches (gateway/tunnel/进行中) stay trailing-aligned instead of mid-row.
    No-op off macOS or when AppKit is unavailable.
    """
    if nsmenu is None or sys.platform != "darwin":
        return
    try:
        import AppKit
        import Foundation
    except Exception:
        return

    menu_font = AppKit.NSFont.menuFontOfSize_(0)
    font_size = menu_font.pointSize()
    small_font = AppKit.NSFont.menuFontOfSize_(font_size - 2)

    tab_items: list[tuple[Any, str, str]] = []
    widths: list[tuple[float, float]] = []
    try:
        n = int(nsmenu.numberOfItems())
    except Exception:
        return

    for i in range(n):
        try:
            item = nsmenu.itemAtIndex_(i)
        except Exception:
            continue
        if item is None:
            continue
        try:
            if item.isSeparatorItem():
                continue
        except Exception:
            pass
        title = str(item.title() or "")
        if "\t" not in title or "\n" in title:
            continue
        left, right = title.split("\t", 1)
        left_w, right_w, _padded = _tab_entry_widths(
            left, right, menu_font, small_font, AppKit, Foundation
        )
        widths.append((left_w, right_w))
        tab_items.append((item, left, right))

    if not tab_items:
        return

    tab_pos = shared_right_tab_pos(widths)
    for item, left, right in tab_items:
        try:
            _apply_tab_attributed_title(
                item, left + "\t" + right, AppKit, Foundation, tab_pos=tab_pos
            )
        except Exception:
            continue


def _apply_tab_attributed_title(
    item, title: str, AppKit, Foundation, *, tab_pos: float | None = None
) -> None:
    left, raw_right = title.split("\t", 1)
    menu_font = AppKit.NSFont.menuFontOfSize_(0)
    font_size = menu_font.pointSize()
    small_font = AppKit.NSFont.menuFontOfSize_(font_size - 2)
    gray = AppKit.NSColor.secondaryLabelColor()
    bg = AppKit.NSColor.colorWithCalibratedWhite_alpha_(0.5, 0.15)
    is_badge = raw_right.strip() == "复制"
    left_w, right_w, right = _tab_entry_widths(
        left, raw_right, menu_font, small_font, AppKit, Foundation
    )
    composed = left + "\t" + right

    # When called without a shared tab_pos (item not yet in a menu), fall back
    # to this row's natural width. Prefer realign_menu_tab_suffixes once the
    # item is attached so short tags do not sit mid-row.
    if tab_pos is None:
        tab_pos = shared_right_tab_pos([(left_w, right_w)])

    para = AppKit.NSMutableParagraphStyle.alloc().init()
    tab = AppKit.NSTextTab.alloc().initWithType_location_(
        AppKit.NSRightTabStopType, tab_pos
    )
    para.setTabStops_([tab])

    attr = Foundation.NSMutableAttributedString.alloc().initWithString_(composed)
    full_range = Foundation.NSRange(0, attr.length())
    attr.addAttribute_value_range_(AppKit.NSFontAttributeName, menu_font, full_range)
    attr.addAttribute_value_range_(
        AppKit.NSParagraphStyleAttributeName, para, full_range
    )

    ns_left_len = Foundation.NSString.stringWithString_(left).length()
    ns_right_len = Foundation.NSString.stringWithString_(right).length()
    right_range = Foundation.NSRange(ns_left_len + 1, ns_right_len)
    attr.addAttribute_value_range_(
        AppKit.NSForegroundColorAttributeName, gray, right_range
    )
    attr.addAttribute_value_range_(
        AppKit.NSFontAttributeName, small_font, right_range
    )
    if is_badge:
        attr.addAttribute_value_range_(
            AppKit.NSBackgroundColorAttributeName, bg, right_range
        )
    item.setAttributedTitle_(attr)


def _text_width(s, font, AppKit, Foundation) -> float:
    ns = Foundation.NSString.stringWithString_(s)
    attrs = Foundation.NSDictionary.dictionaryWithObject_forKey_(
        font, AppKit.NSFontAttributeName
    )
    return ns.sizeWithAttributes_(attrs).width


def install_menu_gray_suffix() -> None:
    """Post-process each NSMenu after it's built.

    - Titles with ``\\t``: right-align a smaller gray tag (and badge ``复制``).
    - Titles with ``\\n``: apply multi-line attributed titles so AppKit actually
      shows more than one row (plain ``setTitle:`` collapses newlines).

    Runs per-menu (including submenus), so the main menu and the models /
    recent-conversation submenus each get their own treatment.
    """
    if sys.platform != "darwin":
        return
    try:
        import AppKit
        import Foundation
        from pystray import _darwin
    except Exception:
        return

    _orig_create_menu = _darwin.Icon._create_menu

    def _patched_create_menu(self, descriptors, callbacks):
        nsmenu = _orig_create_menu(self, descriptors, callbacks)
        if nsmenu is None:
            return None

        for i in range(nsmenu.numberOfItems()):
            item = nsmenu.itemAtIndex_(i)
            if item.isSeparatorItem():
                continue
            title = str(item.title() or "")
            if "\n" in title:
                try:
                    _apply_multiline_attributed_title(item, title, AppKit, Foundation)
                except Exception:
                    pass

        realign_menu_tab_suffixes(nsmenu)
        return nsmenu

    _darwin.Icon._create_menu = _patched_create_menu


def install_live_status_menu(
    *,
    on_will_open: _LiveWillOpen | None = None,
    on_did_close: _LiveDidClose | None = None,
    on_tick: _LiveTick | None = None,
    tick_interval: float = 1.0,
) -> None:
    """Attach an NSMenuDelegate so titles can refresh while the menu is open.

    - ``menuWillOpen:`` / ``menuDidClose:`` drive tray callbacks.
    - A 1s timer in ``NSRunLoopCommonModes`` keeps firing during menu tracking
      (default-mode timers do not), so elapsed-time labels can tick via
      ``setTitle:`` without ``setMenu:``.
    """
    if sys.platform != "darwin":
        return
    try:
        import AppKit
        import Foundation
        import objc
        from pystray import _darwin
    except Exception:
        return

    _live_hooks["will_open"] = on_will_open
    _live_hooks["did_close"] = on_did_close
    _live_hooks["tick"] = on_tick
    interval = max(0.25, float(tick_interval))

    class _StatusMenuLiveDelegate(AppKit.NSObject):
        def init(self):
            self = objc.super(_StatusMenuLiveDelegate, self).init()
            if self is None:
                return None
            self._timer = None
            return self

        def menuWillOpen_(self, menu):
            global _status_menu_session_open
            _status_menu_session_open = True
            cb = _live_hooks.get("will_open")
            if cb is not None:
                try:
                    cb(menu)
                except Exception:
                    pass
            self._start_timer()

        def menuDidClose_(self, menu):
            global _status_menu_session_open
            self._stop_timer()
            _status_menu_session_open = False
            cb = _live_hooks.get("did_close")
            if cb is not None:
                try:
                    cb(menu)
                except Exception:
                    pass

        def liveTick_(self, _timer):
            cb = _live_hooks.get("tick")
            if cb is not None:
                try:
                    cb()
                except Exception:
                    pass

        def _start_timer(self):
            self._stop_timer()
            # CommonModes: must fire during NSEventTrackingRunLoopMode (menu open).
            timer = Foundation.NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(
                interval, self, b"liveTick:", None, True
            )
            Foundation.NSRunLoop.currentRunLoop().addTimer_forMode_(
                timer, Foundation.NSRunLoopCommonModes
            )
            self._timer = timer

        def _stop_timer(self):
            timer = self._timer
            self._timer = None
            if timer is not None:
                try:
                    timer.invalidate()
                except Exception:
                    pass

    _orig_update_menu = _darwin.Icon._update_menu

    def _patched_update_menu(self):
        _orig_update_menu(self)
        handle = getattr(self, "_menu_handle", None)
        if not handle:
            return
        nsmenu = handle[0]
        if nsmenu is None:
            return
        delegate = getattr(self, "_kg_live_menu_delegate", None)
        if delegate is None:
            delegate = _StatusMenuLiveDelegate.alloc().init()
            self._kg_live_menu_delegate = delegate
        try:
            nsmenu.setDelegate_(delegate)
        except Exception:
            pass

    _darwin.Icon._update_menu = _patched_update_menu


def install_retina_icon_fix() -> None:
    """Fix pystray's macOS backend Retina blur.

    Its _assert_image scales the image to "menu-bar thickness" pixels (~22px)
    then builds an NSImage, which defaults to 1px=1pt, so on 2x screens it is
    upscaled to fill 44px and goes blurry. Here we render a 44px bitmap per the
    backingScaleFactor and setSize_ to 22pt, so the system treats it as a Retina
    asset and draws it sharp. macOS only.
    """
    if sys.platform != "darwin":
        return
    try:
        import io

        import AppKit
        import Foundation
        import PIL.Image
        from pystray import _darwin
    except Exception:
        return

    def _assert_image(self) -> None:  # noqa: ANN001
        thickness = self._status_bar.thickness()
        pts = int(thickness)
        try:
            scale = AppKit.NSScreen.mainScreen().backingScaleFactor() or 1.0
        except Exception:
            scale = 2.0
        px = max(1, int(round(thickness * scale)))

        if self._icon_image and tuple(self._icon_image.size()) == (pts, pts):
            return

        source = self._icon
        if source.size != (px, px):
            source = source.resize((px, px), PIL.Image.LANCZOS)

        b = io.BytesIO()
        source.save(b, "png")
        image = AppKit.NSImage.alloc().initWithData_(Foundation.NSData(b.getvalue()))
        image.setSize_((pts, pts))
        # template image: system tints per light/dark menu bar (negative-space
        # silhouette), matching the design.
        image.setTemplate_(True)
        self._icon_image = image
        self._status_item.button().setImage_(image)

    _darwin.Icon._assert_image = _assert_image
