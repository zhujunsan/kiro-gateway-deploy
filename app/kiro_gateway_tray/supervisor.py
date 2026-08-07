# app/kiro_gateway_tray/supervisor.py
"""Orchestrate gateway + cloudflared and handle first-run provisioning."""
from __future__ import annotations

import sys
import threading
import time
from typing import Callable

from . import appconfig
from . import gateway
from .log import logger
from .httpclient import local_client, resolve_proxy
from .appconfig import AppCfg
from .gateway import GatewayProcess
from .cloudflared import CloudflaredProcess


class Supervisor:
    # consecutive failed /health probes before flipping "starting" -> "error"
    _UNHEALTHY_THRESHOLD = 5
    # health probe cadence: tight while settling, relaxed once steady-running
    _PROBE_INTERVAL_ACTIVE = 3      # seconds, while starting/unhealthy
    _PROBE_INTERVAL_STEADY = 15     # seconds, once consistently running
    # escalate to process restart if still at zero connections this long after
    # the first soft reconnect (seconds). Kept short: once readyConnections is
    # already 0 the tunnel is offline either way; waiting only delays recovery.
    _TUNNEL_RECONNECT_TIMEOUT = 5
    # how long restart() waits for the old gateway's port to free before
    # starting the new child (seconds)
    _PORT_FREE_TIMEOUT = 10
    # brief wait on cold start after orphan cleanup, before failing on a busy port
    _PORT_FREE_START_TIMEOUT = 3
    # end-to-end probe interval (seconds): after a successful probe, don't
    # probe again until this much time passes to avoid hitting public edge.
    _E2E_PROBE_INTERVAL = 60
    # seconds between soft reconnect attempts (cooldown)
    _TUNNEL_RECONNECT_COOLDOWN = 10
    # seconds to wait after a soft reconnect before escalating to process restart
    # (network-change path). Aligned with _TUNNEL_RECONNECT_TIMEOUT.
    _SOFT_RECONNECT_VERIFY_WINDOW = 5
    # consecutive failed end-to-end probes before asking for a soft reconnect.
    # >1 so a single transient blip on the public path (DNS hiccup, edge PoP
    # reshuffle, captive-portal moment) doesn't churn the tunnel.
    _E2E_FAILURE_THRESHOLD = 2
    # the only tunnel status that counts as settled for cadence purposes;
    # anything else (connecting / degraded / stopped) means recovery is in
    # progress and the loop must keep probing on the active cadence.
    _TUNNEL_SETTLED_STATUS = "running"

    def __init__(self, gateway=None, tunnel=None) -> None:
        self.gateway = gateway or GatewayProcess()
        self.tunnel = tunnel or CloudflaredProcess()
        self._cfg: AppCfg | None = None
        self._cached_secret: str | None = None
        self.provision_callback: Callable[[AppCfg], str] | None = None
        # Cached gateway health status (never block the main/UI thread)
        self._gw_health: str = "stopped"
        # Last user-facing gateway failure reason (Chinese); cleared on success.
        self._last_error: str | None = None
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
        # --- Tunnel health model fields ---
        self._tunnel_conns: int = 0
        self._tunnel_conns_expected: int = 0
        self._tunnel_e2e_ok: bool | None = None
        self._tunnel_degraded_reason: str = ""
        self._last_e2e_success_ts: float = 0.0
        self._last_tunnel_reconnect_ts: float = 0.0
        # Run-length of end-to-end probes that actually ran and failed. Drives
        # the "edge unreachable but cloudflared thinks it is connected" recovery
        # path; only real probe results move it (a throttled skip does not).
        self._e2e_consecutive_failures: int = 0
        # Fired whenever gateway health or tunnel connectivity changes, so the
        # tray can redraw the menu the moment the tunnel comes up (instead of
        # waiting for the next time the user opens the menu).
        self.on_status_change: Callable[[], None] | None = None
        self._health_thread: threading.Thread | None = None
        self._health_stop = threading.Event()
        # Coordinates stop/join/start without holding this lock while joining.
        # A timed-out old loop blocks replacement instead of being silently
        # revived by clearing its shared Event during an immediate restart.
        self._health_lifecycle_lock = threading.Lock()
        self._health_stopping = False
        # Guards the cached status fields (_gw_health, _tunnel_connected,
        # counters) so status() reads never tear against a probe's write.
        self._state_lock = threading.Lock()
        # Serializes whole probe-and-update cycles so the loop and probe_now
        # can't interleave into the shared state machine.
        self._probe_lock = threading.Lock()
        # Guards config (re)loads so concurrent callers don't double-read disk.
        self._cfg_lock = threading.Lock()
        # Reused connection pool for localhost health/usage probes. See
        # httpclient.local_client for the trust_env=False rationale: probes
        # always hit 127.0.0.1, but httpx does not bypass localhost for
        # HTTP(S)_PROXY; a system proxy would otherwise route these to a proxy
        # and make a healthy gateway/tunnel look down.
        self._client = local_client(timeout=3)
        # Network change watcher (started by _start_health_loop)
        self._network_watcher: object | None = None

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
        self._stop_health_loop()
        watcher = self._network_watcher
        if watcher is not None:
            watcher.stop()
            self._network_watcher = None
        try:
            self._client.close()
        except Exception:
            logger.debug("closing supervisor http client failed", exc_info=True)

    def _wait_healthy(self, timeout: int = 30) -> bool:
        """Poll /health until ready, or fail fast if the child already exited."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._probe_gateway_once(timeout=3):
                return True
            # Bind failures (EADDRINUSE) make uvicorn exit immediately; don't
            # burn the full timeout pretending the gateway is still "starting".
            if not self.gateway.is_alive():
                return False
            time.sleep(1)
        return False

    @property
    def last_error(self) -> str | None:
        """Most recent user-facing gateway start failure, or None when healthy."""
        with self._state_lock:
            return self._last_error

    def _set_gateway_error(self, message: str) -> None:
        """Flip cached health to error and remember the user-facing reason."""
        with self._state_lock:
            self._gw_health = "error"
            self._last_error = message
            self._consecutive_failures = 0
            self._consecutive_ok = 0

    def _diagnose_start_failure(self, cfg: AppCfg) -> str:
        """Build an actionable Chinese message after a failed health wait."""
        port = cfg.gateway.port
        if not gateway.can_bind("127.0.0.1", port):
            return gateway.format_port_busy_message(port)
        if not self.gateway.is_alive():
            return (
                "网关进程启动后异常退出，未能提供服务。"
                "请查看日志目录中的 gateway-bootstrap.log 与 gateway.log。"
            )
        return (
            "网关未能在时限内就绪（健康检查失败）。"
            "请查看日志目录或尝试重新启动。"
        )

    def _ensure_port_free_or_raise(self, cfg: AppCfg, *, timeout: float) -> None:
        """Wait briefly for the listen port; raise PortBusyError if still taken.

        Never picks an alternate port — Worker/docs pin the gateway port.
        """
        port = cfg.gateway.port
        if gateway.wait_port_free(port, timeout=timeout):
            return
        msg = gateway.format_port_busy_message(port)
        self._set_gateway_error(msg)
        logger.error(msg)
        raise gateway.PortBusyError(port)

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

    def _sync_telemetry_secret(self, cfg: AppCfg) -> AppCfg:
        """Best-effort parent-side telemetry secret refresh before child launch.

        The child may still refresh after a 401, but it only changes its runtime
        uploader. Persisting here makes the tray parent the sole config writer
        for rotations and keeps startup non-blocking on Worker failures.
        """
        provision_url = cfg.cloudflare.provision_url
        shared_secret = self._cached_secret or cfg.cloudflare.shared_secret
        if not provision_url or not shared_secret:
            return cfg
        try:
            from . import provision
            username = provision._get_username(cfg)
            secret = provision.refresh_telemetry_secret(
                provision_url, shared_secret, username
            )
        except Exception:
            logger.debug("telemetry secret startup sync failed", exc_info=True)
            return cfg
        if not secret or secret == cfg.telemetry.secret:
            return cfg
        try:
            cfg = appconfig.update(
                lambda latest: setattr(latest.telemetry, "secret", secret)
            )
            self._cfg = cfg
            logger.info("synchronized telemetry secret before gateway launch")
        except Exception:
            logger.debug("telemetry secret startup save failed", exc_info=True)
        return cfg

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
        cfg = self._sync_telemetry_secret(cfg)
        with self._state_lock:
            self._gw_health = "starting"
            self._last_error = None
        # Orphan cleanup may have just signalled a leftover child; give the OS a
        # short window to release the port. If something else still holds it,
        # fail fast with a clear message — never auto-change the port.
        self._ensure_port_free_or_raise(cfg, timeout=self._PORT_FREE_START_TIMEOUT)
        self.gateway.start(cfg)
        healthy = self._wait_healthy()
        if healthy:
            with self._state_lock:
                self._gw_health = "running"
                self._consecutive_ok = 1
                self._last_error = None
        else:
            msg = self._diagnose_start_failure(cfg)
            self._set_gateway_error(msg)
            logger.error("gateway failed to become healthy: {}", msg)
        self.tunnel.start(cfg)
        with self._state_lock:
            self._tunnel_conns_expected = 0
        self._start_health_loop()
        return healthy

    def stop(self) -> None:
        self._stop_health_loop()
        self.tunnel.stop()
        self.gateway.stop()
        with self._state_lock:
            self._gw_health = "stopped"
            self._last_error = None
            self._tunnel_connected = False
            self._consecutive_failures = 0
            self._consecutive_ok = 0
            self._tunnel_disconnected_since = None
            self._tunnel_conns = 0
            self._tunnel_conns_expected = 0
            self._tunnel_e2e_ok = None
            self._tunnel_degraded_reason = ""
            self._e2e_consecutive_failures = 0

    def restart(self) -> bool:
        self.stop()
        # stop() already waits for the old gateway child to exit, but the OS can
        # hold its listening socket open a beat longer. Starting the new child
        # before the port frees would make uvicorn fail to bind. Poll until the
        # port is bindable; if an external listener still holds it, fail fast
        # with a clear error instead of spawning a child that cannot bind.
        cfg = self._get_cfg()
        self._ensure_port_free_or_raise(cfg, timeout=self._PORT_FREE_TIMEOUT)
        return self.start()

    def mark_starting(self) -> None:
        """Optimistically show "starting" before the gateway is actually up.

        Lets the UI give immediate feedback right after launch / setup dialogs
        instead of briefly showing "stopped"."""
        with self._state_lock:
            self._gw_health = "starting"

    def _stop_health_loop(self, *, timeout: float = 5.0) -> bool:
        """Signal the health loop, join it outside the lifecycle lock, and clear it.

        Returns ``False`` when a probe is wedged beyond ``timeout``. In that
        case the live thread remains recorded and a later start refuses to clear
        its stop Event, preventing an old loop from being accidentally revived.
        """
        watcher = self._network_watcher
        if watcher is not None:
            watcher.stop()
            self._network_watcher = None
        with self._health_lifecycle_lock:
            self._health_stopping = True
            self._health_stop.set()
            thread = self._health_thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=timeout)
        if thread is not None and thread.is_alive():
            logger.warning(
                "supervisor health loop did not exit within {}s; refusing replacement",
                timeout,
            )
            return False
        with self._health_lifecycle_lock:
            if self._health_thread is thread:
                self._health_thread = None
            self._health_stopping = False
        return True

    def _start_health_loop(self) -> bool:
        """Start the health loop unless a prior loop is still shutting down."""
        with self._health_lifecycle_lock:
            thread = self._health_thread
            if thread is not None and thread.is_alive():
                if self._health_stopping or self._health_stop.is_set():
                    logger.warning("health loop is still stopping; not starting a replacement")
                    return False
                return True
            self._health_thread = None
            self._health_stopping = False
            self._health_stop.clear()

            def _loop() -> None:
                while not self._health_stop.is_set():
                    relaxed = self._run_probe_cycle()
                    interval = (
                        self._PROBE_INTERVAL_STEADY if relaxed
                        else self._PROBE_INTERVAL_ACTIVE
                    )
                    self._health_stop.wait(interval)

            thread = threading.Thread(target=_loop, name="supervisor-health", daemon=True)
            self._health_thread = thread
            thread.start()

        # Start network watcher for instant reconnect on network changes
        from .network_watch import NetworkWatcher
        if self._network_watcher is None:
            self._network_watcher = NetworkWatcher(
                on_change=lambda: self.request_tunnel_reconnect("network_change")
            )
            self._network_watcher.start()
        return True

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

    def _state_snapshot(self) -> tuple:
        with self._state_lock:
            return (
                self._gw_health,
                self._tunnel_connected,
                self._tunnel_conns,
                self._tunnel_conns_expected,
                self._tunnel_e2e_ok,
            )

    def _run_probe_cycle(self) -> bool:
        """Run ONE gateway+tunnel probe and advance the shared state machine.

        Serialized by ``_probe_lock`` so the background loop and an on-demand
        ``probe_now`` can't interleave half-updates into the one state machine.
        Fires ``on_status_change`` only on an actual transition.

        Returns:
            True when the caller may use the relaxed cadence. That requires the
            gateway to be settled (down, or running steadily) *and* the tunnel
            to be healthily ``running``: a tunnel at zero connections or
            degraded is mid-recovery, and waiting ``_PROBE_INTERVAL_STEADY``
            before looking again would stretch a few-second outage into a
            15-second one even though the gateway itself looks perfect.
        """
        with self._probe_lock:
            with self._state_lock:
                prev_gw = self._gw_health
                prev_tunnel = self._tunnel_connected
                prev_tunnel_status = self._tunnel_status_snapshot()

            alive = self.gateway.is_alive()
            healthy = self._probe_gateway_once() if alive else False
            tunnel_alive = self.tunnel.is_alive()
            conns = self._probe_tunnel_conns()
            tunnel_connected = conns > 0

            # Update connection tracking under state lock
            with self._state_lock:
                self._tunnel_conns = conns
                if conns > 0:
                    self._tunnel_conns_expected = max(
                        self._tunnel_conns_expected, conns
                    )

            # End-to-end probe (throttled to 60s intervals). cloudflared can
            # report healthy edge connections while the public path is broken
            # (stale edge session after a network switch), so repeated failures
            # here are their own trigger for recovery.
            should_soft_reconnect_e2e = False
            if tunnel_connected:
                e2e_result = self._probe_tunnel_e2e()
                with self._state_lock:
                    self._tunnel_e2e_ok = e2e_result
                    if self._e2e_consecutive_failures >= self._E2E_FAILURE_THRESHOLD:
                        # Reset on action so the counter always means "real
                        # failures observed since the last recovery attempt".
                        self._e2e_consecutive_failures = 0
                        should_soft_reconnect_e2e = True

            tunnel_restarted = False
            if should_soft_reconnect_e2e:
                logger.warning(
                    "Cloudflare tunnel edge unreachable for {} consecutive "
                    "end-to-end probes; requesting soft reconnect",
                    self._E2E_FAILURE_THRESHOLD,
                )
                if self._issue_soft_reconnect("tunnel_e2e_failed") == "restarted":
                    # Soft path unavailable, so the process was restarted: the
                    # connection count read above belongs to the dead child. Go
                    # back to a clean "connecting" state and leave the zero-conn
                    # escalation clock for the next cycle to start.
                    tunnel_restarted = True
                    tunnel_connected = False
                    with self._state_lock:
                        self._tunnel_conns = 0
                        self._tunnel_disconnected_since = None

            # Zero connections: soft-reconnect immediately (skip cloudflared's
            # own backoff), then escalate to process restart if still zero after
            # _TUNNEL_RECONNECT_TIMEOUT. Soft reconnect is impossible when the
            # process is dead / stdin is gone — escalate right away in that case.
            should_restart_tunnel = False
            should_soft_reconnect = False
            with self._state_lock:
                if tunnel_connected:
                    self._tunnel_disconnected_since = None
                elif tunnel_restarted:
                    pass  # just restarted above; nothing left to escalate
                elif self._tunnel_disconnected_since is None:
                    self._tunnel_disconnected_since = time.time()
                    should_soft_reconnect = True
                elif time.time() - self._tunnel_disconnected_since > self._TUNNEL_RECONNECT_TIMEOUT:
                    should_restart_tunnel = True
                    self._tunnel_disconnected_since = None

            if should_soft_reconnect:
                logger.info(
                    "Cloudflare tunnel has zero edge connections; requesting soft reconnect"
                )
                if self._issue_soft_reconnect("tunnel_zero_conns") == "restarted":
                    # Soft path unavailable (dead process / broken stdin) —
                    # process already restarted; clear the disconnect clock.
                    tunnel_connected = False
                    with self._state_lock:
                        self._tunnel_disconnected_since = None

            if should_restart_tunnel:
                logger.warning(
                    "Cloudflare tunnel still at zero connections after {}s; "
                    "restarting cloudflared process",
                    self._TUNNEL_RECONNECT_TIMEOUT,
                )
                self._restart_tunnel_process("tunnel_timeout")
                tunnel_connected = False

            with self._state_lock:
                if not alive:
                    self._consecutive_failures = 0
                    self._consecutive_ok = 0
                    # Keep an explicit start-failure (e.g. port busy) visible in
                    # the menu until the user retries successfully; only clear to
                    # "stopped" when there is no remembered start error.
                    if not (self._last_error and self._gw_health == "error"):
                        self._gw_health = "stopped"
                    # Nothing is settling when the gateway is down: relax cadence.
                    relaxed = True
                elif healthy:
                    self._gw_health = "running"
                    self._consecutive_failures = 0
                    self._consecutive_ok += 1
                    self._last_error = None
                    # Back off only after the gateway has proven stable.
                    relaxed = self._consecutive_ok >= 2
                else:
                    self._consecutive_ok = 0
                    # Keep an explicit start-failure (health wait timed out while
                    # the child is still alive) visible; _set_gateway_error resets
                    # consecutive_failures to 0, so the first background probe
                    # would otherwise demote "error" back to "starting".
                    if not (self._last_error and self._gw_health == "error"):
                        self._consecutive_failures += 1
                        self._gw_health = (
                            "error"
                            if self._consecutive_failures >= self._UNHEALTHY_THRESHOLD
                            else "starting"
                        )
                    relaxed = False
                self._tunnel_connected = tunnel_connected
                cur_tunnel_status = self._tunnel_status_snapshot()
                changed = (self._gw_health != prev_gw
                           or self._tunnel_connected != prev_tunnel
                           or cur_tunnel_status != prev_tunnel_status)

            # A gateway-only view of "settled" would park a broken tunnel on the
            # 15s cadence, because a tunnel outage does not make the local
            # gateway unhealthy: zero connections, a degraded edge or a dead
            # cloudflared would each cost up to _PROBE_INTERVAL_STEADY before
            # anyone looked. Only relax once BOTH sides are steady. Gated on
            # ``alive`` so a deliberately-down gateway still relaxes instead of
            # spinning at the active cadence forever. Recomputed outside
            # _state_lock: _tunnel_status takes that lock itself.
            if relaxed and alive and self._tunnel_status() != self._TUNNEL_SETTLED_STATUS:
                relaxed = False

        if changed:
            self._fire_status_change()
        return relaxed

    def _tunnel_status_snapshot(self) -> tuple:
        """Snapshot of tunnel state for change detection (called under _state_lock)."""
        return (
            self._tunnel_conns,
            self._tunnel_conns_expected,
            self._tunnel_e2e_ok,
        )

    def _restart_tunnel_process(self, reason: str) -> None:
        """Stop and restart the cloudflared process.

        Handles re-provisioning if the cloud tunnel was deleted. Resets the
        expected connection count so the new process can learn from scratch.

        Args:
            reason: Logging tag for why the restart happened.
        """
        logger.warning("restarting cloudflared tunnel process (reason: {})", reason)
        try:
            reprovisioned = self._reprovision_if_deleted()
            if not reprovisioned:
                self.tunnel.stop()
                cfg = self._get_cfg()
                self.tunnel.start(cfg)
        except Exception:
            logger.exception("Failed to restart cloudflared tunnel")
        with self._state_lock:
            self._tunnel_conns_expected = 0
            # The new process gets a clean slate: a streak inherited from the
            # old one would make the very next failure escalate prematurely.
            self._e2e_consecutive_failures = 0

    def _issue_soft_reconnect(self, reason: str) -> str:
        """Ask cloudflared to rebuild edge connections without killing the process.

        Respects ``_TUNNEL_RECONNECT_COOLDOWN`` so network-change and zero-conn
        probes cannot spam stdin. When the control channel is unusable (process
        dead, stdin closed), escalates immediately to a full process restart —
        there is nothing soft left to try.

        Args:
            reason: Logging / restart tag for why reconnect was requested.

        Returns:
            ``"sent"`` if the soft reconnect command was written;
            ``"skipped"`` if suppressed by cooldown;
            ``"restarted"`` if soft reconnect was impossible and a process
            restart was triggered instead.
        """
        now = time.time()
        with self._state_lock:
            elapsed = now - self._last_tunnel_reconnect_ts
            if elapsed < self._TUNNEL_RECONNECT_COOLDOWN:
                logger.debug(
                    "tunnel reconnect skipped: cooldown ({:.1f}s < {}s)",
                    elapsed,
                    self._TUNNEL_RECONNECT_COOLDOWN,
                )
                return "skipped"
            self._last_tunnel_reconnect_ts = now
            count = max(self._tunnel_conns_expected, 1)

        logger.info(
            "requesting tunnel soft reconnect (reason: {}, count: {})",
            reason,
            count,
        )
        success = self.tunnel.request_reconnect(count)
        if not success:
            self._restart_tunnel_process(reason)
            self._fire_status_change()
            return "restarted"
        return "sent"

    def request_tunnel_reconnect(self, reason: str) -> None:
        """Attempt a soft reconnect; escalate to process restart on failure.

        Uses cloudflared's stdin control channel to issue reconnect commands
        without killing the process. If the soft reconnect doesn't restore
        connectivity within _SOFT_RECONNECT_VERIFY_WINDOW seconds, escalates
        to a full process restart.

        Args:
            reason: Human-readable reason for the reconnect attempt.
        """
        result = self._issue_soft_reconnect(reason)
        if result != "sent":
            return

        def _verify() -> None:
            with self._probe_lock:
                deadline = time.time() + self._SOFT_RECONNECT_VERIFY_WINDOW
                while time.time() < deadline:
                    time.sleep(1.0)
                    conns = self._probe_tunnel_conns()
                    with self._state_lock:
                        self._tunnel_conns = conns
                        if conns > 0:
                            self._tunnel_conns_expected = max(
                                self._tunnel_conns_expected, conns
                            )
                    if conns > 0:
                        e2e_ok = self._probe_tunnel_e2e(ignore_throttle=True)
                        with self._state_lock:
                            self._tunnel_e2e_ok = e2e_ok
                            self._tunnel_connected = True
                        if e2e_ok:
                            self._fire_status_change()
                            return
                # Verification window expired without recovery
                self._restart_tunnel_process("soft_reconnect_failed")
                self._fire_status_change()

        threading.Thread(target=_verify, name="reconnect-verify", daemon=True).start()

    def _probe_tunnel_conns(self) -> int:
        """Return the number of live edge connections cloudflared reports.

        Probes the metrics /ready endpoint and parses ``readyConnections`` from
        the JSON body. Returns 0 if the process is not alive, the response is
        not 200, or the body is unparseable.
        """
        if not self.tunnel.is_alive():
            return 0
        try:
            resp = self._client.get(
                f"http://127.0.0.1:{self.tunnel.metrics_port}/ready", timeout=1
            )
            if resp.status_code != 200:
                return 0
            data = resp.json()
            conns = int(data.get("readyConnections", 0))
            return max(conns, 0)
        except (ValueError, TypeError, KeyError, AttributeError):
            return 0
        except Exception:
            return 0

    def _probe_tunnel_ready(self) -> bool:
        """True if cloudflared reports at least one live edge connection.

        Thin wrapper over _probe_tunnel_conns for backward compatibility with
        the zero-conn reconnect timeout logic.
        """
        return self._probe_tunnel_conns() > 0

    def _probe_tunnel_e2e(self, *, ignore_throttle: bool = False) -> bool:
        """End-to-end probe through the public tunnel to verify the full path.

        Performs GET https://<hostname>/health through the real edge->cloudflared
        ->local chain. Throttled to once per _E2E_PROBE_INTERVAL after the last
        success unless ignore_throttle is set. Returns True when no hostname is
        configured (skip probe for unregistered tunnels).

        Side effect: maintains ``_e2e_consecutive_failures``, but only for
        requests that actually hit the network. A throttled or unconfigured
        skip also returns True, so counting on the return value alone would
        silently clear a real failure streak; the two cases are separated here
        instead of at the call site.

        Args:
            ignore_throttle: When True, skip the 60s cooldown (used for post-
                reconnect verification).

        Returns:
            True if the probe succeeded or was skipped, False on failure.
        """
        cfg = self._get_cfg()
        hostname = cfg.cloudflare.hostname
        if not hostname:
            return True
        if not ignore_throttle:
            now = time.time()
            with self._state_lock:
                if now - self._last_e2e_success_ts < self._E2E_PROBE_INTERVAL:
                    return True
        import httpx
        proxy_url = resolve_proxy()
        try:
            with httpx.Client(
                timeout=5.0,
                proxy=proxy_url,
            ) as client:
                resp = client.get(f"https://{hostname}/health")
                ok = resp.status_code == 200
        except Exception:
            ok = False
        with self._state_lock:
            if ok:
                self._last_e2e_success_ts = time.time()
                self._e2e_consecutive_failures = 0
            else:
                self._e2e_consecutive_failures += 1
                logger.debug(
                    "tunnel end-to-end probe failed ({} consecutive)",
                    self._e2e_consecutive_failures,
                )
        return ok

    def _fire_status_change(self) -> None:
        cb = self.on_status_change
        if cb is None:
            return
        try:
            cb()
        except Exception:
            logger.debug("on_status_change callback failed", exc_info=True)

    def _tunnel_status(self) -> str:
        """Compute tunnel status from connection count and e2e probe results.

        Returns:
            One of "stopped", "connecting", "running", or "degraded".
        """
        if not self.tunnel.is_alive():
            return "stopped"
        with self._state_lock:
            conns = self._tunnel_conns
            expected = self._tunnel_conns_expected
            e2e_ok = self._tunnel_e2e_ok
        if conns == 0:
            return "connecting"
        # running: connections at or above expected AND e2e not explicitly failing
        effective_expected = expected if expected > 0 else 1
        if conns >= effective_expected and e2e_ok is not False:
            return "running"
        return "degraded"

    def _compute_tunnel_degraded_reason(self) -> str:
        """Determine why the tunnel is degraded (for display)."""
        with self._state_lock:
            conns = self._tunnel_conns
            expected = self._tunnel_conns_expected
            e2e_ok = self._tunnel_e2e_ok
        if e2e_ok is False:
            return "边缘不可达"
        effective_expected = expected if expected > 0 else 1
        if conns < effective_expected:
            return f"{conns}/{expected} 连接"
        return ""

    def _tunnel_detail(self) -> str:
        """Return the detail string for the tunnel menu line.

        Returns human-readable connection info like "4/4 连接" or "边缘不可达",
        or empty string when not applicable.
        """
        status = self._tunnel_status()
        if status in ("stopped", "connecting"):
            return ""
        with self._state_lock:
            conns = self._tunnel_conns
            expected = self._tunnel_conns_expected
            e2e_ok = self._tunnel_e2e_ok
        if status == "degraded":
            if e2e_ok is False:
                return "边缘不可达"
            effective_expected = expected if expected > 0 else 1
            return f"{conns}/{effective_expected} 连接"
        # status == "running"
        if expected > 0:
            return f"{conns}/{expected} 连接"
        return ""

    def status(self) -> dict[str, str]:
        """Non-blocking: reads cached health state, never does I/O."""
        cfg = self._get_cfg()
        provisioned = appconfig.is_provisioned(cfg)
        with self._state_lock:
            gw_health = self._gw_health
            last_error = self._last_error or ""
        return {
            "gateway": gw_health,
            "tunnel": self._tunnel_status(),
            "tunnel_detail": self._tunnel_detail(),
            "hostname": cfg.cloudflare.hostname if provisioned else "(未注册)",
            "error": last_error,
        }
