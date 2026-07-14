# app/kiro_gateway_tray/tray.py
"""System-tray / menu-bar UI via pystray."""
from __future__ import annotations

import sys
import threading
import time
import webbrowser
from typing import Callable

from . import (
    __version__,
    GITHUB_REPO,
    appconfig,
    autostart,
    dialogs,
    macos_menu,
    notify as _notify_mod,
    paths,
    platform_compat,
    request_activity,
    tray_live,
    usage,
)
from .async_cache import AsyncRefreshCache
from .icon import make_icon
from .log import logger
from .notify import APP_NAME
from .request_activity import ActivitySnapshot
from .supervisor import Supervisor
from .theme_watcher import ThemeWatcher


class TrayUnavailable(RuntimeError):
    pass


_STATUS_ZH = {
    "running": "运行中",
    "stopped": "已停止",
    "starting": "启动中",
    "connecting": "连接中",
    "error": "异常",
}

# Prefixes used to find live-updatable NSMenuItems while the menu is open.
_ACTIVITY_ACTIVE_PREFIX = "📡 进行中"
_ACTIVITY_RECENT_TITLE = "💬 最近对话"
_GATEWAY_PREFIX = "🖥 网关:"
_TUNNEL_PREFIX = "🌐 隧道:"


def _local_url(cfg) -> str:
    return appconfig.local_url(cfg)


def _tunnel_url(cfg) -> str:
    return appconfig.tunnel_url(cfg)


def _base_url(cfg) -> str:
    # 启动通知里优先报 tunnel 地址，没有就退回本地。
    return appconfig.base_url(cfg)


def _speedtest_url(cfg) -> str:
    """URL of the gateway's built-in speed-test page (tunnel host).

    Only ever called when a tunnel is provisioned (the menu item is hidden
    otherwise), so this always targets the public host to measure the full
    edge→cloudflared→local round-trip. Note there is no ``/v1`` suffix — the
    speed-test routes live at the gateway root under ``/speedtest``.

    The proxy API key is appended as a ``?key=`` param so the page (opened from
    the menu) authenticates without the user having to paste the password. The
    page prefills its input from this param.
    """
    from urllib.parse import quote
    base = f"https://{cfg.cloudflare.hostname}/speedtest"
    key = cfg.gateway.proxy_api_key or ""
    return f"{base}?key={quote(key, safe='')}" if key else base


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


