# app/kiro_gateway_tray/supervisor.py
"""Orchestrate gateway + cloudflared and handle first-run provisioning."""
from __future__ import annotations

import sys
import threading
import time
from typing import Callable

import httpx

from . import appconfig
from .log import logger
from .appconfig import AppCfg
from .gateway import GatewayProcess
from .cloudflared import CloudflaredProcess


class Supervisor:
    # consecutive failed /health probes before flipping "starting" -> "error"
    _UNHEALTHY_THRESHOLD = 5
    # health probe cadence: tight while settling, relaxed once steady-running
    _PROBE_INTERVAL_ACTIVE = 3      # seconds, while starting/unhealthy
    _PROBE_INTERVAL_STEADY = 15     # seconds, once consistently running

    def __init__(self, gateway=None, tunnel=None) -> None:
        self.gateway = gateway or GatewayProcess()
        self.tunnel = tunnel or CloudflaredProcess()
        self._cfg: AppCfg | None = None
        self._cached_secret: str | None = None
        self.provision_callback: Callable[[AppCfg], str] | None = None
        # Cached gateway health status (never block the main/UI thread)
        self._gw_health: str = "stopped"
        # Cached tunnel connectivity, refreshed by the health loop via the
        # cloudflared /ready probe (not by parsing logs).
        self._tunnel_connected: bool = False
        # Fired whenever gateway health or tunnel connectivity changes, so the
        # tray can redraw the menu the moment the tunnel comes up (instead of
        # waiting for the next time the user opens the menu).
        self.on_status_change: Callable[[], None] | None = None
        self._health_thread: threading.Thread | None = None
        self._health_stop = threading.Event()
        # Reused connection pool for localhost health/usage probes.
        self._client = httpx.Client(timeout=3)

    def _load(self) -> AppCfg:
        self._cfg = appconfig.load()
        # Restore the activation secret persisted at registration time so that
        # update-port works across restarts (not just within the session that
        # first provisioned). Falls back to any in-session value.
        if self._cfg.cloudflare.shared_secret:
            self._cached_secret = self._cfg.cloudflare.shared_secret
        return self._cfg

    def _wait_healthy(self, timeout: int = 30) -> bool:
        cfg = self._cfg or self._load()
        url = f"{appconfig.gateway_origin(cfg)}/health"
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                if self._client.get(url, timeout=3).status_code == 200:
                    return True
            except httpx.HTTPError:
                pass
            time.sleep(1)
        return False

    def _ensure_provisioned(self, cfg: AppCfg) -> None:
        """Run provision flow if not yet registered. Updates cfg and saves."""
        if appconfig.is_provisioned(cfg):
            return
        if self.provision_callback is None:
            raise RuntimeError(
                "未完成首启注册，且没有注入 provision_callback。\n"
                "请先在 config.toml 填写 [cloudflare] provision_url，"
                "然后重新启动 App 完成激活。"
            )
        shared_secret = self.provision_callback(cfg)
        self.register(cfg, shared_secret)

    def register(self, cfg: AppCfg, shared_secret: str) -> None:
        """Call the Worker with the shared secret, then persist tunnel creds.

        Shared by the CLI (via _ensure_provisioned) and the tray, which must run
        its dialogs on the main thread before the tray loop starts."""
        self._cached_secret = shared_secret
        from . import provision
        hostname, run_token = provision.run(cfg, shared_secret)
        cfg.cloudflare.hostname = hostname
        cfg.cloudflare.run_token = run_token
        cfg.cloudflare.registered_port = cfg.gateway.port
        # Persist the secret so port-sync survives restarts. Safe-ish: config is
        # chmod 0600 on POSIX. See appconfig.save().
        cfg.cloudflare.shared_secret = shared_secret
        appconfig.save(cfg)

    def _sync_port_if_changed(self, cfg: AppCfg) -> None:
        """If local port differs from the one registered with Worker, update it."""
        if not appconfig.is_provisioned(cfg):
            return
        if cfg.cloudflare.registered_port == cfg.gateway.port:
            return
        secret = self._cached_secret or cfg.cloudflare.shared_secret
        if not secret:
            # No secret available (older config registered before secrets were
            # persisted). Can't re-sync the port; the tunnel keeps pointing at
            # the old port. Surface it so this isn't a silent 502 mystery.
            msg = (
                "本地端口已变更但无法同步到 Worker（缺少激活码缓存）。"
                f"请重新激活，或将端口改回 {cfg.cloudflare.registered_port}。"
            )
            print(f"[kiro-gateway-tray] {msg}", file=sys.stderr)
            logger.warning(msg)
            return
        from . import provision
        try:
            effective_port = provision.update_port(cfg, secret)
            cfg.cloudflare.registered_port = effective_port
            appconfig.save(cfg)
            logger.info("synced tunnel port to {} via Worker", effective_port)
        except Exception as e:
            print(f"[kiro-gateway-tray] update-port 失败: {e}", file=sys.stderr)
            logger.exception("update-port failed")

    def start(self) -> bool:
        cfg = self._load()
        self._ensure_provisioned(cfg)
        self._sync_port_if_changed(cfg)
        self._gw_health = "starting"
        self.gateway.start(cfg)
        healthy = self._wait_healthy()
        if healthy:
            self._gw_health = "running"
        self.tunnel.start(cfg)
        self._start_health_loop()
        return healthy

    def stop(self) -> None:
        self._health_stop.set()
        self.tunnel.stop()
        self.gateway.stop()
        self._gw_health = "stopped"
        self._tunnel_connected = False

    def restart(self) -> bool:
        self.stop()
        return self.start()

    def _start_health_loop(self) -> None:
        """Start a background thread that probes gateway /health.

        Probes every ``_PROBE_INTERVAL_ACTIVE`` seconds while settling; once the
        gateway has been answering steadily it backs off to
        ``_PROBE_INTERVAL_STEADY`` to avoid needless wakeups. While the process
        is alive but not yet answering, the state is "starting"; after
        ``_UNHEALTHY_THRESHOLD`` consecutive failed probes it flips to "error"
        so a wedged gateway (bad port, bad profile_arn) is visible instead of
        spinning on "starting" forever."""
        if self._health_thread and self._health_thread.is_alive():
            return
        self._health_stop.clear()

        def _loop():
            consecutive_failures = 0
            consecutive_ok = 0
            while not self._health_stop.is_set():
                interval = self._PROBE_INTERVAL_ACTIVE
                prev_gw = self._gw_health
                prev_tunnel = self._tunnel_connected
                if not self.gateway.is_alive():
                    self._gw_health = "stopped"
                    consecutive_failures = 0
                    consecutive_ok = 0
                else:
                    cfg = self._cfg or self._load()
                    url = f"{appconfig.gateway_origin(cfg)}/health"
                    healthy = False
                    try:
                        healthy = self._client.get(url, timeout=1).status_code == 200
                    except Exception:
                        healthy = False
                    if healthy:
                        self._gw_health = "running"
                        consecutive_failures = 0
                        consecutive_ok += 1
                        # Back off only after the gateway has proven stable.
                        if consecutive_ok >= 2:
                            interval = self._PROBE_INTERVAL_STEADY
                    else:
                        consecutive_ok = 0
                        consecutive_failures += 1
                        self._gw_health = (
                            "error"
                            if consecutive_failures >= self._UNHEALTHY_THRESHOLD
                            else "starting"
                        )
                self._tunnel_connected = self._probe_tunnel_ready()
                # Notify the UI only on an actual state transition so we don't
                # spin the menu redraw on every probe.
                if (self._gw_health != prev_gw
                        or self._tunnel_connected != prev_tunnel):
                    self._fire_status_change()
                self._health_stop.wait(interval)

        self._health_thread = threading.Thread(target=_loop, daemon=True)
        self._health_thread.start()

    def _probe_tunnel_ready(self) -> bool:
        """True if cloudflared reports at least one live edge connection.

        Probes the metrics /ready endpoint (200 = ready, 503 = not yet). Any
        error (process down, port not up yet) counts as not-ready. Uses the port
        cloudflared actually bound (which may differ from the configured one if
        that was busy), not the config value."""
        if not self.tunnel.is_alive():
            return False
        try:
            return self._client.get(
                f"http://127.0.0.1:{self.tunnel.metrics_port}/ready", timeout=1
            ).status_code == 200
        except Exception:
            return False

    def _fire_status_change(self) -> None:
        cb = self.on_status_change
        if cb is None:
            return
        try:
            cb()
        except Exception:
            logger.debug("on_status_change callback failed", exc_info=True)

    def _tunnel_status(self) -> str:
        if not self.tunnel.is_alive():
            return "stopped"
        return "running" if self._tunnel_connected else "connecting"

    def status(self) -> dict[str, str]:
        """Non-blocking: reads cached health state, never does I/O."""
        cfg = self._cfg or self._load()
        provisioned = appconfig.is_provisioned(cfg)
        return {
            "gateway": self._gw_health,
            "tunnel": self._tunnel_status(),
            "hostname": cfg.cloudflare.hostname if provisioned else "(未注册)",
        }
