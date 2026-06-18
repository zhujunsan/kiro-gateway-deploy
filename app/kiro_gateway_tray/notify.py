# app/kiro_gateway_tray/notify.py
"""Desktop notifications that adopt *this* app's identity.

Why this module exists
-----------------------
pystray's macOS backend (``pystray/_darwin.py``) sends notifications by shelling
out to ``osascript -e 'display notification ...'``. AppleScript runs under the
system "脚本编辑器 / Script Editor" host, so macOS attributes the banner to that
process and shows **its** icon — not ours. That is why the "Kiro Gateway Tray
错误" banners appeared with the Script Editor glyph.

On macOS we instead post the notification through ``NSUserNotificationCenter``
from inside our own process. When running as the packaged ``.app`` (which has a
bundle identifier and icon), the banner then adopts this app's icon and name.

Off macOS, or if the native path is unavailable for any reason, we fall back to
pystray's ``icon.notify()`` so behaviour is unchanged.
"""
from __future__ import annotations

import sys

from .log import logger

APP_NAME = "Kiro Gateway Tray"


def _notify_macos(title: str, message: str) -> bool:
    """Post via NSUserNotificationCenter. Returns True on success.

    Runs entirely in-process so the banner inherits our bundle identity/icon
    instead of Script Editor's. Deprecated by Apple in favour of the
    UserNotifications framework, but UNUserNotificationCenter requires a
    code-signed, authorized bundle; NSUserNotificationCenter works for our
    PyInstaller .app today without an authorization prompt.
    """
    try:
        from Foundation import NSUserNotification, NSUserNotificationCenter
    except Exception:
        return False
    try:
        center = NSUserNotificationCenter.defaultUserNotificationCenter()
        if center is None:
            # No bundle context (e.g. plain `python -m` run): nothing to post to.
            return False
        note = NSUserNotification.alloc().init()
        note.setTitle_(title)
        note.setInformativeText_(message)
        center.deliverNotification_(note)
        return True
    except Exception:
        logger.debug("NSUserNotification delivery failed", exc_info=True)
        return False


def notify(icon, title: str, message: str) -> None:
    """Show a desktop notification, preferring this app's own identity.

    ``icon`` is the pystray Icon used for the cross-platform fallback.
    """
    if sys.platform == "darwin" and _notify_macos(title, message):
        return
    try:
        icon.notify(message, title)
    except Exception:
        logger.debug("icon.notify fallback failed", exc_info=True)


__all__ = ["notify", "APP_NAME"]
