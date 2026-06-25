# app/kiro_gateway_tray/tray.py
"""System-tray / menu-bar UI via pystray."""
from __future__ import annotations

import sys
import threading
import time
import webbrowser
from typing import Callable

from . import __version__, GITHUB_REPO, appconfig, autostart, dialogs, macos_menu, notify as _notify_mod, paths, platform_compat, usage
from .async_cache import AsyncRefreshCache
from .icon import make_icon
from .log import logger
from .notify import APP_NAME
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
    """Guided setup. Cloudflare credentials + Kiro profileArn.

    Gateway API key is auto-generated (strong random).
    profileArn is entered by the user (the gateway only writes it back into the
    token file after a successful run, so it is usually unreadable on first run);
    api_region is parsed from the entered ARN.
    Returns the shared secret for provisioning.
    """
    from . import provision as _prov

    # --- Page 1/3: Provision 服务地址 (URL, validated) ---
    url = dialogs.prompt_validated(
        f"{APP_NAME} - 隧道配置 (1/3)",
        "请输入 Provision 服务地址：\n\n由管理员提供的隧道签发服务 URL。",
        validate=dialogs.validate_url,
        default=cfg.cloudflare.provision_url or "",
    )
    cfg.cloudflare.provision_url = url
    appconfig.save(cfg)

    # --- Page 2/3: 激活码（共享密钥, hidden, validated） ---
    secret = dialogs.prompt_validated(
        f"{APP_NAME} - 隧道配置 (2/3)",
        f"请输入激活码（共享密钥）：\n\nWorker: {url}",
        validate=dialogs.validate_secret,
        hidden=True,
    )

    # --- Page 3/3: Kiro profileArn (multiline, validated) ---
    # profileArn 由 Kiro Gateway 成功运行后才写回 kiro-auth-token.json，首次初始化
    # 通常读不到，需要用户手动粘贴。ARN 很长，用多行可换行的输入框便于核对；
    # api_region 从该 ARN 中解析得到。
    default_arn = _prov.read_profile_arn(cfg)
    profile_arn = dialogs.prompt_validated(
        f"{APP_NAME} - Profile ARN (3/3)",
        "请输入 Kiro profileArn：\n\n"
        "形如 arn:aws:codewhisperer:us-east-1:123456789012:profile/XXXX\n"
        "（首次使用时 Kiro Gateway 尚未写回，需要手动填写）",
        validate=dialogs.validate_profile_arn,
        default=default_arn,
        multiline=True,
    )
    cfg.gateway.profile_arn = profile_arn
    region = _prov.region_from_arn(profile_arn)
    if region:
        cfg.gateway.api_region = region

    # --- Auto-generate Gateway API key ---
    if not cfg.gateway.proxy_api_key or cfg.gateway.proxy_api_key == "change-me":
        cfg.gateway.proxy_api_key = dialogs.generate_api_key()
    appconfig.save(cfg)

    return secret


