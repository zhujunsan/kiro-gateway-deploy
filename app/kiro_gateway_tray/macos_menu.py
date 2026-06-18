# app/kiro_gateway_tray/macos_menu.py
"""macOS-only cosmetic patches for pystray's Cocoa backend.

Both functions are no-ops off macOS and swallow import errors, so callers can
invoke them unconditionally. They monkey-patch ``pystray._darwin.Icon`` to:
  - right-align a smaller gray "tag" after a ``\\t`` in menu item titles
  - render the menu-bar glyph at Retina resolution so it isn't blurry
"""
from __future__ import annotations

import sys


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


def install_menu_gray_suffix() -> None:
    """Post-process each NSMenu after it's built. For every item whose title
    contains ``\\t``:

    1. Measure the left-side text width of ALL tab items in the same menu.
    2. Set a single right-aligned tab stop at ``max_left_width + pad`` so all
       right-side tags line up, adapting to the actual content width.
    3. Style the right part as smaller gray text, with a background badge on
       clickable items.

    Runs per-menu (including submenus), so the main menu and the models submenu
    each get their own optimal width.
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
    _TAG_GAP = 16.0

    def _ns_len(s):
        return Foundation.NSString.stringWithString_(s).length()

    def _text_width(s, font):
        ns = Foundation.NSString.stringWithString_(s)
        attrs = Foundation.NSDictionary.dictionaryWithObject_forKey_(
            font, AppKit.NSFontAttributeName
        )
        return ns.sizeWithAttributes_(attrs).width

    def _patched_create_menu(self, descriptors, callbacks):
        nsmenu = _orig_create_menu(self, descriptors, callbacks)
        if nsmenu is None:
            return None

        menu_font = AppKit.NSFont.menuFontOfSize_(0)
        font_size = menu_font.pointSize()
        small_font = AppKit.NSFont.menuFontOfSize_(font_size - 2)
        gray = AppKit.NSColor.secondaryLabelColor()
        bg = AppKit.NSColor.colorWithCalibratedWhite_alpha_(0.5, 0.15)

        tab_items = []
        max_total_w = 0.0

        for i in range(nsmenu.numberOfItems()):
            item = nsmenu.itemAtIndex_(i)
            if item.isSeparatorItem():
                continue
            title = str(item.title() or "")
            if "\t" not in title:
                continue
            left, right = title.split("\t", 1)
            is_badge = right.strip() == "复制"
            padded_right = f"   {right}   " if is_badge else right
            left_w = _text_width(left, menu_font)
            right_w = _text_width(padded_right, small_font)
            total_w = left_w + _TAG_GAP + right_w
            if total_w > max_total_w:
                max_total_w = total_w
            tab_items.append((item, left, right))

        if not tab_items:
            return nsmenu

        tab_pos = max_total_w

        for item, left, right in tab_items:
            is_badge = right.strip() == "复制"
            if is_badge:
                right = f"   {right}   "
            composed = left + "\t" + right

            para = AppKit.NSMutableParagraphStyle.alloc().init()
            tab = AppKit.NSTextTab.alloc().initWithType_location_(
                AppKit.NSRightTabStopType, tab_pos
            )
            para.setTabStops_([tab])

            attr = Foundation.NSMutableAttributedString.alloc().initWithString_(
                composed
            )
            full_range = Foundation.NSRange(0, attr.length())
            attr.addAttribute_value_range_(
                AppKit.NSFontAttributeName, menu_font, full_range
            )
            attr.addAttribute_value_range_(
                AppKit.NSParagraphStyleAttributeName, para, full_range
            )

            ns_left_len = _ns_len(left)
            ns_right_len = _ns_len(right)
            right_start = ns_left_len + 1
            right_range = Foundation.NSRange(right_start, ns_right_len)

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

        return nsmenu

    _darwin.Icon._create_menu = _patched_create_menu


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
