# app/kiro_tray/tray.py
"""System-tray / menu-bar UI via pystray."""
from __future__ import annotations

import sys
import threading
import webbrowser
from pathlib import Path

from . import appconfig, paths, usage
from .supervisor import Supervisor


class TrayUnavailable(RuntimeError):
    pass


# ----------------------------------------------------------------------------
# Menu-bar icon: a macOS "template image" negative-space silhouette. The source
# icon-source.png is a "black rounded square + white k→ glyph"; the black body
# (square minus glyph) is exactly the alpha shape we want — body opaque, glyph
# and corners transparent. With template mode on, the system tints it: white in
# dark menu bars (glyph knocked out to show through), inverted in light bars,
# auto-adapting to light/dark.
# Status is encoded by SHAPE (template drops color): bottom-right corner is a
# knocked-out solid dot when running, a hollow ring when stopped.
# Packaged (PyInstaller) assets come from sys._MEIPASS; from source they sit in
# app/resources/.
# ----------------------------------------------------------------------------
def _asset_path(name: str) -> Path:
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        for cand in (Path(meipass) / "resources" / name, Path(meipass) / name):
            if cand.exists():
                return cand
    return Path(__file__).resolve().parent.parent / "resources" / name


# Draw on a high-res canvas and let Cocoa scale to menu-bar size; combined with
# the Retina fix below this stays crisp.
_TRAY_RENDER = 256
# Transparent padding ratio: native menu-bar icons all have breathing room.
# Content occupies 75% -> padding 12.5%.
_TRAY_PAD = 0.125


def _load_silhouette():
    """Extract the negative-space silhouette alpha mask from icon-source.png.

    Black body -> opaque, white glyph / outside-corners -> transparent. i.e.
    alpha = inverted grayscale, cropped to the body bbox.
    """
    from PIL import Image

    src = _asset_path("icon-source.png")
    if not src.exists():
        src = _asset_path("icon.png")
    if not src.exists():
        return None
    try:
        gray = Image.open(src).convert("L")
    except Exception:
        return None
    alpha = gray.point(lambda p: 255 - p)  # black(0)->255 opaque; white(255)->0 transparent
    bbox = alpha.point(lambda p: 255 if p > 32 else 0).getbbox()
    if bbox:
        alpha = alpha.crop(bbox)
    return alpha


_SILHOUETTE = None
_SILHOUETTE_LOADED = False


def _silhouette():
    global _SILHOUETTE, _SILHOUETTE_LOADED
    if not _SILHOUETTE_LOADED:
        _SILHOUETTE = _load_silhouette()
        _SILHOUETTE_LOADED = True
    return _SILHOUETTE


def make_icon(running: bool):
    """Return the template negative-space silhouette (transparent + opaque black
    body). The system tints it automatically."""
    from PIL import Image, ImageChops, ImageDraw

    size = _TRAY_RENDER
    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))

    pad = int(size * _TRAY_PAD)
    box = size - 2 * pad
    sil = _silhouette()
    if sil is not None:
        alpha = sil.resize((box, box), Image.LANCZOS)
        body = Image.new("RGBA", (box, box), (0, 0, 0, 255))
        body.putalpha(alpha)
        canvas.paste(body, (pad, pad), body)
    else:
        ImageDraw.Draw(canvas).rounded_rectangle(
            (pad, pad, pad + box - 1, pad + box - 1),
            radius=int(box * 0.22),
            fill=(0, 0, 0, 255),
        )

    # Status shape in the bottom-right corner, knocked out of the silhouette
    # (alpha=0) so the tinted body shows the background through it.
    # running = knocked-out solid dot; stopped = knocked-out ring. Shape-coded
    # because template mode keeps no color.
    overlay = Image.new("L", (size, size), 0)
    od = ImageDraw.Draw(overlay)
    r = int(size * 0.24)
    x1 = size - pad
    y1 = size - pad
    dot = (x1 - r, y1 - r, x1, y1)
    if running:
        od.ellipse(dot, fill=255)  # solid = running
    else:
        ring = max(2, int(size * 0.05))
        od.ellipse(dot, outline=255, width=ring)  # hollow ring = stopped
    ca = canvas.getchannel("A")
    knocked = ImageChops.subtract(ca, overlay)
    canvas.putalpha(knocked)
    return canvas