class _FallbackLiveClickDelegate:
    """Plain stand-in for ``macos_menu.make_live_click_delegate`` in unit tests."""

    def __init__(self) -> None:
        self._handlers: dict[int, Callable[[], None]] = {}

    def clear(self) -> None:
        self._handlers.clear()

    def setHandler_forTag_(self, handler, tag) -> None:
        if handler is None:
            self._handlers.pop(int(tag), None)
        else:
            self._handlers[int(tag)] = handler

    def activateLiveItem_(self, sender) -> None:
        handler = self._handlers.get(int(sender.tag()))
        if handler is not None:
            handler()


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
    # Poll the activity file often enough to notice start/finish; do NOT redraw
    # on this cadence just to tick elapsed time (see _on_activity_update).
    _ACTIVITY_REFRESH_INTERVAL = 3.0

    def __init__(self) -> None:
        import pystray
        self._pystray = pystray

        self.sup = Supervisor()
        self.sup.provision_callback = _first_run_setup
        self._icon = None

        self._usage_cache = _UsageCache(on_update=self._request_redraw)
        self._models_cache = AsyncRefreshCache(
            usage.fetch_models, cooldown=14400, on_update=self._request_redraw,
        )
        self._activity_cache = AsyncRefreshCache(
            request_activity.load_snapshot,
            cooldown=0,
            on_update=self._on_activity_update,
        )
        self._activity_fingerprint: str | None = None
        self._live_active_ids: tuple[str, ...] | None = None
        self._live_recent_fp: str | None = None
        self._live_active_click_delegate = None
        self._live_recent_click_delegate = None
        self._active_autorebuild_delegate = None
        self._recent_autorebuild_delegate = None
        self._redraw_deferred = False
        self._menu_session_open = False
        self.sup.on_status_change = self._on_supervisor_status_change

        self._update_info = None
        self._update_gate = _ThrottleGate()
        self._probe_gate = _ThrottleGate(min_interval=self._PROBE_MIN_INTERVAL)
        self._usage_refresh_stop = threading.Event()
        # Windows-only: auto-adapt the tray icon to the taskbar light/dark
        # theme. Constructed in run(); start() is a no-op off Windows.
        self._theme_watcher: ThemeWatcher | None = None

    def _on_activity_update(self) -> None:
        """Redraw only when the activity snapshot's contents change.

        Never rebuild the menu just to refresh elapsed-time labels: on macOS,
        ``update_menu`` → ``setMenu:`` while the status menu is open breaks
        AppKit menu tracking and can freeze keyboard input system-wide until
        the tray process exits. While the menu is open, elapsed times are
        patched in place via ``macos_menu`` live titles instead.
        """
        snap = self._activity_cache.get()
        if snap is None:
            return
        fp = self._activity_fingerprint_of(snap)
        if fp == self._activity_fingerprint:
            return
        self._activity_fingerprint = fp
        if self._menu_session_open or macos_menu.is_status_menu_open(self._icon):
            # Structure changed under an open menu: refresh titles in place and
            # defer a full rebuild until menuDidClose.
            self._redraw_deferred = True
            if sys.platform == "darwin":
                self._live_patch_open_menu(snap)
            else:
                self._live_patch_open_menu_crossplatform(snap)
            return
        self._request_redraw()

    @staticmethod
    def _activity_fingerprint_of(snap: ActivitySnapshot) -> str:
        active = tuple(
            (a.id, a.phase, a.question_preview, a.model) for a in snap.active
        )
        recent = tuple(
            (r.id, r.ok, r.duration_ms, r.question_preview, r.answer_preview, r.error_preview)
            for r in snap.recent
        )
        return repr((active, recent))

    def _set_activity_cache_value(self, snap: ActivitySnapshot) -> None:
        """Write the activity cache without firing on_update (avoids redraw storms)."""
        cache = self._activity_cache
        with cache._lock:
            cache._value = snap
            cache._succeeded = True
            cache._last_fetch = time.monotonic()
            cache._backoff = 0.0

    def _activity_active_title(self, snap: ActivitySnapshot | None = None) -> str:
        gw = self.sup.status()["gateway"]
        if gw != "running":
            return f"{_ACTIVITY_ACTIVE_PREFIX}\t({_STATUS_ZH.get(gw, gw)})"
        snap = self._activity_snapshot() if snap is None else snap
        n = len(snap.active)
        if n == 0:
            return f"{_ACTIVITY_ACTIVE_PREFIX}\t空闲"
        oldest = min(snap.active, key=lambda a: a.started_at)
        elapsed = request_activity.format_duration(time.time() - oldest.started_at)
        return f"{_ACTIVITY_ACTIVE_PREFIX} ({n})\t最长 {elapsed}"

    def _gateway_title(self) -> str:
        s = self.sup.status()
        return f"{_GATEWAY_PREFIX} 本地 Kiro Gateway\t{_STATUS_ZH.get(s['gateway'], s['gateway'])}"

    def _tunnel_title(self) -> str:
        s = self.sup.status()
        return f"{_TUNNEL_PREFIX} Cloudflare Tunnel\t{_STATUS_ZH.get(s['tunnel'], s['tunnel'])}"

    @staticmethod
    def _recent_fingerprint_of(snap: ActivitySnapshot) -> str:
        return repr(
            tuple(
                (r.id, r.ok, r.duration_ms, r.question_preview, r.answer_preview, r.error_preview)
                for r in snap.recent
            )
        )

    def _ensure_live_click_delegate(self, which: str):
        attr = (
            "_live_active_click_delegate"
            if which == "active"
            else "_live_recent_click_delegate"
        )
        delegate = getattr(self, attr)
        if delegate is None:
            delegate = macos_menu.make_live_click_delegate()
            # Non-AppKit fallback for unit tests: a plain object with the same API.
            if delegate is None:
                delegate = _FallbackLiveClickDelegate()
            setattr(self, attr, delegate)
        return delegate

    def _live_patch_status_titles(self, nsmenu=None) -> None:
        """Refresh gateway/tunnel status labels without setMenu:."""
        if nsmenu is None:
            ic = self._icon
            handle = getattr(ic, "_menu_handle", None) if ic is not None else None
            if not handle:
                return
            nsmenu = handle[0]
        if nsmenu is None:
            return
        try:
            gw_item = macos_menu.find_menu_item_by_title_prefix(nsmenu, _GATEWAY_PREFIX)
            if gw_item is not None:
                macos_menu.apply_menu_item_title(gw_item, self._gateway_title())
            tunnel_item = macos_menu.find_menu_item_by_title_prefix(nsmenu, _TUNNEL_PREFIX)
            if tunnel_item is not None:
                macos_menu.apply_menu_item_title(tunnel_item, self._tunnel_title())
        except Exception:
            logger.debug("live status title patch failed", exc_info=True)

    def _rebuild_active_submenu(self, active_item, snap: ActivitySnapshot) -> None:
        submenu = active_item.submenu() if active_item is not None else None
        if submenu is None:
            return
        gw = self.sup.status()["gateway"]
        if gw != "running":
            label = _STATUS_ZH.get(gw, gw)
            rows = [(f"网关未运行（{label}）", False, None)]
        elif not snap.active:
            rows = [("当前无进行中的请求", False, None)]
        else:
            now = time.time()
            rows = []
            for entry in snap.active:
                detail = entry.question_preview or entry.model
                handler = self._on_copy_activity_text(detail, "进行中请求")
                # Live-click handlers are zero-arg; pystray handlers take (icon, item).
                rows.append((
                    request_activity.format_active_line(entry, now=now),
                    True,
                    lambda h=handler: h(self._icon, None),
                ))
        macos_menu.replace_submenu_rows(
            submenu, rows, click_delegate=self._ensure_live_click_delegate("active")
        )
        self._live_active_ids = tuple(a.id for a in snap.active)

    def _rebuild_recent_submenu(self, recent_item, snap: ActivitySnapshot) -> None:
        submenu = recent_item.submenu() if recent_item is not None else None
        if submenu is None:
            return
        if not snap.recent:
            rows = [("暂无最近对话", False, None)]
        else:
            rows = []
            for r in snap.recent:
                detail = (
                    f"问: {r.question_preview or '（无）'}\n"
                    f"答: {r.answer_preview or '（无）'}"
                    if r.ok
                    else (
                        f"问: {r.question_preview or '（无）'}\n"
                        f"错误: {r.error_preview or '失败'}"
                    )
                )
                handler = self._on_copy_activity_text(detail, "最近对话")
                rows.append((
                    request_activity.format_recent_line(r),
                    True,
                    lambda h=handler: h(self._icon, None),
                ))
        macos_menu.replace_submenu_rows(
            submenu, rows, click_delegate=self._ensure_live_click_delegate("recent")
        )
        self._live_recent_fp = self._recent_fingerprint_of(snap)

    def _resolve_status_menu(self, nsmenu=None):
        if nsmenu is not None:
            return nsmenu
        ic = self._icon
        handle = getattr(ic, "_menu_handle", None) if ic is not None else None
        if not handle:
            return None
        return handle[0]

    def _attach_submenu_autorebuild(self, nsmenu=None) -> None:
        """Attach ``menuNeedsUpdate:`` delegates so dynamic submenus refill on
        each expand.

        AppKit re-renders a submenu that is refilled *before* it is displayed,
        but not one edited while already open. Rebuilding on ``menuNeedsUpdate:``
        guarantees every expand shows the current snapshot. Re-attached on each
        root open because ``setMenu:`` (redraw) builds fresh NSMenu objects.
        """
        nsmenu = self._resolve_status_menu(nsmenu)
        if nsmenu is None:
            return
        try:
            active_item = macos_menu.find_menu_item_by_title_prefix(
                nsmenu, _ACTIVITY_ACTIVE_PREFIX
            )
            if active_item is not None and active_item.submenu() is not None:
                self._active_autorebuild_delegate = macos_menu.attach_submenu_autorebuild(
                    active_item.submenu(),
                    lambda _sub, it=active_item: self._rebuild_active_submenu(
                        it, request_activity.load_snapshot()
                    ),
                )
            recent_item = macos_menu.find_menu_item_by_exact_title(
                nsmenu, _ACTIVITY_RECENT_TITLE
            )
            if recent_item is not None and recent_item.submenu() is not None:
                self._recent_autorebuild_delegate = macos_menu.attach_submenu_autorebuild(
                    recent_item.submenu(),
                    lambda _sub, it=recent_item: self._rebuild_recent_submenu(
                        it, request_activity.load_snapshot()
                    ),
                )
        except Exception:
            logger.debug("attach submenu autorebuild failed", exc_info=True)

    def _inplace_patch_active_submenu(self, active_item, snap: ActivitySnapshot) -> None:
        """Refresh visible active rows in place (works while the submenu is open).

        Structural add/remove does not re-render an already-displayed submenu,
        so we only ``setTitle:`` existing rows. When the request set went empty
        we rewrite row 0 to the idle placeholder instead of leaving a stale
        "生成中" line; ``menuNeedsUpdate:`` fixes the structure on the next expand.
        """
        submenu = active_item.submenu() if active_item is not None else None
        if submenu is None:
            return
        try:
            n = int(submenu.numberOfItems())
        except Exception:
            return
        if n <= 0:
            return
        gw = self.sup.status()["gateway"]
        if gw != "running":
            first = submenu.itemAtIndex_(0)
            if first is not None and not first.isSeparatorItem():
                macos_menu.apply_menu_item_title(
                    first, f"网关未运行（{_STATUS_ZH.get(gw, gw)}）"
                )
            return
        if not snap.active:
            first = submenu.itemAtIndex_(0)
            if first is not None and not first.isSeparatorItem():
                macos_menu.apply_menu_item_title(first, "当前无进行中的请求")
            return
        now = time.time()
        for i, entry in enumerate(snap.active):
            if i >= n:
                break
            row = submenu.itemAtIndex_(i)
            if row is None or row.isSeparatorItem():
                continue
            macos_menu.apply_menu_item_title(
                row, request_activity.format_active_line(entry, now=now)
            )

    def _live_patch_open_menu(self, snap: ActivitySnapshot, nsmenu=None, *, force_rebuild: bool = False) -> None:
        """In-place updates for an already-open status menu (macOS).

        Patches gateway/tunnel status titles and the "进行中" header/rows without
        setMenu:. Submenu *structure* is refreshed by the ``menuNeedsUpdate:``
        delegates on each expand; here we only touch titles so changes render
        while a submenu is being displayed.
        """
        nsmenu = self._resolve_status_menu(nsmenu)
        if nsmenu is None:
            return
        try:
            self._live_patch_status_titles(nsmenu)

            active_item = macos_menu.find_menu_item_by_title_prefix(
                nsmenu, _ACTIVITY_ACTIVE_PREFIX
            )
            if active_item is not None:
                macos_menu.apply_menu_item_title(
                    active_item, self._activity_active_title(snap)
                )
                if force_rebuild:
                    self._rebuild_active_submenu(active_item, snap)
                else:
                    # Menu is open: structural add/remove won't re-render a
                    # displayed submenu, so only patch titles in place (this
                    # rewrites a finished row to the idle placeholder too). The
                    # menuNeedsUpdate: delegate rebuilds structure on next expand.
                    self._inplace_patch_active_submenu(active_item, snap)
                    self._live_active_ids = tuple(a.id for a in snap.active)

            recent_item = macos_menu.find_menu_item_by_exact_title(
                nsmenu, _ACTIVITY_RECENT_TITLE
            )
            if recent_item is not None:
                recent_fp = self._recent_fingerprint_of(snap)
                if force_rebuild:
                    self._rebuild_recent_submenu(recent_item, snap)
                else:
                    # Recent rows only change when a request finishes; structure
                    # is handled by menuNeedsUpdate: on the next expand. Track the
                    # fingerprint so the post-close redraw stays consistent.
                    self._live_recent_fp = recent_fp
        except Exception:
            logger.debug("live activity title patch failed", exc_info=True)

    def _on_status_menu_will_open(self, nsmenu) -> None:
        """Fresh snapshot + in-place titles at the moment the menu opens."""
        self._menu_session_open = True
        # Forget live submenu membership so the open path always rebuilds from
        # the latest snapshot (empty→items and stale placeholders included).
        self._live_active_ids = None
        self._live_recent_fp = None
        self._on_menu_open()
        try:
            self._attach_submenu_autorebuild(nsmenu)
            self._live_patch_status_titles(nsmenu)
            snap = request_activity.load_snapshot()
            self._set_activity_cache_value(snap)
            self._activity_fingerprint = self._activity_fingerprint_of(snap)
            self._live_patch_open_menu(snap, nsmenu, force_rebuild=True)
        except Exception:
            logger.debug("status menu will-open refresh failed", exc_info=True)

    def _on_status_menu_did_close(self, _nsmenu) -> None:
        self._menu_session_open = False
        self._live_active_ids = None
        self._live_recent_fp = None
        if self._redraw_deferred:
            self._request_redraw()

    def _refresh_activity_cache_quiet(self) -> None:
        """Load the latest activity snapshot into the cache without on_update."""
        try:
            snap = request_activity.load_snapshot()
            self._set_activity_cache_value(snap)
            self._activity_fingerprint = self._activity_fingerprint_of(snap)
        except Exception:
            logger.debug("quiet activity cache refresh failed", exc_info=True)

    def _on_non_macos_menu_will_open(self) -> None:
        """Win/Linux: refresh caches and rebuild the menu before it pops up.

        Unlike macOS, ``update_menu()`` is safe here — and required, because
        pystray does not re-evaluate label callables on open. Setting
        ``_menu_session_open`` also defers mid-popup redraws: Win32's
        ``_update_menu`` ``DestroyMenu``s the tracked ``hmenu``, which is
        unsafe during ``TrackPopupMenuEx``. While open, ``tray_live`` ticks
        and patches titles in place instead.
        """
        if self._menu_session_open:
            # Re-entrant (e.g. AppIndicator show → rebuild → show). Only
            # refresh caches; do not rebuild again.
            self._on_menu_open()
            self._refresh_activity_cache_quiet()
            return
        self._menu_session_open = True
        self._on_menu_open()
        self._refresh_activity_cache_quiet()
        tray_live.sync_rebuild_menu(self._icon)

    def _on_non_macos_menu_did_close(self) -> None:
        self._menu_session_open = False
        if self._redraw_deferred:
            self._request_redraw()

    def _on_non_macos_menu_tick(self) -> None:
        """1s tick while Win/Linux popup is open: in-place title refresh."""
        if not self._menu_session_open or sys.platform == "darwin":
            return
        try:
            snap = request_activity.load_snapshot()
            self._set_activity_cache_value(snap)
            fp = self._activity_fingerprint_of(snap)
            if fp != self._activity_fingerprint:
                self._activity_fingerprint = fp
                self._redraw_deferred = True
            self._live_patch_open_menu_crossplatform(snap)
        except Exception:
            logger.debug("non-macos menu live tick failed", exc_info=True)

    def _live_patch_open_menu_crossplatform(self, snap: ActivitySnapshot) -> None:
        """In-place Win/Linux title updates (no DestroyMenu / set_menu)."""
        gw = self.sup.status()["gateway"]
        if gw != "running":
            active_rows = [f"网关未运行（{_STATUS_ZH.get(gw, gw)}）"]
        elif not snap.active:
            active_rows = ["当前无进行中的请求"]
        else:
            now = time.time()
            active_rows = [
                request_activity.format_active_line(a, now=now) for a in snap.active
            ]

        if not snap.recent:
            recent_rows = ["暂无最近对话"]
        else:
            recent_rows = [request_activity.format_recent_line(r) for r in snap.recent]

        tray_live.apply_live_titles(
            self._icon,
            top_level_prefixes=[
                (_GATEWAY_PREFIX, self._gateway_title()),
                (_TUNNEL_PREFIX, self._tunnel_title()),
                (_ACTIVITY_ACTIVE_PREFIX, self._activity_active_title(snap)),
            ],
            submenu_by_parent_prefix={
                _ACTIVITY_ACTIVE_PREFIX: active_rows,
            },
            submenu_by_parent_exact={
                _ACTIVITY_RECENT_TITLE: recent_rows,
            },
        )

    def _on_status_menu_tick(self) -> None:
        """1s tick while menu is open: refresh elapsed titles without setMenu:."""
        if not self._menu_session_open:
            return
        try:
            self._live_patch_status_titles()
            snap = request_activity.load_snapshot()
            self._set_activity_cache_value(snap)
            fp = self._activity_fingerprint_of(snap)
            if fp != self._activity_fingerprint:
                self._activity_fingerprint = fp
                self._redraw_deferred = True
            self._live_patch_open_menu(snap)
        except Exception:
            logger.debug("status menu live tick failed", exc_info=True)

    def _on_supervisor_status_change(self) -> None:
        """Keep gateway/tunnel labels live even while the status menu is open."""
        if self._menu_session_open or macos_menu.is_status_menu_open(self._icon):
            self._redraw_deferred = True

            def _patch():
                if sys.platform == "darwin":
                    self._live_patch_status_titles()
                else:
                    snap = self._activity_snapshot()
                    self._live_patch_open_menu_crossplatform(snap)

            macos_menu.run_on_main_thread(_patch)
            return
        self._request_redraw()

    # --- redraw / notify helpers ---

    def _request_redraw(self) -> None:
        """Rebuild the tray NSMenu on the AppKit main thread.

        pystray's macOS backend ends in ``NSStatusItem.setMenu:``. On macOS 27+
        that path asserts the main-queue barrier; calling it from a worker
        thread hard-crashes with SIGTRAP and no Python traceback.

        If the status menu is currently open, defer the rebuild until it
        closes — replacing the menu mid-tracking hijacks keyboard input.
        """
        ic = self._icon
        if ic is None:
            return

        def _do():
            try:
                if self._menu_session_open or macos_menu.is_status_menu_open(ic):
                    self._redraw_deferred = True
                    return
                self._redraw_deferred = False
                ic.update_menu()
            except Exception:
                logger.debug("update_menu failed during redraw", exc_info=True)

        macos_menu.run_on_main_thread(_do)

    def _notify(self, title: str, msg: str) -> None:
        _notify_mod.notify(self._icon, title, msg)

    def _on_app_reopen(self) -> None:
        """macOS sends this when the running .app is opened again."""
        dialogs.alert(APP_NAME, "Kiro Gateway Tray 已在运行中，不允许启动多个实例。")

    def _refresh_icon(self) -> None:
        """Update the menu-bar glyph on the AppKit main thread.

        Setting ``icon.icon`` touches ``NSStatusItem.button().setImage_`` on
        macOS; same main-thread rule as ``update_menu``. Off macOS,
        ``run_on_main_thread`` runs inline, so Windows ThemeWatcher is fine.
        """
        ic = self._icon
        if ic is None:
            return

        def _do():
            try:
                ic.icon = make_icon(self.sup.status()["gateway"] == "running")
            except Exception:
                logger.debug("_refresh_icon failed", exc_info=True)

        macos_menu.run_on_main_thread(_do)

    def _on_theme_change(self, light_theme: bool) -> None:
        """Windows taskbar theme changed: re-render the icon for the new theme.

        Called from the ThemeWatcher daemon thread. ``_refresh_icon`` marshals
        to the main thread on macOS and runs inline elsewhere, so this is safe
        on every platform. ``light_theme`` is informational; ``_refresh_icon``
        re-detects via ``make_icon`` auto-detect, keeping a single path.
        """
        self._refresh_icon()

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
            # Never call icon.update_menu() here — worker thread; see _request_redraw.
            self._refresh_icon()
            self._request_redraw()
        threading.Thread(target=_work, daemon=True).start()

    def _on_stop(self, icon, _item):
        self.sup.stop()
        self._notify(APP_NAME, "网关已停止")
        self._refresh_icon()
        self._request_redraw()

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
        if self._theme_watcher is not None:
            self._theme_watcher.stop()
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

    def _on_open_speedtest(self, _icon, _item):
        cfg = appconfig.load(use_cache=True)
        webbrowser.open(_speedtest_url(cfg))

    def _speedtest_visible(self, _item) -> bool:
        # 仅在配了隧道时展示：本地打本地测不出"绕一圈"的开销，没意义。
        cfg = appconfig.load(use_cache=True)
        return bool(cfg.cloudflare.hostname)

    def _on_toggle_autostart(self, icon, _item):
        want = not autostart.is_enabled()
        try:
            autostart.set_enabled(want)
        except Exception as e:
            logger.exception("toggle autostart failed")
            self._notify(f"{APP_NAME} 错误", f"设置开机自启失败：{str(e)[:160]}")
            self._request_redraw()
            return
        if want:
            extra = ""
            if sys.platform == "darwin":
                extra = "\n可在「系统设置 → 通用 → 登录项」中管理。"
            self._notify(APP_NAME, f"已开启开机自启，下次登录将自动启动。{extra}")
        else:
            self._notify(APP_NAME, "已关闭开机自启。")
        self._request_redraw()

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
        return self._gateway_title()

    def _tunnel_line(self, _item):
        return self._tunnel_title()

    def _usage_line(self, _item):
        gw = self.sup.status()["gateway"]
        if gw != "running":
            return f"📊 额度: ({_STATUS_ZH.get(gw, gw)})"
        self._usage_cache.refresh()
        return f"📊 额度: {self._usage_cache.display()}"

    def _activity_snapshot(self) -> ActivitySnapshot:
        snap = self._activity_cache.get()
        return snap if snap is not None else ActivitySnapshot()

    def _activity_active_line(self, _item):
        if self.sup.status()["gateway"] == "running":
            self._activity_cache.refresh()
        return self._activity_active_title()

    def _activity_active_submenu(self):
        pystray = self._pystray
        gw = self.sup.status()["gateway"]
        if gw != "running":
            label = _STATUS_ZH.get(gw, gw)
            return [pystray.MenuItem(f"网关未运行（{label}）", None, enabled=False)]
        self._activity_cache.refresh()
        snap = self._activity_snapshot()
        if not snap.active:
            return [pystray.MenuItem("当前无进行中的请求", None, enabled=False)]
        now = time.time()
        return [
            pystray.MenuItem(
                request_activity.format_active_line(a, now=now),
                self._on_copy_activity_text(
                    a.question_preview or a.model,
                    "进行中请求",
                ),
            )
            for a in snap.active
        ]

    def _activity_recent_submenu(self):
        pystray = self._pystray
        self._activity_cache.refresh()
        snap = self._activity_snapshot()
        if not snap.recent:
            return [pystray.MenuItem("暂无最近对话", None, enabled=False)]
        items = []
        for r in snap.recent:
            detail = (
                f"问: {r.question_preview or '（无）'}\n"
                f"答: {r.answer_preview or '（无）'}"
                if r.ok
                else (
                    f"问: {r.question_preview or '（无）'}\n"
                    f"错误: {r.error_preview or '失败'}"
                )
            )
            items.append(
                pystray.MenuItem(
                    request_activity.format_recent_line(r),
                    self._on_copy_activity_text(detail, "最近对话"),
                )
            )
        return items

    def _on_copy_activity_text(self, text: str, label: str):
        def _handler(_icon, _item):
            self._copy(text, label)
        return _handler

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
        regular, aliases = usage.split_models_for_menu(items)
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
        status = updates.version_status(__version__)
        if not status.latest:
            return f"{line}\t检查中…"
        if status.upgradable:
            return f"{line}\t可升级 {status.latest.lstrip('vV')}"
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

    def _start_activity_refresh_loop(self) -> None:
        """Keep in-flight / recent request rows live on the tray menu.

        Same macOS static-NSMenu constraint as usage: without a background tick,
        newly started/finished conversations would freeze until some other
        status change forced a redraw. Elapsed-time labels are only refreshed
        when the snapshot contents change (or another redraw happens) — we
        deliberately do not call update_menu on a timer while requests are
        in flight, because that races with an open status menu.
        """
        def _loop():
            while not self._usage_refresh_stop.wait(self._ACTIVITY_REFRESH_INTERVAL):
                if self.sup.status()["gateway"] != "running":
                    # Still flush a deferred redraw after the menu closes.
                    if self._redraw_deferred:
                        self._request_redraw()
                    continue
                try:
                    self._activity_cache.refresh(force=True)
                except Exception:
                    logger.debug("activity refresh tick failed", exc_info=True)
                if self._redraw_deferred:
                    self._request_redraw()

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
            pystray.MenuItem(
                self._activity_active_line,
                pystray.Menu(self._activity_active_submenu),
            ),
            pystray.MenuItem(
                _ACTIVITY_RECENT_TITLE,
                pystray.Menu(self._activity_recent_submenu),
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
            pystray.MenuItem("📶 隧道网络测速", self._on_open_speedtest, visible=self._speedtest_visible),
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
            # _startup runs on a daemon thread; AppKit setMenu:/setImage_ must
            # hit the main queue (macOS 27 asserts otherwise → SIGTRAP flash-quit).
            self._refresh_icon()
            self._request_redraw()
            self._models_cache.refresh(force=True)

        self.sup.mark_starting()
        menu = self._build_menu()
        self._icon = pystray.Icon("kiro-gateway-tray", make_icon(False), "Kiro Gateway", menu)
        macos_menu.install_retina_icon_fix()
        macos_menu.install_menu_gray_suffix()
        macos_menu.install_live_status_menu(
            on_will_open=self._on_status_menu_will_open,
            on_did_close=self._on_status_menu_did_close,
            on_tick=self._on_status_menu_tick,
            tick_interval=1.0,
        )
        tray_live.install_open_refresh(
            on_will_open=self._on_non_macos_menu_will_open,
            on_did_close=self._on_non_macos_menu_did_close,
            on_tick=self._on_non_macos_menu_tick,
            tick_interval=1.0,
        )
        macos_menu.install_reopen_handler(self._icon, self._on_app_reopen)
        self._attach_submenu_autorebuild()
        threading.Thread(target=_startup, daemon=True).start()
        self._kick_update_check()
        self._start_usage_refresh_loop()
        self._start_activity_refresh_loop()
        # Windows-only taskbar theme auto-adaptation. start() is a no-op on
        # macOS/Linux, so it's safe to call unconditionally.
        self._theme_watcher = ThemeWatcher(self._on_theme_change)
        self._theme_watcher.start()
        self._icon.run()


def run() -> None:
    """Start the tray loop. Raises TrayUnavailable if no backend works."""
    try:
        import pystray  # noqa: F401 — ensure importable before constructing TrayApp
    except Exception as e:
        raise TrayUnavailable(str(e))
    TrayApp().run()