class _ThrottleGate:
    """Collapse a burst of triggers into at most one in-flight worker.

    The tray re-runs every menu-line callable on each ``update_menu()``, and a
    state change can itself trigger another redraw. Without throttling, opening
    the menu once could spawn a short burst of duplicate probe/check threads.
    This gate admits a call only when no worker is in flight AND at least
    ``min_interval`` seconds have passed since the last admission.

    Thread-safe. ``min_interval`` of 0 means "only the in-flight guard applies".
    """

    def __init__(self, min_interval: float = 0.0) -> None:
        self._min_interval = min_interval
        self._busy = False
        self._last = 0.0
        self._ever_entered = False
        self._lock = threading.Lock()

    def try_enter(self, now: float | None = None) -> bool:
        """Return True if the caller may proceed (and mark a worker in flight)."""
        t = time.monotonic() if now is None else now
        with self._lock:
            if self._busy:
                return False
            if (
                self._min_interval
                and self._ever_entered
                and (t - self._last) < self._min_interval
            ):
                return False
            self._busy = True
            self._last = t
            self._ever_entered = True
            return True

    def done(self) -> None:
        with self._lock:
            self._busy = False


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
            except Exception as e:
                # 网关 /health 通过不代表 /usage 能成功：后者要带 Kiro token
                # 回源上游，token 失效/上游抖动/启动就绪窗口都会失败。这里把
                # 失败结果缓存 60 秒（避免每次重绘猛打上游）是合理的，但异常详情
                # 必须落盘，否则根因不可查。usage.fetch() 在非 200 时抛的
                # RuntimeError 已含状态码与响应体前 200 字符；把它拼进消息正文，
                # 再用 opt(exception=True) 附上堆栈（loguru 不认 logging 的
                # exc_info 参数，须用 opt(exception=)）。
                logger.opt(exception=True).warning("usage fetch failed: {}", e)
                return "获取失败"

        self._cache = AsyncRefreshCache(_fetch, cooldown=self._COOLDOWN, on_update=on_update)

    def display(self) -> str:
        if self._cache.inflight and self._cache.get() is None:
            return "获取中…"
        val = self._cache.get()
        return val if val is not None else "加载中…"

    def refresh(self, icon=None) -> None:  # icon kept for call-site compatibility
        self._cache.refresh()