def _install_retina_icon_fix() -> None:
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


def _local_url(cfg) -> str:
    return f"http://127.0.0.1:{cfg.gateway.port}/v1"


def _tunnel_url(cfg) -> str:
    if cfg.cloudflare.hostname:
        return f"https://{cfg.cloudflare.hostname}/v1"
    return ""


def _base_url(cfg) -> str:
    # 启动通知里优先报 tunnel 地址，没有就退回本地。
    return _tunnel_url(cfg) or _local_url(cfg)


def _ask_shared_secret(cfg) -> str:
    """Prompt user for shared secret. Tries Tk dialog, falls back to print."""
    try:
        import tkinter as tk
        from tkinter import simpledialog
        root = tk.Tk()
        root.withdraw()
        secret = simpledialog.askstring(
            "Kiro Tray - 首次激活",
            f"请输入激活码（共享密钥）\nWorker: {cfg.cloudflare.provision_url}",
            parent=root,
        )
        root.destroy()
        if not secret:
            raise RuntimeError("用户取消了激活。")
        return secret
    except ImportError:
        raise RuntimeError("无法弹出输入框，请改用 CLI 模式完成首次激活（kiro-tray --cli）。")


class _UsageCache:
    """Thread-safe cache for the /usage result, rendered by the menu line.

    State machine: None (never fetched) -> "loading" -> text | error.
    Each time the menu opens we kick a background refresh, but the line
    renders immediately from whatever is cached so the UI never blocks.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._text: str | None = None      # None = never fetched
        self._inflight = False

    def display(self) -> str:
        with self._lock:
            return self._text if self._text is not None else "加载中…"

    def refresh(self, icon) -> None:
        with self._lock:
            if self._inflight:
                return
            self._inflight = True
            if self._text is None:
                self._text = "加载中…"

        def _work():
            try:
                data = usage.fetch()
                text = usage.format_menu_line(data)   # e.g. "1732.9 / 1000"
            except Exception:
                text = "获取失败"
            with self._lock:
                self._text = text
                self._inflight = False
            try:
                icon.update_menu()                    # re-render with fresh value
            except Exception:
                pass

        threading.Thread(target=_work, daemon=True).start()


def run() -> None:
    """Start the tray loop. Raises TrayUnavailable if no backend works."""
    try:
        import pystray
    except Exception as e:
        raise TrayUnavailable(str(e))

    sup = Supervisor()
    sup.provision_callback = _ask_shared_secret
    usage_cache = _UsageCache()

    def _notify(icon, title, msg):
        try:
            icon.notify(msg, title)
        except Exception:
            pass

    def _refresh_icon(icon):
        # Swap the menu-bar glyph to match running/stopped state.
        try:
            icon.icon = make_icon(sup.status()["gateway"] == "running")
        except Exception:
            pass

    def on_start_or_restart(icon, _item):
        # Same menu slot: "启动" when stopped, "重启" when already running.
        restarting = sup.status()["gateway"] == "running"
        def _work():
            try:
                if restarting:
                    sup.restart()
                    verb = "已重启"
                else:
                    sup.start()
                    verb = "已启动"
                cfg = appconfig.load()
                _notify(icon, "Kiro Tray", f"{verb}\n{_tunnel_url(cfg)}")
            except Exception as e:
                _notify(icon, "Kiro Tray 错误", str(e)[:200])
            _refresh_icon(icon)
            icon.update_menu()
        threading.Thread(target=_work, daemon=True).start()

    def on_stop(icon, _item):
        sup.stop()
        _notify(icon, "Kiro Tray", "网关已停止")
        _refresh_icon(icon)
        icon.update_menu()

    def _copy(icon, value, label):
        try:
            import pyperclip
            pyperclip.copy(value)
            _notify(icon, "Kiro Tray", f"已复制{label}: {value}")
        except Exception:
            _notify(icon, label, value)

    def on_copy_local_url(icon, _item):
        cfg = appconfig.load()
        _copy(icon, _local_url(cfg), "本地 URL")

    def on_copy_tunnel_url(icon, _item):
        cfg = appconfig.load()
        _copy(icon, _tunnel_url(cfg), "Tunnel URL")

    def on_copy_password(icon, _item):
        cfg = appconfig.load()
        _copy(icon, cfg.gateway.proxy_api_key, "Gateway 密码")

    def on_open_config(_icon, _item):
        webbrowser.open(paths.config_file().as_uri())

    def on_open_logs(_icon, _item):
        webbrowser.open(paths.log_dir().as_uri())

    def on_quit(icon, _item):
        sup.stop()
        icon.stop()

    # --- status line text callables (re-evaluated each time menu shows) ---
    def gateway_line(_item):
        s = sup.status()
        return f"网关　本地 kiro gateway　[{s['gateway']}]"

    def tunnel_line(_item):
        s = sup.status()
        return f"隧道　cloudflare tunnel　[{s['tunnel']}]"

    # The usage line's text callable doubles as the refresh trigger: pystray
    # re-evaluates every item's text each time the menu is opened, so reading
    # the line kicks a background refresh. display() returns the cached value
    # instantly (or "加载中…"), and the background thread calls
    # icon.update_menu() when fresh data arrives.
    def usage_line(_item):
        usage_cache.refresh(icon)
        return f"额度　{usage_cache.display()}"

    # Start/restart is one menu item with dynamic text: shows "重启" when the
    # gateway is already running (calls sup.restart()), "启动" otherwise.
    def start_line(_item):
        return "重启" if sup.status()["gateway"] == "running" else "启动"

    # --- update notice (Task 13): only shown when a newer release exists ---
    # _update_info is filled by a background check kicked off at startup
    # (see updates.check below). Default None = nothing to show.
    _update = {"info": None}

    def _kick_update_check():
        def _work():
            try:
                from . import updates
                info = updates.check()
                if info.update_available:
                    _update["info"] = info
                    icon.update_menu()
            except Exception:
                pass  # silent failure, never bother the user
        threading.Thread(target=_work, daemon=True).start()

    def update_visible(_item) -> bool:
        return _update["info"] is not None

    def update_line(_item) -> str:
        info = _update["info"]
        return f"🔔 有新版本 {info.latest}，点击下载" if info else ""

    def on_update(icon, _item):
        info = _update["info"]
        if info:
            webbrowser.open(info.release_url)

    menu = pystray.Menu(
        # Update notice goes first; the separator below it auto-collapses when
        # the notice is hidden (pystray drops leading/adjacent separators).
        pystray.MenuItem(update_line, on_update, visible=update_visible),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(gateway_line, None, enabled=False),
        pystray.MenuItem(tunnel_line, None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(usage_line, None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("打开配置文件", on_open_config),
        pystray.MenuItem("打开日志目录", on_open_logs),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("复制本地 URL", on_copy_local_url),
        pystray.MenuItem("复制 Tunnel URL", on_copy_tunnel_url),
        pystray.MenuItem("复制 Gateway 密码", on_copy_password),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(start_line, on_start_or_restart),
        pystray.MenuItem("停止", on_stop),
        pystray.MenuItem("退出", on_quit),
    )

    def _startup():
        # Bring the gateway + tunnel up in the background, then sync the icon
        # to whatever state we ended in (running on success, stopped on error).
        try:
            sup.start()
        except Exception as e:
            _notify(icon, "Kiro Tray 错误", str(e)[:200])
        _refresh_icon(icon)
        icon.update_menu()

    icon = pystray.Icon("kiro-tray", make_icon(False), "Kiro Gateway", menu)
    _install_retina_icon_fix()  # macOS only; sharp menu-bar glyph + template tint
    threading.Thread(target=_startup, daemon=True).start()
    _kick_update_check()        # startup check; updates.check() handles 24h caching
    icon.run()
