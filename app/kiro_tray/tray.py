# app/kiro_tray/tray.py
"""System-tray / menu-bar UI via pystray."""
from __future__ import annotations

import threading
import webbrowser
from pathlib import Path

from . import appconfig, paths, usage
from .supervisor import Supervisor


class TrayUnavailable(RuntimeError):
    pass


def _load_icon():
    from PIL import Image
    icon_path = Path(__file__).parent.parent / "resources" / "icon.png"
    if icon_path.exists():
        return Image.open(icon_path)
    return Image.new("RGB", (64, 64), (60, 120, 220))


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
            icon.update_menu()
        threading.Thread(target=_work, daemon=True).start()

    def on_stop(icon, _item):
        sup.stop()
        _notify(icon, "Kiro Tray", "网关已停止")
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

    icon = pystray.Icon("kiro-tray", _load_icon(), "Kiro Gateway", menu)
    threading.Thread(target=sup.start, daemon=True).start()
    _kick_update_check()        # startup check; updates.check() handles 24h caching
    icon.run()
