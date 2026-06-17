# app/kiro_gateway_tray/tray.py
"""System-tray / menu-bar UI via pystray."""
from __future__ import annotations

import sys
import threading
import webbrowser
from typing import Callable

from . import appconfig, dialogs, macos_menu, paths, platform_compat, usage
from .async_cache import AsyncRefreshCache
from .icon import make_icon
from .supervisor import Supervisor


class TrayUnavailable(RuntimeError):
    pass


_STATUS_ZH = {
    "running": "运行中",
    "stopped": "已停止",
    "starting": "启动中",
    "connecting": "连接中",
    "error": "异常",
}


def _local_url(cfg) -> str:
    return appconfig.local_url(cfg)


def _tunnel_url(cfg) -> str:
    return appconfig.tunnel_url(cfg)


def _base_url(cfg) -> str:
    # 启动通知里优先报 tunnel 地址，没有就退回本地。
    return appconfig.base_url(cfg)


def _first_run_setup(cfg) -> str:
    """Guided setup. One page for Cloudflare credentials.

    Gateway API key is auto-generated (strong random).
    Auto-reads profile_arn from Kiro token file.
    Returns the shared secret for provisioning.
    """
    from . import provision as _prov

    # --- Page: Cloudflare (provision_url + shared secret) ---
    if sys.platform == "darwin":
        url, secret = dialogs.osascript_form_cf(
            "Kiro Tray - 隧道配置",
            default_url=cfg.cloudflare.provision_url or "",
        )
    else:
        if not cfg.cloudflare.provision_url:
            url = dialogs.prompt_input(
                "Kiro Tray - 隧道配置",
                "请输入 Provision 服务地址：\n\n"
                "由管理员提供的隧道签发服务 URL。",
            ).strip()
            if not url:
                raise RuntimeError("Provision 服务地址不能为空。")
        else:
            url = cfg.cloudflare.provision_url
        secret = dialogs.prompt_input(
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
        cfg.gateway.proxy_api_key = dialogs.generate_api_key()
    appconfig.save(cfg)

    return secret


class _UsageCache:
    """Thread-safe cache for the /usage result, rendered by the menu line.

    Refreshes at most once per 60 seconds. The menu line renders instantly
    from the cached value; a background thread fetches fresh data.
    """
    _COOLDOWN = 60  # seconds between refreshes

    def __init__(self, on_update: "Callable[[], None] | None" = None) -> None:
        # fetch returns a ready-to-render string and never raises, so the cache
        # value doubles as the menu text.
        def _fetch() -> str:
            try:
                return usage.format_menu_line(usage.fetch())
            except Exception:
                return "获取失败"

        self._cache = AsyncRefreshCache(_fetch, cooldown=self._COOLDOWN, on_update=on_update)

    def display(self) -> str:
        if self._cache.inflight and self._cache.get() is None:
            return "获取中…"
        val = self._cache.get()
        return val if val is not None else "加载中…"

    def refresh(self, icon) -> None:  # icon kept for call-site compatibility
        self._cache.refresh()


def run() -> None:
    """Start the tray loop. Raises TrayUnavailable if no backend works."""
    try:
        import pystray
    except Exception as e:
        raise TrayUnavailable(str(e))

    sup = Supervisor()
    sup.provision_callback = _first_run_setup
    # icon is created near the end of run(); late-bind it so background caches
    # can request a menu redraw once a fresh value lands.
    _icon_ref: dict = {"icon": None}

    def _request_redraw() -> None:
        ic = _icon_ref["icon"]
        if ic is not None:
            try:
                ic.update_menu()
            except Exception:
                pass

    usage_cache = _UsageCache(on_update=_request_redraw)

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
            platform_compat.copy_to_clipboard(value)
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
    models_cache = AsyncRefreshCache(usage.fetch_models, on_update=_request_redraw)

    def _on_copy_model(model_id):
        def _handler(icon, _item):
            _copy(icon, model_id, f"模型 {model_id}")
        return _handler

    def _models_submenu_items():
        models_cache.refresh()
        items = models_cache.get()
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
            dialogs.alert("Kiro Tray 错误", str(e)[:300])
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
    _icon_ref["icon"] = icon
    macos_menu.install_retina_icon_fix()  # macOS only; sharp menu-bar glyph + template tint
    macos_menu.install_menu_gray_suffix()  # macOS only; right-aligned gray text after \t
    threading.Thread(target=_startup, daemon=True).start()
    _kick_update_check()        # startup check; updates.check() handles 24h caching
    icon.run()