class TrayApp:
    """Encapsulates the tray loop state. All the former dict-as-cell and nested
    closures live as instance attributes/methods, making throttle and render
    logic testable in isolation.
    """

    _PROBE_MIN_INTERVAL = 2.0  # seconds between on-open health probes
    _USAGE_REFRESH_INTERVAL = 60  # seconds between background usage refreshes

    def __init__(self) -> None:
        import pystray
        self._pystray = pystray

        self.sup = Supervisor()
        self.sup.provision_callback = _first_run_setup
        self._icon = None

        self._usage_cache = _UsageCache(on_update=self._request_redraw)
        self._models_cache = AsyncRefreshCache(
            usage.fetch_models, cooldown=60, on_update=self._request_redraw,
        )
        self.sup.on_status_change = self._request_redraw

        self._update_info = None
        self._update_gate = _ThrottleGate()
        self._probe_gate = _ThrottleGate(min_interval=self._PROBE_MIN_INTERVAL)
        self._usage_refresh_stop = threading.Event()

    # --- redraw / notify helpers ---

    def _request_redraw(self) -> None:
        ic = self._icon
        if ic is None:
            return

        def _do():
            try:
                ic.update_menu()
            except Exception:
                pass

        macos_menu.run_on_main_thread(_do)

    def _notify(self, title: str, msg: str) -> None:
        _notify_mod.notify(self._icon, title, msg)

    def _refresh_icon(self) -> None:
        try:
            self._icon.icon = make_icon(self.sup.status()["gateway"] == "running")
        except Exception:
            logger.debug("_refresh_icon failed", exc_info=True)

    # --- menu actions ---

    def _on_start_or_restart(self, icon, _item):
        restarting = self.sup.status()["gateway"] == "running"

        def _work():
            try:
                if restarting:
                    self.sup.restart()
                    verb = "已重启"
                else:
                    self.sup.start()
                    verb = "已启动"
                cfg = appconfig.load()
                self._notify(APP_NAME, f"{verb}\n{_tunnel_url(cfg)}")
            except Exception as e:
                self._notify(f"{APP_NAME} 错误", str(e)[:200])
            self._refresh_icon()
            icon.update_menu()
        threading.Thread(target=_work, daemon=True).start()

    def _on_stop(self, icon, _item):
        self.sup.stop()
        self._notify(APP_NAME, "网关已停止")
        self._refresh_icon()
        icon.update_menu()

    def _copy(self, value: str, label: str) -> None:
        try:
            platform_compat.copy_to_clipboard(value)
            self._notify(APP_NAME, f"已复制{label}")
        except Exception:
            self._notify(label, value)

    def _on_copy_local_url(self, _icon, _item):
        cfg = appconfig.load(use_cache=True)
        self._copy(_local_url(cfg), "本地 URL")

    def _on_copy_tunnel_url(self, _icon, _item):
        cfg = appconfig.load(use_cache=True)
        self._copy(_tunnel_url(cfg), "Tunnel URL")

    def _on_copy_password(self, _icon, _item):
        cfg = appconfig.load(use_cache=True)
        self._copy(cfg.gateway.proxy_api_key, "网关 密码")

    def _on_open_config(self, _icon, _item):
        platform_compat.open_file(paths.config_file())

    def _on_open_logs(self, _icon, _item):
        platform_compat.open_directory(paths.log_dir())

    def _on_quit(self, icon, _item):
        self._usage_refresh_stop.set()
        self.sup.stop()
        self.sup.close()
        icon.stop()

    def _on_update(self, _icon, _item):
        info = self._update_info
        if info:
            webbrowser.open(info.release_url)

    def _on_open_release(self, _icon, _item):
        webbrowser.open(
            f"https://github.com/{GITHUB_REPO}/releases/tag/v{__version__}"
        )

    def _on_toggle_autostart(self, icon, _item):
        want = not autostart.is_enabled()
        try:
            autostart.set_enabled(want)
        except Exception as e:
            logger.exception("toggle autostart failed")
            self._notify(f"{APP_NAME} 错误", f"设置开机自启失败：{str(e)[:160]}")
            icon.update_menu()
            return
        if want:
            extra = ""
            if sys.platform == "darwin":
                extra = "\n可在「系统设置 → 通用 → 登录项」中管理。"
            self._notify(APP_NAME, f"已开启开机自启，下次登录将自动启动。{extra}")
        else:
            self._notify(APP_NAME, "已关闭开机自启。")
        icon.update_menu()

    def _on_copy_model(self, model_id):
        def _handler(_icon, _item):
            self._copy(model_id, f"模型 {model_id}")
        return _handler

    # --- menu line callables ---
    # NOTE: on macOS these are NOT re-evaluated when the menu opens (the NSMenu
    # is static once set via setMenu_). They run only during update_menu(), i.e.
    # on a redraw. Anything that must stay live (usage, status) is driven by a
    # background refresh + _request_redraw, not by the user opening the menu.

    def _gateway_line(self, _item):
        self._on_menu_open()
        s = self.sup.status()
        return f"🖥 网关: 本地 Kiro Gateway\t{_STATUS_ZH.get(s['gateway'], s['gateway'])}"

    def _tunnel_line(self, _item):
        s = self.sup.status()
        return f"🌐 隧道: Cloudflare Tunnel\t{_STATUS_ZH.get(s['tunnel'], s['tunnel'])}"

    def _usage_line(self, _item):
        gw = self.sup.status()["gateway"]
        if gw != "running":
            return f"📊 额度: ({_STATUS_ZH.get(gw, gw)})"
        self._usage_cache.refresh()
        return f"📊 额度: {self._usage_cache.display()}"

    def _local_url_line(self, _item):
        cfg = appconfig.load(use_cache=True)
        return f"🔗 本地 URL: {_local_url(cfg)}\t复制"

    def _tunnel_url_line(self, _item):
        cfg = appconfig.load(use_cache=True)
        url = _tunnel_url(cfg) or "未配置"
        return f"🔗 隧道 URL: {url}\t复制"

    def _password_line(self, _item):
        cfg = appconfig.load(use_cache=True)
        key = cfg.gateway.proxy_api_key
        masked = key[:1] + "***" + key[-1:] if len(key) >= 2 else "***"
        return f"🔑 网关 密码: {masked}\t复制"

    def _autostart_line(self, _item):
        state = "✓ 已开启" if autostart.is_enabled() else "✗ 未开启"
        return f"🚀 开机自启\t{state}"

    def _models_submenu_items(self):
        pystray = self._pystray
        gw_status = self.sup.status()["gateway"]
        if gw_status != "running":
            label = _STATUS_ZH.get(gw_status, gw_status)
            return [pystray.MenuItem(f"等待服务就绪（{label}）…", None, enabled=False)]
        self._models_cache.refresh()
        items = self._models_cache.get()
        if items is None:
            return [pystray.MenuItem("加载中…", None, enabled=False)]
        if not items:
            return [pystray.MenuItem("无可用模型", None, enabled=False)]
        regular = [m for m in items if not m.startswith("kiro")]
        aliases = [m for m in items if m.startswith("kiro")]
        menu_items = [
            pystray.MenuItem(f"{m}\t复制", self._on_copy_model(m))
            for m in regular
        ]
        if aliases:
            if menu_items:
                menu_items.append(pystray.Menu.SEPARATOR)
            menu_items.append(
                pystray.MenuItem("别名（Cursor 内使用）", None, enabled=False)
            )
            menu_items.extend(
                pystray.MenuItem(f"{m}\t复制", self._on_copy_model(m))
                for m in aliases
            )
        return menu_items

    def _start_line(self, _item):
        if self.sup.status()["gateway"] == "running":
            return "🔄 重启"
        return "▶️ 启动"

    def _version_line(self, _item) -> str:
        self._ensure_update_info_sync()
        self._kick_update_check()
        from . import updates
        line = f"ℹ️ 当前版本 v{__version__}"
        cached = updates._read_cache() or {}
        latest = cached.get("latest")
        if not latest:
            return f"{line}\t检查中…"
        latest_ver = latest.lstrip("vV")
        if latest_ver == __version__:
            return f"{line}\t已是最新"
        if updates._is_newer(__version__, latest):
            return f"{line}\t可升级 {latest_ver}"
        return f"{line}\t已是最新"

    def _ensure_update_info_sync(self) -> None:
        """Apply cached update info immediately (no network)."""
        if self._update_info is not None:
            return
        try:
            from . import updates
            info = updates.peek_cached()
            if info is not None:
                self._update_info = info
        except Exception:
            logger.debug("update cache peek failed", exc_info=True)

    def _update_visible(self, _item) -> bool:
        # Evaluated before other menu lines (update item is first). Sync peek
        # here so the line can appear on the first open without waiting for the
        # async GitHub fetch kicked off below.
        self._ensure_update_info_sync()
        self._kick_update_check()
        return self._update_info is not None

    def _update_line(self, _item) -> str:
        info = self._update_info
        return f"🔔 有新版本 {info.latest}，点击下载" if info else ""

    # --- update check + probe throttle ---

    def _kick_update_check(self) -> None:
        if not self._update_gate.try_enter():
            logger.debug("update check skipped (in flight)")
            return

        def _work():
            try:
                from . import updates
                stale = updates._should_check()
                if stale:
                    logger.info("update check: querying GitHub releases")
                info = updates.check()
                logger.info(
                    "update check: current={} latest={} available={}",
                    info.current,
                    info.latest,
                    info.update_available,
                )
                if info.update_available:
                    prev = self._update_info
                    self._update_info = info
                    if prev is None or prev.latest != info.latest:
                        self._request_redraw()
            except Exception:
                logger.warning("update check failed", exc_info=True)
            finally:
                self._update_gate.done()
        threading.Thread(target=_work, daemon=True).start()

    def _on_menu_open(self) -> None:
        if self._probe_gate.try_enter():
            def _probe():
                try:
                    self.sup.probe_now()
                except Exception:
                    logger.debug("probe_now failed", exc_info=True)
                finally:
                    self._probe_gate.done()
            threading.Thread(target=_probe, daemon=True).start()

    def _start_usage_refresh_loop(self) -> None:
        """Periodically refresh the usage cache while the gateway is running.

        On macOS the tray menu is a static NSMenu installed via setMenu_: opening
        it does NOT re-evaluate the Python label callables, so _usage_line's
        refresh() only ever ran during update_menu() — which, once the gateway
        settles and stops emitting status transitions, is never called again.
        The menu line then freezes for hours. This loop is the missing driver:
        it ticks the cache on its own cadence and lets the cache's on_update fire
        a redraw, so the displayed quota tracks the account in near real time
        regardless of whether the user opens the menu.
        """
        def _loop():
            while not self._usage_refresh_stop.wait(self._USAGE_REFRESH_INTERVAL):
                if self.sup.status()["gateway"] != "running":
                    continue
                try:
                    self._usage_cache.refresh()
                except Exception:
                    logger.debug("usage refresh tick failed", exc_info=True)

        threading.Thread(target=_loop, daemon=True).start()

    # --- build the menu and run the loop ---

    def _build_menu(self):
        pystray = self._pystray
        return pystray.Menu(
            pystray.MenuItem(self._update_line, self._on_update, visible=self._update_visible),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(self._gateway_line, None, enabled=False),
            pystray.MenuItem(self._tunnel_line, None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(self._usage_line, None, enabled=False),
            pystray.MenuItem(
                "🤖 模型列表",
                pystray.Menu(self._models_submenu_items),
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(self._local_url_line, self._on_copy_local_url),
            pystray.MenuItem(self._tunnel_url_line, self._on_copy_tunnel_url),
            pystray.MenuItem(self._password_line, self._on_copy_password),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("📄 打开配置文件", self._on_open_config),
            pystray.MenuItem("📁 打开日志目录", self._on_open_logs),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(self._autostart_line, self._on_toggle_autostart),
            pystray.MenuItem(self._version_line, self._on_open_release),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(self._start_line, self._on_start_or_restart),
            pystray.MenuItem("⏹️ 停止", self._on_stop),
            pystray.MenuItem("⏏️ 退出", self._on_quit),
        )

    def run(self) -> None:
        pystray = self._pystray

        # --- first-run guided setup BEFORE tray loop (main thread for macOS AppKit)
        cfg = appconfig.load()
        pending_secret: str | None = None
        if not appconfig.is_provisioned(cfg):
            try:
                pending_secret = _first_run_setup(cfg)
                cfg = appconfig.load()  # reload after provision_url was saved
            except Exception as e:
                print(f"[kiro-gateway-tray setup error] {e}", file=sys.stderr)
                logger.exception("first-run setup failed")
                dialogs.alert(f"{APP_NAME} 错误", str(e)[:300])
                return

        def _startup():
            time.sleep(0.5)
            try:
                if pending_secret is not None:
                    cfg_reg = appconfig.load()
                    self.sup.register(cfg_reg, pending_secret)
                self.sup.start()
            except Exception as e:
                print(f"[kiro-gateway-tray startup error] {e}", file=sys.stderr)
                logger.exception("supervisor start failed")
                self._notify(f"{APP_NAME} 错误", str(e)[:200])
            self._refresh_icon()
            self._icon.update_menu()

        self.sup.mark_starting()
        menu = self._build_menu()
        self._icon = pystray.Icon("kiro-gateway-tray", make_icon(False), "Kiro Gateway", menu)
        macos_menu.install_retina_icon_fix()
        macos_menu.install_menu_gray_suffix()
        threading.Thread(target=_startup, daemon=True).start()
        self._kick_update_check()
        self._start_usage_refresh_loop()
        self._icon.run()


def run() -> None:
    """Start the tray loop. Raises TrayUnavailable if no backend works."""
    try:
        import pystray  # noqa: F401 — ensure importable before constructing TrayApp
    except Exception as e:
        raise TrayUnavailable(str(e))
    TrayApp().run()
