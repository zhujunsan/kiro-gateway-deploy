# app/kiro_gateway_tray/supervisor.py
"""Orchestrate gateway + cloudflared and handle first-run provisioning."""
from __future__ import annotations

import sys
import threading
import time
from typing import Callable

import httpx

from . import appconfig
from . import gateway
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
    # automatically restart cloudflared if stuck in connecting/disconnected state for this long (seconds)
    _TUNNEL_RECONNECT_TIMEOUT = 60
    # how long restart() waits for the old gateway's port to free before
    # starting the new child (seconds)
    _PORT_FREE_TIMEOUT = 10

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
        # Timestamp since when the tunnel has been disconnected while running
        self._tunnel_disconnected_since: float | None = None
        # Failure/success run-lengths feeding the health state machine. Kept on
        # the instance (not as loop-locals) so the on-demand probe_now() and the
        # background loop drive ONE shared state machine instead of two that
        # fight over _gw_health.
        self._consecutive_failures = 0
        self._consecutive_ok = 0
        # Fired whenever gateway health or tunnel connectivity changes, so the
        # tray can redraw the menu the moment the tunnel comes up (instead of
        # waiting for the next time the user opens the menu).
        self.on_status_change: Callable[[], None] | None = None
        self._health_thread: threading.Thread | None = None
        self._health_stop = threading.Event()
        # Guards the cached status fields (_gw_health, _tunnel_connected,
        # counters) so status() reads never tear against a probe's write.
        self._state_lock = threading.Lock()
        # Serializes whole probe-and-update cycles so the loop and probe_now
        # can't interleave into the shared state machine.
        self._probe_lock = threading.Lock()
        # Guards config (re)loads so concurrent callers don't double-read disk.
        self._cfg_lock = threading.Lock()
        # Reused connection pool for localhost health/usage probes.
        # trust_env=False: probes always hit 127.0.0.1, but httpx does not bypass
        # localhost for HTTP(S)_PROXY; a system proxy would otherwise route these
        # to a proxy and make a healthy gateway/tunnel look down.
        self._client = httpx.Client(timeout=3, trust_env=False)

    def _load(self) -> AppCfg:
        with self._cfg_lock:
            self._cfg = appconfig.load()
            # Restore the activation secret persisted at registration time so
            # that update-port works across restarts (not just within the
            # session that first provisioned). Falls back to any in-session value.
            if self._cfg.cloudflare.shared_secret:
                self._cached_secret = self._cfg.cloudflare.shared_secret
            return self._cfg

    def _get_cfg(self) -> AppCfg:
        """Return the cached config, loading it once if not yet present."""
        cfg = self._cfg
        return cfg if cfg is not None else self._load()

    def close(self) -> None:
        """Stop the health loop and release the HTTP connection pool.

        Call once on final teardown (app quit), not on stop(): stop() may be
        followed by start() again (restart), which reuses the client.
        """
        self._health_stop.set()
        try:
            self._client.close()
        except Exception:
            logger.debug("closing supervisor http client failed", exc_info=True)

    def _wait_healthy(self, timeout: int = 30) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._probe_gateway_once(timeout=3):
                return True
            time.sleep(1)
        return False

    def _probe_gateway_once(self, *, timeout: float = 1) -> bool:
        """One gateway /health probe. True iff it answered 200. Never raises."""
        cfg = self._get_cfg()
        url = f"{appconfig.gateway_origin(cfg)}/health"
        try:
            return self._client.get(url, timeout=timeout).status_code == 200
        except Exception:
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
        hostname, run_token, telemetry_secret = provision.run(cfg, shared_secret)
        cfg.cloudflare.hostname = hostname
        cfg.cloudflare.run_token = run_token
        cfg.cloudflare.registered_port = cfg.gateway.port
        # Persist the secret so port-sync survives restarts. Safe-ish: config is
        # chmod 0600 on POSIX. See appconfig.save().
        cfg.cloudflare.shared_secret = shared_secret
        # First-dispatch of the telemetry pre-shared key (design §8). The Worker
        # only returns it when configured; don't clobber an existing value with
        # an empty one (e.g. older Worker, or re-provision after rotation).
        if telemetry_secret:
            cfg.telemetry.secret = telemetry_secret
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

    def _reprovision_if_deleted(self) -> bool:
        """Check if the cloud tunnel was deleted; if so, re-provision silently.

        Returns True if a re-provision was performed (tunnel restarted with new
        token), False otherwise (caller should do a plain restart).
        """
        cfg = self._get_cfg()
        secret = self._cached_secret or cfg.cloudflare.shared_secret
        if not secret:
            return False
        from . import provision
        exists = provision.tunnel_exists(cfg, secret)
        if exists is not False:
            return False
        logger.warning("cloud tunnel deleted; re-provisioning with stored activation code")
        try:
            self.register(cfg, secret)
            cfg = self._load()
            self.tunnel.stop()
            self.tunnel.start(cfg)
            return True
        except Exception:
            logger.exception("re-provision after tunnel deletion failed")
            return False

    def start(self) -> bool:
        cfg = self._load()
        self._ensure_provisioned(cfg)
        self._sync_port_if_changed(cfg)
        with self._state_lock:
            self._gw_health = "starting"
        self.gateway.start(cfg)
        healthy = self._wait_healthy()
        if healthy:
            with self._state_lock:
                self._gw_health = "running"
                self._consecutive_ok = 1
        self.tunnel.start(cfg)
        self._start_health_loop()
        return healthy

    def stop(self) -> None:
        self._health_stop.set()
        self.tunnel.stop()
        self.gateway.stop()
        with self._state_lock:
            self._gw_health = "stopped"
            self._tunnel_connected = False
            self._consecutive_failures = 0
            self._consecutive_ok = 0
            self._tunnel_disconnected_since = None

    def restart(self) -> bool:
        self.stop()
        # stop() already waits for the old gateway child to exit, but the OS can
        # hold its listening socket open a beat longer. Starting the new child
        # before the port frees would make uvicorn fail to bind. Poll until the
        # port is bindable (bounded wait); proceed anyway on timeout so a stuck
        # external listener doesn't wedge restart forever — start() surfaces the
        # bind failure via the health probe.
        cfg = self._get_cfg()
        if not gateway.wait_port_free(cfg.gateway.port, timeout=self._PORT_FREE_TIMEOUT):
            logger.warning(
                "gateway port {} still busy after {}s; starting anyway",
                cfg.gateway.port,
                self._PORT_FREE_TIMEOUT,
            )
        return self.start()

    def mark_starting(self) -> None:
        """Optimistically show "starting" before the gateway is actually up.

        Lets the UI give immediate feedback right after launch / setup dialogs
        instead of briefly showing "stopped"."""
        with self._state_lock:
            self._gw_health = "starting"

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
            while not self._health_stop.is_set():
                relaxed = self._run_probe_cycle()
                interval = (
                    self._PROBE_INTERVAL_STEADY if relaxed
                    else self._PROBE_INTERVAL_ACTIVE
                )
                self._health_stop.wait(interval)

        self._health_thread = threading.Thread(target=_loop, daemon=True)
        self._health_thread.start()

    def probe_now(self) -> bool:
        """Run one gateway+tunnel health probe immediately, off the loop cadence.

        Lets the tray refresh status the instant the user opens the menu instead
        of waiting for the next scheduled probe. Non-blocking-safe to call from a
        background thread; fires on_status_change only on an actual transition.
        Returns True if any cached state changed.
        """
        before = self._state_snapshot()
        self._run_probe_cycle()
        return self._state_snapshot() != before

    def _state_snapshot(self) -> tuple[str, bool]:
        with self._state_lock:
            return (self._gw_health, self._tunnel_connected)

    def _run_probe_cycle(self) -> bool:
        """Run ONE gateway+tunnel probe and advance the shared state machine.

        Serialized by ``_probe_lock`` so the background loop and an on-demand
        ``probe_now`` can't interleave half-updates into the one state machine.
        Returns True when the caller should use the relaxed cadence (gateway
        down, or running steadily). Fires ``on_status_change`` only on an actual
        transition.
        """
        with self._probe_lock:
            with self._state_lock:
                prev_gw = self._gw_health
                prev_tunnel = self._tunnel_connected

            alive = self.gateway.is_alive()
            healthy = self._probe_gateway_once() if alive else False
            tunnel_alive = self.tunnel.is_alive()
            tunnel_connected = self._probe_tunnel_ready()

            should_restart_tunnel = False
            with self._state_lock:
                if not tunnel_alive or tunnel_connected:
                    self._tunnel_disconnected_since = None
                else:
                    if self._tunnel_disconnected_since is None:
                        self._tunnel_disconnected_since = time.time()
                    elif time.time() - self._tunnel_disconnected_since > self._TUNNEL_RECONNECT_TIMEOUT:
                        should_restart_tunnel = True
                        self._tunnel_disconnected_since = None

            if should_restart_tunnel:
                logger.warning(
                    "Cloudflare tunnel has been disconnected for more than {}s; checking status...",
                    self._TUNNEL_RECONNECT_TIMEOUT
                )
                try:
                    reprovisioned = self._reprovision_if_deleted()
                    if not reprovisioned:
                        self.tunnel.stop()
                        cfg = self._get_cfg()
                        self.tunnel.start(cfg)
                except Exception:
                    logger.exception("Failed to restart cloudflared tunnel")
                tunnel_connected = False

            with self._state_lock:
                if not alive:
                    self._gw_health = "stopped"
                    self._consecutive_failures = 0
                    self._consecutive_ok = 0
                    # Nothing is settling when the gateway is down: relax cadence.
                    relaxed = True
                elif healthy:
                    self._gw_health = "running"
                    self._consecutive_failures = 0
                    self._consecutive_ok += 1
                    # Back off only after the gateway has proven stable.
                    relaxed = self._consecutive_ok >= 2
                else:
                    self._consecutive_ok = 0
                    self._consecutive_failures += 1
                    self._gw_health = (
                        "error"
                        if self._consecutive_failures >= self._UNHEALTHY_THRESHOLD
                        else "starting"
                    )
                    relaxed = False
                self._tunnel_connected = tunnel_connected
                changed = (self._gw_health != prev_gw
                           or self._tunnel_connected != prev_tunnel)

        if changed:
            self._fire_status_change()
        return relaxed

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
        with self._state_lock:
            connected = self._tunnel_connected
        return "running" if connected else "connecting"

    def status(self) -> dict[str, str]:
        """Non-blocking: reads cached health state, never does I/O."""
        cfg = self._get_cfg()
        provisioned = appconfig.is_provisioned(cfg)
        with self._state_lock:
            gw_health = self._gw_health
        return {
            "gateway": gw_health,
            "tunnel": self._tunnel_status(),
            "hostname": cfg.cloudflare.hostname if provisioned else "(未注册)",
        }
