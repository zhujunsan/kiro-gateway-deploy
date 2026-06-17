# app/kiro_gateway_tray/tray.py
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
    # running = checkmark ✓; stopped = cross ✗. Shape-coded because template
    # mode keeps no color.
    overlay = Image.new("L", (size, size), 0)
    od = ImageDraw.Draw(overlay)
    r = int(size * 0.24)
    x1 = size - pad
    y1 = size - pad
    cx, cy = x1 - r // 2, y1 - r // 2
    sw = max(3, int(size * 0.06))  # stroke width
    if running:
        # checkmark: short leg down-right, long leg up-right
        od.line([(cx - r // 3, cy), (cx - r // 8, cy + r // 3)], fill=255, width=sw)
        od.line([(cx - r // 8, cy + r // 3), (cx + r // 3, cy - r // 4)], fill=255, width=sw)
    else:
        # cross: two diagonal lines
        half = r // 3
        od.line([(cx - half, cy - half), (cx + half, cy + half)], fill=255, width=sw)
        od.line([(cx - half, cy + half), (cx + half, cy - half)], fill=255, width=sw)
    ca = canvas.getchannel("A")
    knocked = ImageChops.subtract(ca, overlay)
    canvas.putalpha(knocked)
    return canvas


_STATUS_ZH = {
    "running": "运行中",
    "stopped": "已停止",
    "starting": "启动中",
    "connecting": "连接中",
}


def _install_menu_gray_suffix() -> None:
    """Monkey-patch pystray's macOS ``_create_menu`` to post-process each
    NSMenu after it's built.  For every item whose title contains ``\\t``:

    1. Measure the left-side text width of ALL tab items in the same menu.
    2. Set a single right-aligned tab stop at ``max_left_width + pad`` so
       all right-side tags line up, adapting to the actual content width.
    3. Style the right part as smaller gray text, with a background badge
       on clickable items.

    This runs per-menu (including submenus), so the main menu and the
    models submenu each get their own optimal width.
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
    return appconfig.local_url(cfg)


def _tunnel_url(cfg) -> str:
    return appconfig.tunnel_url(cfg)


def _base_url(cfg) -> str:
    # 启动通知里优先报 tunnel 地址，没有就退回本地。
    return appconfig.base_url(cfg)


def _escape_applescript(s: str) -> str:
    """Escape a string for safe embedding in an AppleScript double-quoted literal.
    Backslashes must be escaped first, then double quotes."""
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _osascript_input(title: str, prompt: str, default: str = "", hidden: bool = False) -> str:
    """Show a native macOS input dialog via osascript. Returns user input or raises."""
    import subprocess
    hidden_clause = "with hidden answer" if hidden else ""
    escaped_prompt = _escape_applescript(prompt)
    escaped_default = _escape_applescript(default)
    script = (
        f'display dialog "{escaped_prompt}" '
        f'default answer "{escaped_default}" '
        f'with title "{title}" '
        f'{hidden_clause}'
    )
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=300,
        )
    except Exception as e:
        raise RuntimeError(f"无法弹出对话框: {e}")
    if result.returncode != 0:
        raise RuntimeError("用户取消了操作。")
    for part in result.stdout.strip().split(", "):
        if part.startswith("text returned:"):
            return part[len("text returned:"):]
    raise RuntimeError("无法解析对话框返回值。")


def _osascript_alert(title: str, message: str) -> None:
    """Show a simple macOS alert dialog."""
    if sys.platform != "darwin":
        return
    import subprocess
    escaped = _escape_applescript(message).replace("\n", "\\n")
    subprocess.run(
        ["osascript", "-e", f'display alert "{title}" message "{escaped}"'],
        capture_output=True, timeout=30,
    )


def _prompt_input(title: str, prompt: str, default: str = "", hidden: bool = False) -> str:
    """Cross-platform input prompt: osascript on macOS, tkinter elsewhere."""
    if sys.platform == "darwin":
        return _osascript_input(title, prompt, default, hidden)
    try:
        import tkinter as tk
        from tkinter import simpledialog
        root = tk.Tk()
        root.withdraw()
        val = simpledialog.askstring(title, prompt, parent=root, show="*" if hidden else None)
        root.destroy()
        if val is None:
            raise RuntimeError("用户取消了操作。")
        return val
    except ImportError:
        raise RuntimeError("无法弹出输入框，请改用 CLI 模式（kiro-gateway-tray --cli）。")


def _generate_api_key(length: int = 32) -> str:
    """Generate a cryptographically random API key."""
    import secrets
    import string
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def _osascript_form_cf(title: str, default_url: str = "") -> tuple[str, str]:
    """macOS two-step form: provision URL then shared secret.

    Uses plain `display dialog` (no System Events, no permission prompt).
    Returns (provision_url, secret).
    """
    import subprocess
    escaped_url = _escape_applescript(default_url)
    script = (
        f'display dialog "请输入 Provision 服务地址：\\n\\n由管理员提供的隧道签发服务 URL" '
        f'with title "{title} (1/2)" '
        f'default answer "{escaped_url}" '
        f'buttons {{"取消", "下一步"}} default button "下一步"'
    )
    result = subprocess.run(
        ["osascript", "-e", script], capture_output=True, text=True, timeout=300,
    )
    if result.returncode != 0:
        raise RuntimeError("用户取消了操作。")
    url = ""
    for part in result.stdout.strip().split(", "):
        if part.startswith("text returned:"):
            url = part[len("text returned:"):]
    if not url:
        raise RuntimeError("Provision 服务地址不能为空。")

    escaped_url2 = _escape_applescript(url)
    script2 = (
        f'display dialog "Worker: {escaped_url2}\\n\\n请输入激活码（共享密钥）：" '
        f'with title "{title} (2/2)" '
        f'default answer "" '
        f'with hidden answer '
        f'buttons {{"上一步", "完成"}} default button "完成"'
    )
    result2 = subprocess.run(
        ["osascript", "-e", script2], capture_output=True, text=True, timeout=300,
    )
    if result2.returncode != 0:
        raise RuntimeError("用户取消了操作。")
    output = result2.stdout.strip()
    btn = ""
    secret = ""
    for part in output.split(", "):
        if part.startswith("button returned:"):
            btn = part[len("button returned:"):]
        elif part.startswith("text returned:"):
            secret = part[len("text returned:"):]
    if btn == "上一步":
        return _osascript_form_cf(title, url)
    if not secret:
        raise RuntimeError("激活码不能为空。")
    return url, secret


def _first_run_setup(cfg) -> str:
    """Guided setup. One page for Cloudflare credentials.

    Gateway API key is auto-generated (strong random).
    Auto-reads profile_arn from Kiro token file.
    Returns the shared secret for provisioning.
    """
    from . import provision as _prov

    # --- Page: Cloudflare (provision_url + shared secret) ---
    if sys.platform == "darwin":
        url, secret = _osascript_form_cf(
            "Kiro Tray - 隧道配置",
            default_url=cfg.cloudflare.provision_url or "",
        )
    else:
        if not cfg.cloudflare.provision_url:
            url = _prompt_input(
                "Kiro Tray - 隧道配置",
                "请输入 Provision 服务地址：\n\n"
                "由管理员提供的隧道签发服务 URL。",
            ).strip()
            if not url:
                raise RuntimeError("Provision 服务地址不能为空。")
        else:
            url = cfg.cloudflare.provision_url
        secret = _prompt_input(
            "Kiro Tray - 隧道配置",
            f"请输入激活码（共享密钥）：\n\nWorker: {url}",
            hidden=True,
        ).strip()
        if not secret:
            raise RuntimeError("激活码不能为空。")

    cfg.cloudflare.provision_url = url
    appconfig.save(cfg)

    # --- Auto-generate Gateway API key ---
    auto_arn = _prov.read_profile_arn(cfg)
    if auto_arn and not cfg.gateway.profile_arn:
        cfg.gateway.profile_arn = auto_arn
    auto_region = _prov.read_api_region(cfg)
    if auto_region:
        cfg.gateway.api_region = auto_region

    if not cfg.gateway.proxy_api_key or cfg.gateway.proxy_api_key == "change-me":
        cfg.gateway.proxy_api_key = _generate_api_key()
    appconfig.save(cfg)

    return secret


class _UsageCache:
    """Thread-safe cache for the /usage result, rendered by the menu line.

    Refreshes at most once per 60 seconds. The menu line renders instantly
    from the cached value; a background thread fetches fresh data.
    """
    _COOLDOWN = 60  # seconds between refreshes

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._text: str | None = None
        self._inflight = False
        self._last_fetch: float = 0

    def display(self) -> str:
        with self._lock:
            if self._inflight:
                return "获取中…"
            return self._text if self._text is not None else "加载中…"

    def refresh(self, icon) -> None:
        import time
        now = time.monotonic()
        with self._lock:
            if self._inflight:
                return
            if self._text is not None and (now - self._last_fetch) < self._COOLDOWN:
                return
            self._inflight = True

        def _work():
            try:
                data = usage.fetch()
                text = usage.format_menu_line(data)
            except Exception:
                text = "获取失败"
            with self._lock:
                self._text = text
                self._inflight = False
                self._last_fetch = time.monotonic()
            try:
                icon.update_menu()
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
    sup.provision_callback = _first_run_setup
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
            import subprocess
            if sys.platform == "darwin":
                subprocess.run(
                    ["pbcopy"], input=value.encode(), check=True, timeout=5,
                )
            elif sys.platform == "win32":
                subprocess.run(
                    ["clip"], input=value.encode("utf-16le"), check=True,
                    timeout=5,
                )
            else:
                subprocess.run(
                    ["xclip", "-selection", "clipboard"],
                    input=value.encode(), check=True, timeout=5,
                )
            _notify(icon, "Kiro Tray", f"已复制{label}")
        except Exception:
            _notify(icon, label, value)

    def on_copy_local_url(icon, _item):
        cfg = appconfig.load(use_cache=True)
        _copy(icon, _local_url(cfg), "本地 URL")

    def on_copy_tunnel_url(icon, _item):
        cfg = appconfig.load(use_cache=True)
        _copy(icon, _tunnel_url(cfg), "Tunnel URL")

    def on_copy_password(icon, _item):
        cfg = appconfig.load(use_cache=True)
        _copy(icon, cfg.gateway.proxy_api_key, "Gateway 密码")

    def on_open_config(_icon, _item):
        webbrowser.open(paths.config_file().as_uri())

    def on_open_logs(_icon, _item):
        webbrowser.open(paths.log_dir().as_uri())

    def on_quit(icon, _item):
        sup.stop()
        icon.stop()

    # --- status line text callables (re-evaluated each time menu shows) ---
    # \t makes macOS NSMenuItem right-align the text after the tab.
    def gateway_line(_item):
        s = sup.status()
        return f"🖥 网关: 本地 Kiro Gateway\t{_STATUS_ZH.get(s['gateway'], s['gateway'])}"

    def tunnel_line(_item):
        s = sup.status()
        return f"🌐 隧道: Cloudflare Tunnel\t{_STATUS_ZH.get(s['tunnel'], s['tunnel'])}"

    def usage_line(_item):
        usage_cache.refresh(icon)
        return f"📊 额度: {usage_cache.display()}"

    # --- copy items: show actual value + clipboard tag ---
    def local_url_line(_item):
        cfg = appconfig.load(use_cache=True)
        return f"🔗 本地 URL: {_local_url(cfg)}\t复制"

    def tunnel_url_line(_item):
        cfg = appconfig.load(use_cache=True)
        url = _tunnel_url(cfg) or "未配置"
        return f"🔗 隧道 URL: {url}\t复制"

    def password_line(_item):
        cfg = appconfig.load(use_cache=True)
        key = cfg.gateway.proxy_api_key
        masked = key[:1] + "***" + key[-1:] if len(key) >= 2 else "***"
        return f"🔑 Gateway 密码: {masked}\t复制"

    # --- models submenu: dynamic list loaded from /v1/models ---
    _models_cache = {"items": None, "inflight": False}
    _models_lock = threading.Lock()

    def _refresh_models():
        with _models_lock:
            if _models_cache["inflight"]:
                return
            _models_cache["inflight"] = True

        def _work():
            try:
                models = usage.fetch_models()
            except Exception:
                models = []
            with _models_lock:
                _models_cache["items"] = models
                _models_cache["inflight"] = False
            try:
                icon.update_menu()
            except Exception:
                pass

        threading.Thread(target=_work, daemon=True).start()

    def _on_copy_model(model_id):
        def _handler(icon, _item):
            _copy(icon, model_id, f"模型 {model_id}")
        return _handler

    def _models_submenu_items():
        _refresh_models()
        with _models_lock:
            items = _models_cache["items"]
        if items is None:
            return [pystray.MenuItem("加载中…", None, enabled=False)]
        if not items:
            return [pystray.MenuItem("无可用模型", None, enabled=False)]
        return [
            pystray.MenuItem(f"{m}\t复制", _on_copy_model(m))
            for m in items
        ]

    def start_line(_item):
        if sup.status()["gateway"] == "running":
            return "🔄 重启"
        return "▶️ 启动"

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
        pystray.MenuItem(
            "🤖  模型列表",
            pystray.Menu(_models_submenu_items),
        ),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(local_url_line, on_copy_local_url),
        pystray.MenuItem(tunnel_url_line, on_copy_tunnel_url),
        pystray.MenuItem(password_line, on_copy_password),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("📄  打开配置文件", on_open_config),
        pystray.MenuItem("📁  打开日志目录", on_open_logs),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(start_line, on_start_or_restart),
        pystray.MenuItem("⏹️  停止", on_stop),
        pystray.MenuItem("🚪  退出", on_quit),
    )

    # --- first-run guided setup BEFORE tray loop (main thread, dialogs work) ---
    cfg = appconfig.load()
    if not appconfig.is_provisioned(cfg):
        try:
            secret = _first_run_setup(cfg)
            cfg = appconfig.load()  # reload after provision_url was saved
            sup.register(cfg, secret)
        except Exception as e:
            print(f"[kiro-gateway-tray setup error] {e}", file=sys.stderr)
            _osascript_alert("Kiro Tray 错误", str(e)[:300])
            return

    def _startup():
        import time
        time.sleep(0.5)
        try:
            sup.start()
        except Exception as e:
            print(f"[kiro-gateway-tray startup error] {e}", file=sys.stderr)
            _notify(icon, "Kiro Tray 错误", str(e)[:200])
        _refresh_icon(icon)
        icon.update_menu()

    icon = pystray.Icon("kiro-gateway-tray", make_icon(False), "Kiro Gateway", menu)
    _install_retina_icon_fix()  # macOS only; sharp menu-bar glyph + template tint
    _install_menu_gray_suffix()  # macOS only; right-aligned gray text after \t
    threading.Thread(target=_startup, daemon=True).start()
    _kick_update_check()        # startup check; updates.check() handles 24h caching
    icon.run()
