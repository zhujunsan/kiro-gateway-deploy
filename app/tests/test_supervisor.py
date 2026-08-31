import socket
import ssl
import time

import httpx
import pytest
from kiro_gateway_tray import supervisor, appconfig


@pytest.fixture(autouse=True)
def _identity_matches_stored_hostname(monkeypatch):
    """Keep start() from re-provisioning: slug derived from the stored hostname."""
    import kiro_gateway_tray.provision as pmod

    def _username(cfg):
        return pmod.username_from_hostname(cfg.cloudflare.hostname) or "testuser"

    monkeypatch.setattr(pmod, "_get_username", _username)


class _FakeGateway:
    def __init__(self): self.started = False
    def start(self, cfg): self.started = True
    def stop(self): self.started = False
    def is_alive(self): return self.started


class _FakeTunnel:
    def __init__(self):
        self.started = False
        self.reconnect_calls = []

    def start(self, cfg):
        self.started = True

    def stop(self):
        self.started = False

    def is_alive(self):
        return self.started

    def request_reconnect(self, count):
        self.reconnect_calls.append(count)
        return True

    @property
    def metrics_port(self):
        # Deliberately not cloudflared's common 20241 — a live local tunnel
        # on that port would leak real readyConnections into unit probes.
        return 27999


def _make_sup(monkeypatch, tmp_path, provisioned=True):
    monkeypatch.setenv("KIRO_GATEWAY_TRAY_HOME", str(tmp_path))
    cfg = appconfig.load()
    if provisioned:
        cfg.cloudflare.hostname = "kg-test.example.com"
        cfg.cloudflare.run_token = "eyJ_test"
        appconfig.save(cfg)
    s = supervisor.Supervisor(gateway=_FakeGateway(), tunnel=_FakeTunnel())
    monkeypatch.setattr(s, "_wait_healthy", lambda timeout=30: True)
    # Unit tests don't occupy the real gateway port; keep the preflight green.
    monkeypatch.setattr(supervisor.gateway, "wait_port_free", lambda *a, **k: True)
    return s


def test_start_provisioned(monkeypatch, tmp_path):
    s = _make_sup(monkeypatch, tmp_path, provisioned=True)
    s.start()
    assert s.gateway.is_alive() is True
    assert s.tunnel.is_alive() is True
    assert s.status()["hostname"] == "kg-test.example.com"


def test_start_not_provisioned_no_callback_raises(monkeypatch, tmp_path):
    s = _make_sup(monkeypatch, tmp_path, provisioned=False)
    try:
        s.start()
        assert False, "expected RuntimeError"
    except RuntimeError as e:
        assert "注册" in str(e) or "provision" in str(e).lower()


def test_start_not_provisioned_with_callback(monkeypatch, tmp_path):
    s = _make_sup(monkeypatch, tmp_path, provisioned=False)

    def fake_provision(cfg):
        cfg.cloudflare.hostname = "kg-cb.example.com"
        cfg.cloudflare.run_token = "eyJ_cb"
        appconfig.save(cfg)
        raise StopIteration("mock provision complete")

    # Patch provision.run to avoid real HTTP call (returns 3-tuple incl. telemetry_secret)
    import kiro_gateway_tray.provision as pmod
    monkeypatch.setattr(pmod, "run", lambda cfg, secret: ("kg-cb.example.com", "eyJ_cb", ""))
    s.provision_callback = lambda cfg: "fake-secret"
    s.start()
    assert s.gateway.is_alive() is True


def test_stop(monkeypatch, tmp_path):
    s = _make_sup(monkeypatch, tmp_path)
    s.start()
    s.stop()
    assert s.gateway.is_alive() is False
    assert s.tunnel.is_alive() is False


def test_persisted_secret_enables_port_sync_across_restart(monkeypatch, tmp_path):
    # Simulate an already-registered user reopening the app: no in-session
    # secret, but one persisted in config. Changing the port must trigger
    # update_port (regression for the silent-skip bug).
    monkeypatch.setenv("KIRO_GATEWAY_TRAY_HOME", str(tmp_path))
    cfg = appconfig.load()
    cfg.cloudflare.hostname = "kg-test.example.com"
    cfg.cloudflare.run_token = "eyJ_test"
    cfg.cloudflare.shared_secret = "persisted-secret"
    cfg.cloudflare.registered_port = 64005
    cfg.gateway.port = 64010  # user changed the port
    appconfig.save(cfg)

    s = supervisor.Supervisor(gateway=_FakeGateway(), tunnel=_FakeTunnel())
    monkeypatch.setattr(s, "_wait_healthy", lambda timeout=30: True)
    monkeypatch.setattr(supervisor.gateway, "wait_port_free", lambda *a, **k: True)

    calls = {"update_port": 0}
    import kiro_gateway_tray.provision as pmod

    def fake_update_port(cfg, secret):
        calls["update_port"] += 1
        assert secret == "persisted-secret"
        return 64010

    monkeypatch.setattr(pmod, "update_port", fake_update_port)
    s.start()
    assert calls["update_port"] == 1
    assert appconfig.load().cloudflare.registered_port == 64010


def test_port_sync_skipped_without_secret(monkeypatch, tmp_path, capsys):
    # Older config registered before secrets were persisted: no secret anywhere.
    # Port-sync must skip and warn rather than crash.
    monkeypatch.setenv("KIRO_GATEWAY_TRAY_HOME", str(tmp_path))
    cfg = appconfig.load()
    cfg.cloudflare.hostname = "kg-test.example.com"
    cfg.cloudflare.run_token = "eyJ_test"
    cfg.cloudflare.registered_port = 64005
    cfg.gateway.port = 64010
    appconfig.save(cfg)

    s = supervisor.Supervisor(gateway=_FakeGateway(), tunnel=_FakeTunnel())
    monkeypatch.setattr(s, "_wait_healthy", lambda timeout=30: True)
    monkeypatch.setattr(supervisor.gateway, "wait_port_free", lambda *a, **k: True)
    s.start()
    err = capsys.readouterr().err
    assert "无法同步" in err


def test_health_probe_intervals_ordered():
    # Steady cadence must be looser than the active one, and both positive.
    assert supervisor.Supervisor._PROBE_INTERVAL_ACTIVE > 0
    assert supervisor.Supervisor._PROBE_INTERVAL_STEADY > supervisor.Supervisor._PROBE_INTERVAL_ACTIVE


def test_probe_now_detects_running(monkeypatch, tmp_path):
    # An immediate probe (used on menu-open) must flip a started gateway whose
    # /health answers 200 to "running" and fire the status-change callback.
    s = _make_sup(monkeypatch, tmp_path)
    s.gateway.start(None)

    class _Resp:
        status_code = 200

    monkeypatch.setattr(s._client, "get", lambda *a, **k: _Resp())
    fired = {"n": 0}
    s.on_status_change = lambda: fired.__setitem__("n", fired["n"] + 1)

    changed = s.probe_now()
    assert changed is True
    assert s.status()["gateway"] == "running"
    assert fired["n"] == 1


def test_probe_now_stopped_when_process_dead(monkeypatch, tmp_path):
    # Gateway not alive -> probe reports stopped without touching the network.
    s = _make_sup(monkeypatch, tmp_path)

    def _boom(*a, **k):
        raise AssertionError("must not probe /health when process is dead")

    monkeypatch.setattr(s._client, "get", _boom)
    s.probe_now()
    assert s.status()["gateway"] == "stopped"


def test_run_probe_cycle_error_after_threshold(monkeypatch, tmp_path):
    # After _UNHEALTHY_THRESHOLD consecutive failed probes, state flips to "error".
    s = _make_sup(monkeypatch, tmp_path)
    s.gateway.start(None)

    class _Resp:
        status_code = 503

    monkeypatch.setattr(s._client, "get", lambda *a, **k: _Resp())

    for _ in range(supervisor.Supervisor._UNHEALTHY_THRESHOLD - 1):
        s._run_probe_cycle()
    assert s.status()["gateway"] == "starting"

    s._run_probe_cycle()
    assert s.status()["gateway"] == "error"


def test_run_probe_cycle_resets_counters_on_recovery(monkeypatch, tmp_path):
    # A single healthy probe resets the failure counter and flips to "running".
    s = _make_sup(monkeypatch, tmp_path)
    s.gateway.start(None)

    class _Fail:
        status_code = 503

    class _Ok:
        status_code = 200

    monkeypatch.setattr(s._client, "get", lambda *a, **k: _Fail())
    for _ in range(3):
        s._run_probe_cycle()
    assert s.status()["gateway"] == "starting"

    monkeypatch.setattr(s._client, "get", lambda *a, **k: _Ok())
    s._run_probe_cycle()
    assert s.status()["gateway"] == "running"


def test_close_releases_client(monkeypatch, tmp_path):
    s = _make_sup(monkeypatch, tmp_path)
    closed = {"n": 0}
    monkeypatch.setattr(s._client, "close", lambda: closed.__setitem__("n", 1))
    s.close()
    assert closed["n"] == 1


def test_mark_starting(monkeypatch, tmp_path):
    s = _make_sup(monkeypatch, tmp_path)
    assert s.status()["gateway"] == "stopped"
    s.mark_starting()
    assert s.status()["gateway"] == "starting"


def test_run_probe_cycle_soft_reconnects_on_zero_conns(monkeypatch, tmp_path):
    """First zero-conn probe must soft-reconnect immediately, not wait."""
    s = _make_sup(monkeypatch, tmp_path)
    s.start()
    s._stop_health_loop()
    s.gateway.started = True
    s.tunnel.started = True
    s.tunnel.reconnect_calls.clear()
    with s._state_lock:
        s._tunnel_disconnected_since = None
        s._last_tunnel_reconnect_ts = 0.0

    class _Resp:
        status_code = 200

        def json(self):
            return {"readyConnections": 0}

    monkeypatch.setattr(s._client, "get", lambda *a, **k: _Resp())
    monkeypatch.setattr(s, "_probe_gateway_once", lambda: True)
    monkeypatch.setattr(s, "_probe_tunnel_e2e", lambda **kw: True)
    monkeypatch.setattr(s, "_reprovision_if_deleted", lambda: False)

    restarted = []
    monkeypatch.setattr(
        s, "_restart_tunnel_process",
        lambda reason: restarted.append(reason),
    )

    s._run_probe_cycle()
    assert s._tunnel_disconnected_since is not None
    assert s.tunnel.reconnect_calls == [1]
    assert not restarted
    s.close()


def test_run_probe_cycle_auto_restarts_tunnel_on_timeout(monkeypatch, tmp_path):
    s = _make_sup(monkeypatch, tmp_path)
    s.start()
    s._stop_health_loop()

    # Gateway is alive and healthy, tunnel is alive but not ready
    s.gateway.started = True
    s.tunnel.started = True
    s.tunnel.reconnect_calls.clear()
    with s._state_lock:
        s._tunnel_disconnected_since = None
        s._last_tunnel_reconnect_ts = 0.0

    class _Resp:
        status_code = 200

        def json(self):
            return {"readyConnections": 0}

    monkeypatch.setattr(s._client, "get", lambda *a, **k: _Resp())
    monkeypatch.setattr(s, "_probe_gateway_once", lambda: True)
    monkeypatch.setattr(s, "_probe_tunnel_e2e", lambda **kw: True)

    # Mock _reprovision_if_deleted to return False (tunnel exists, just restart)
    monkeypatch.setattr(s, "_reprovision_if_deleted", lambda: False)

    # First probe: soft reconnect + sets the disconnected timestamp
    s._run_probe_cycle()
    assert s._tunnel_disconnected_since is not None
    initial_ts = s._tunnel_disconnected_since
    assert s.tunnel.reconnect_calls  # soft reconnect fired

    # Check that it didn't process-restart yet
    restarted = []
    monkeypatch.setattr(s.tunnel, "stop", lambda: restarted.append("stop"))
    monkeypatch.setattr(s.tunnel, "start", lambda cfg: restarted.append("start"))

    # Run probe again with no time advancement: should not restart
    s._run_probe_cycle()
    assert not restarted
    assert s._tunnel_disconnected_since == initial_ts

    # Mock time advancement beyond _TUNNEL_RECONNECT_TIMEOUT (5s)
    fake_time = initial_ts + s._TUNNEL_RECONNECT_TIMEOUT + 1
    monkeypatch.setattr(time, "time", lambda: fake_time)

    s._run_probe_cycle()
    assert "stop" in restarted
    assert "start" in restarted
    assert s._tunnel_disconnected_since is None
    s.close()


def test_run_probe_cycle_dead_tunnel_restarts_immediately(monkeypatch, tmp_path):
    """Dead cloudflared cannot soft-reconnect — escalate on first zero probe."""
    s = _make_sup(monkeypatch, tmp_path)
    s.start()
    # Stop the background loop so this test owns every probe cycle.
    s._stop_health_loop()

    # Gateway is alive and healthy, tunnel is NOT alive
    s.gateway.started = True
    s.tunnel.started = False
    with s._state_lock:
        s._tunnel_disconnected_since = None
        s._last_tunnel_reconnect_ts = 0.0
    # Soft reconnect must fail so we escalate immediately
    s.tunnel.request_reconnect = lambda count: False

    monkeypatch.setattr(s, "_probe_gateway_once", lambda: True)
    monkeypatch.setattr(s, "_reprovision_if_deleted", lambda: False)

    restart_calls = []
    monkeypatch.setattr(
        s, "_restart_tunnel_process",
        lambda reason: restart_calls.append(reason),
    )

    s._run_probe_cycle()
    assert "tunnel_zero_conns" in restart_calls
    assert s._tunnel_disconnected_since is None
    s.close()


def test_run_probe_cycle_auto_restarts_tunnel_when_dead_on_timeout(monkeypatch, tmp_path):
    # Soft reconnect skipped by cooldown: still escalate via the short timeout
    # when the process stays dead / at zero connections.
    s = _make_sup(monkeypatch, tmp_path)
    s.start()
    s._stop_health_loop()

    s.gateway.started = True
    s.tunnel.started = False
    with s._state_lock:
        s._tunnel_disconnected_since = None
        # Pretend a soft reconnect was just attempted so cooldown suppresses the
        # immediate escalate path; timeout must still fire.
        s._last_tunnel_reconnect_ts = time.time()

    monkeypatch.setattr(s, "_probe_gateway_once", lambda: True)
    monkeypatch.setattr(s, "_reprovision_if_deleted", lambda: False)

    # First probe: sets the disconnected timestamp (soft skipped by cooldown)
    s._run_probe_cycle()
    assert s._tunnel_disconnected_since is not None
    initial_ts = s._tunnel_disconnected_since

    restarted = []
    monkeypatch.setattr(s.tunnel, "stop", lambda: restarted.append("stop"))
    monkeypatch.setattr(s.tunnel, "start", lambda cfg: restarted.append("start"))

    # Mock time advancement beyond _TUNNEL_RECONNECT_TIMEOUT
    fake_time = initial_ts + s._TUNNEL_RECONNECT_TIMEOUT + 1
    monkeypatch.setattr(time, "time", lambda: fake_time)

    s._run_probe_cycle()
    assert "stop" in restarted
    assert "start" in restarted
    assert s._tunnel_disconnected_since is None
    s.close()


def test_restart_waits_for_port_before_start(monkeypatch, tmp_path):
    # restart() must fully stop the old gateway and confirm the port is free
    # BEFORE starting the new child, otherwise the new uvicorn races the dying
    # one for the port. Assert ordering: stop -> wait_port_free -> start.
    s = _make_sup(monkeypatch, tmp_path)
    s.start()

    order = []
    monkeypatch.setattr(s.gateway, "stop", lambda: order.append("stop"))
    monkeypatch.setattr(s.tunnel, "stop", lambda: order.append("tunnel_stop"))

    def fake_wait(port, **kwargs):
        order.append(f"wait:{port}")
        return True

    monkeypatch.setattr(supervisor.gateway, "wait_port_free", fake_wait)
    monkeypatch.setattr(s, "start", lambda: order.append("start") or True)

    s.restart()

    assert order.index("stop") < order.index(f"wait:{s._get_cfg().gateway.port}")
    assert order.index(f"wait:{s._get_cfg().gateway.port}") < order.index("start")


def test_restart_stop_budget_is_bounded_with_wedged_child(monkeypatch, tmp_path):
    # End-to-end guard for the tray "restart" click while an SSE stream is still
    # open: the real GatewayProcess.stop() runs, the child ignores SIGTERM, and
    # restart() must still escalate to SIGKILL within the configured windows
    # instead of blocking the UI on uvicorn's graceful drain.
    s = _make_sup(monkeypatch, tmp_path)
    real_gateway = supervisor.gateway.GatewayProcess()
    s.gateway = real_gateway
    waits: list[float | None] = []

    class _WedgedChild:
        def poll(self):
            return None

        def terminate(self):
            pass

        def kill(self):
            pass

        def wait(self, timeout=None):
            waits.append(timeout)
            raise supervisor.gateway.subprocess.TimeoutExpired(cmd="gw", timeout=timeout)

    real_gateway._proc = _WedgedChild()
    monkeypatch.setattr(supervisor.gateway.proc_guard, "clear_gateway_pid", lambda: None)
    monkeypatch.setattr(real_gateway, "start", lambda cfg: None)
    monkeypatch.setattr(s, "_wait_healthy", lambda timeout=30: True)

    assert s.restart() is True
    assert sum(w or 0 for w in waits) <= (
        supervisor.gateway.GRACEFUL_STOP_TIMEOUT
        + supervisor.gateway.KILL_REAP_TIMEOUT
    )
    s.close()


def test_restart_raises_when_port_stays_busy(monkeypatch, tmp_path):
    # External listener still holding the port after stop: fail fast with
    # PortBusyError instead of spawning a child that cannot bind.
    s = _make_sup(monkeypatch, tmp_path)
    s.start()

    monkeypatch.setattr(supervisor.gateway, "wait_port_free", lambda port, **k: False)
    started = {"n": 0}
    monkeypatch.setattr(s, "start", lambda: started.__setitem__("n", started["n"] + 1) or True)

    try:
        s.restart()
        assert False, "expected PortBusyError"
    except supervisor.gateway.PortBusyError as e:
        assert e.port == s._get_cfg().gateway.port
        assert "占用" in str(e)
    assert started["n"] == 0
    assert s.status()["gateway"] == "error"
    assert "占用" in (s.last_error or "")


def test_start_raises_when_port_busy(monkeypatch, tmp_path):
    s = _make_sup(monkeypatch, tmp_path)
    monkeypatch.setattr(supervisor.gateway, "wait_port_free", lambda port, **k: False)

    try:
        s.start()
        assert False, "expected PortBusyError"
    except supervisor.gateway.PortBusyError as e:
        assert "64005" in str(e) or str(e.port) in str(e)
    assert s.gateway.is_alive() is False
    assert s.status()["gateway"] == "error"
    assert "端口" in (s.last_error or "")


def test_start_sets_error_when_health_fails(monkeypatch, tmp_path):
    s = _make_sup(monkeypatch, tmp_path)
    monkeypatch.setattr(s, "_wait_healthy", lambda timeout=30: False)
    monkeypatch.setattr(
        s,
        "_diagnose_start_failure",
        lambda cfg: "网关未能在时限内就绪（健康检查失败）。请查看日志目录或尝试重新启动。",
    )

    ok = s.start()
    assert ok is False
    assert s.status()["gateway"] == "error"
    assert "健康检查" in (s.last_error or "")


def test_run_probe_cycle_preserves_start_failure_error_when_alive_unhealthy(
    monkeypatch, tmp_path
):
    # Race: start() sets error + zeros consecutive_failures, then the health
    # loop's first alive-but-unhealthy probe must NOT demote error -> starting.
    s = _make_sup(monkeypatch, tmp_path)
    s.gateway.start(None)
    s._set_gateway_error("网关未能在时限内就绪（健康检查失败）。")

    class _Fail:
        status_code = 503

    monkeypatch.setattr(s._client, "get", lambda *a, **k: _Fail())
    s._run_probe_cycle()
    assert s.status()["gateway"] == "error"
    assert "健康检查" in (s.last_error or "")


def test_wait_healthy_fails_fast_when_process_dead(monkeypatch, tmp_path):
    # Dead child + failed /health must return False immediately, not burn the
    # full 30s timeout (bind failures exit uvicorn at once).
    monkeypatch.setenv("KIRO_GATEWAY_TRAY_HOME", str(tmp_path))
    cfg = appconfig.load()
    cfg.cloudflare.hostname = "kg-test.example.com"
    cfg.cloudflare.run_token = "eyJ_test"
    appconfig.save(cfg)
    s = supervisor.Supervisor(gateway=_FakeGateway(), tunnel=_FakeTunnel())
    s.gateway.started = False
    monkeypatch.setattr(s, "_probe_gateway_once", lambda timeout=1: False)

    t0 = time.monotonic()
    assert s._wait_healthy(timeout=30) is False
    assert time.monotonic() - t0 < 2.0


def test_reprovision_if_deleted_exists_true(monkeypatch, tmp_path):
    # tunnel_exists returns True → _reprovision_if_deleted returns False (no rebuild)
    monkeypatch.setenv("KIRO_GATEWAY_TRAY_HOME", str(tmp_path))
    cfg = appconfig.load()
    cfg.cloudflare.hostname = "kg-test.example.com"
    cfg.cloudflare.run_token = "eyJ_test"
    cfg.cloudflare.shared_secret = "s3cret"
    cfg.cloudflare.provision_url = "https://w.example.com"
    appconfig.save(cfg)

    s = supervisor.Supervisor(gateway=_FakeGateway(), tunnel=_FakeTunnel())
    s._startup_ready.set()
    import kiro_gateway_tray.provision as pmod
    monkeypatch.setattr(pmod, "tunnel_exists", lambda cfg, secret: True)

    assert s._reprovision_if_deleted() is False


def test_reprovision_if_deleted_exists_none(monkeypatch, tmp_path):
    # tunnel_exists returns None (network error) → conservative, no rebuild
    monkeypatch.setenv("KIRO_GATEWAY_TRAY_HOME", str(tmp_path))
    cfg = appconfig.load()
    cfg.cloudflare.hostname = "kg-test.example.com"
    cfg.cloudflare.run_token = "eyJ_test"
    cfg.cloudflare.shared_secret = "s3cret"
    cfg.cloudflare.provision_url = "https://w.example.com"
    appconfig.save(cfg)

    s = supervisor.Supervisor(gateway=_FakeGateway(), tunnel=_FakeTunnel())
    s._startup_ready.set()
    import kiro_gateway_tray.provision as pmod
    monkeypatch.setattr(pmod, "tunnel_exists", lambda cfg, secret: None)

    assert s._reprovision_if_deleted() is False


def test_reprovision_if_deleted_exists_false(monkeypatch, tmp_path):
    # tunnel_exists returns False → re-provision silently
    monkeypatch.setenv("KIRO_GATEWAY_TRAY_HOME", str(tmp_path))
    cfg = appconfig.load()
    cfg.cloudflare.hostname = "kg-test.example.com"
    cfg.cloudflare.run_token = "eyJ_old"
    cfg.cloudflare.shared_secret = "s3cret"
    cfg.cloudflare.provision_url = "https://w.example.com"
    appconfig.save(cfg)

    s = supervisor.Supervisor(gateway=_FakeGateway(), tunnel=_FakeTunnel())
    s._startup_ready.set()
    import kiro_gateway_tray.provision as pmod
    monkeypatch.setattr(pmod, "tunnel_exists", lambda cfg, secret: False)
    monkeypatch.setattr(pmod, "run", lambda cfg, secret: ("kg-test.example.com", "eyJ_new", ""))

    result = s._reprovision_if_deleted()
    assert result is True
    reloaded = appconfig.load()
    assert reloaded.cloudflare.run_token == "eyJ_new"


def test_reprovision_if_deleted_no_secret(monkeypatch, tmp_path):
    # No secret available → safe fallback, no rebuild attempt
    monkeypatch.setenv("KIRO_GATEWAY_TRAY_HOME", str(tmp_path))
    cfg = appconfig.load()
    cfg.cloudflare.hostname = "kg-test.example.com"
    cfg.cloudflare.run_token = "eyJ_test"
    appconfig.save(cfg)

    s = supervisor.Supervisor(gateway=_FakeGateway(), tunnel=_FakeTunnel())
    assert s._reprovision_if_deleted() is False


def test_run_probe_cycle_auto_restarts_tunnel_when_dead_on_timeout(monkeypatch, tmp_path):
    # Tests that when tunnel is dead (is_alive is False), it still restarts on timeout
    s = _make_sup(monkeypatch, tmp_path)
    s.start()

    # Gateway is alive and healthy, tunnel is NOT alive
    s.gateway.started = True
    s.tunnel.started = False

    class _Resp:
        status_code = 200

    monkeypatch.setattr(s._client, "get", lambda *a, **k: _Resp())
    monkeypatch.setattr(s, "_reprovision_if_deleted", lambda: False)

    # First probe: sets the disconnected timestamp
    s._run_probe_cycle()
    assert s._tunnel_disconnected_since is not None
    initial_ts = s._tunnel_disconnected_since

    restarted = []
    monkeypatch.setattr(s.tunnel, "stop", lambda: restarted.append("stop"))
    monkeypatch.setattr(s.tunnel, "start", lambda cfg: restarted.append("start"))

    # Mock time advancement beyond _TUNNEL_RECONNECT_TIMEOUT
    fake_time = initial_ts + s._TUNNEL_RECONNECT_TIMEOUT + 5
    monkeypatch.setattr(time, "time", lambda: fake_time)

    s._run_probe_cycle()
    assert "stop" in restarted
    assert "start" in restarted
    assert s._tunnel_disconnected_since is None



def test_health_loop_immediate_restart_replaces_exited_thread(monkeypatch, tmp_path):
    """stop → start must create a live new loop, never revive the old one."""
    s = _make_sup(monkeypatch, tmp_path)
    s.start()
    first = s._health_thread
    assert first is not None and first.is_alive()

    s.stop()
    assert not first.is_alive()
    assert s._health_thread is None

    s.start()
    second = s._health_thread
    assert second is not None and second is not first and second.is_alive()
    s.close()
    assert not second.is_alive()


def test_stop_health_loop_is_idempotent(monkeypatch, tmp_path):
    s = _make_sup(monkeypatch, tmp_path)
    s.start()
    s.stop()
    s.stop()
    assert s._health_thread is None
    s.close()


class TestSupervisorLoginState:
    """Newer gateways report a signed-out account on /health instead of exiting.

    Before that, "no usable Kiro credentials" killed the process and the tray
    could only say "gateway exited" (Sentry KIRO-GATEWAY-TRAY-W). Reading the
    state here is what lets the UI say "open Kiro and sign in".
    """

    @staticmethod
    def _resp(payload):
        class _Resp:
            status_code = 200

            def json(self):
                if payload is None:
                    raise ValueError("no json")
                return payload

        return _Resp()

    def test_reports_login_required_from_degraded_health(self, monkeypatch, tmp_path):
        s = _make_sup(monkeypatch, tmp_path)
        monkeypatch.setattr(s._client, "get", lambda *a, **k: self._resp({
            "status": "degraded",
            "account": {
                "code": "account_auth_required",
                "message": "expired",
                "login_required": True,
            },
        }))

        state = s.login_state()

        assert state.login_required is True
        assert state.code == "account_auth_required"

    def test_ready_account_is_signed_in(self, monkeypatch, tmp_path):
        s = _make_sup(monkeypatch, tmp_path)
        monkeypatch.setattr(s._client, "get", lambda *a, **k: self._resp({
            "status": "healthy",
            "account": {"status": "ready", "login_required": False},
        }))

        assert s.login_state().login_required is False

    def test_old_gateway_without_account_block_is_signed_in(self, monkeypatch, tmp_path):
        s = _make_sup(monkeypatch, tmp_path)
        monkeypatch.setattr(
            s._client, "get", lambda *a, **k: self._resp({"status": "healthy"})
        )

        assert s.login_state().login_required is False

    def test_unreachable_health_never_claims_signed_out(self, monkeypatch, tmp_path):
        s = _make_sup(monkeypatch, tmp_path)
        monkeypatch.setattr(
            s._client,
            "get",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("refused")),
        )

        assert s.login_state().login_required is False

    def test_non_json_health_never_claims_signed_out(self, monkeypatch, tmp_path):
        s = _make_sup(monkeypatch, tmp_path)
        monkeypatch.setattr(s._client, "get", lambda *a, **k: self._resp(None))

        assert s.login_state().login_required is False


def test_startup_sync_persists_changed_telemetry_secret(monkeypatch, tmp_path):
    monkeypatch.setenv("KIRO_GATEWAY_TRAY_HOME", str(tmp_path))
    cfg = appconfig.load()
    cfg.cloudflare.hostname = "kg-test.example.com"
    cfg.cloudflare.run_token = "token"
    cfg.cloudflare.registered_port = cfg.gateway.port
    cfg.cloudflare.provision_url = "https://provision.example"
    cfg.cloudflare.shared_secret = "activation"
    cfg.telemetry.secret = "old"
    appconfig.save(cfg)
    s = supervisor.Supervisor(gateway=_FakeGateway(), tunnel=_FakeTunnel())
    monkeypatch.setattr(s, "_wait_healthy", lambda timeout=30: True)
    monkeypatch.setattr(supervisor.gateway, "wait_port_free", lambda *a, **k: True)
    import kiro_gateway_tray.provision as provision
    monkeypatch.setattr(provision, "_get_username", lambda _cfg: "user")
    monkeypatch.setattr(
        provision, "refresh_telemetry_secret", lambda *_args: "rotated"
    )

    s.start()
    assert appconfig.load().telemetry.secret == "rotated"
    s.close()


def test_startup_sync_failure_does_not_block_gateway(monkeypatch, tmp_path):
    s = _make_sup(monkeypatch, tmp_path)
    cfg = appconfig.load()
    cfg.cloudflare.registered_port = cfg.gateway.port
    cfg.cloudflare.provision_url = "https://provision.example"
    cfg.cloudflare.shared_secret = "activation"
    appconfig.save(cfg)
    import kiro_gateway_tray.provision as provision
    monkeypatch.setattr(
        provision, "refresh_telemetry_secret",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("offline")),
    )

    assert s.start() is True
    assert s.gateway.is_alive() is True
    s.close()


# =============================================================================
# Tunnel Health Model Tests
# =============================================================================

import json


class _FakeTunnelWithReconnect:
    """Fake tunnel with request_reconnect support."""

    def __init__(self):
        self.started = False
        self.reconnect_calls = []
        # Same rationale as _FakeTunnel.metrics_port: avoid colliding with a
        # real cloudflared metrics listener on the developer's machine.
        self._metrics_port = 27999

    def start(self, cfg):
        self.started = True

    def stop(self):
        self.started = False

    def is_alive(self):
        return self.started

    def request_reconnect(self, count):
        self.reconnect_calls.append(count)
        return True

    @property
    def metrics_port(self):
        return self._metrics_port


def _make_sup_v2(monkeypatch, tmp_path, provisioned=True, tunnel=None):
    """Create a supervisor with the new fake tunnel supporting reconnect."""
    monkeypatch.setenv("KIRO_GATEWAY_TRAY_HOME", str(tmp_path))
    cfg = appconfig.load()
    if provisioned:
        cfg.cloudflare.hostname = "kg-test.example.com"
        cfg.cloudflare.run_token = "eyJ_test"
        appconfig.save(cfg)
    tun = tunnel or _FakeTunnelWithReconnect()
    s = supervisor.Supervisor(gateway=_FakeGateway(), tunnel=tun)
    monkeypatch.setattr(s, "_wait_healthy", lambda timeout=30: True)
    monkeypatch.setattr(supervisor.gateway, "wait_port_free", lambda *a, **k: True)
    # Block network watcher from actually starting OS-level watchers
    monkeypatch.setattr(
        "kiro_gateway_tray.network_watch.NetworkWatcher.start", lambda self: None
    )
    monkeypatch.setattr(
        "kiro_gateway_tray.network_watch.NetworkWatcher.stop", lambda self: None
    )
    return s


class _ReadyResponse:
    """Simulates the cloudflared /ready endpoint response."""

    def __init__(self, ready_connections: int):
        self.status_code = 200 if ready_connections > 0 else 503
        self._body = {"status": self.status_code, "readyConnections": ready_connections}

    def json(self):
        return self._body


class _ReadyResponseBadJson:
    status_code = 200

    def json(self):
        raise ValueError("bad json")


class _ReadyResponseNon200:
    status_code = 503

    def json(self):
        return {}


class TestProbeReadyConnections:
    """Test _probe_tunnel_conns parses readyConnections correctly."""

    def test_four_connections_running(self, monkeypatch, tmp_path):
        s = _make_sup_v2(monkeypatch, tmp_path)
        s.tunnel.started = True
        monkeypatch.setattr(s._client, "get", lambda *a, **k: _ReadyResponse(4))
        assert s._probe_tunnel_conns() == 4

    def test_one_connection(self, monkeypatch, tmp_path):
        s = _make_sup_v2(monkeypatch, tmp_path)
        s.tunnel.started = True
        monkeypatch.setattr(s._client, "get", lambda *a, **k: _ReadyResponse(1))
        assert s._probe_tunnel_conns() == 1

    def test_zero_connections(self, monkeypatch, tmp_path):
        s = _make_sup_v2(monkeypatch, tmp_path)
        s.tunnel.started = True
        monkeypatch.setattr(s._client, "get", lambda *a, **k: _ReadyResponse(0))
        assert s._probe_tunnel_conns() == 0

    def test_bad_json_returns_zero(self, monkeypatch, tmp_path):
        s = _make_sup_v2(monkeypatch, tmp_path)
        s.tunnel.started = True
        monkeypatch.setattr(s._client, "get", lambda *a, **k: _ReadyResponseBadJson())
        assert s._probe_tunnel_conns() == 0

    def test_non_200_returns_zero(self, monkeypatch, tmp_path):
        s = _make_sup_v2(monkeypatch, tmp_path)
        s.tunnel.started = True
        monkeypatch.setattr(s._client, "get", lambda *a, **k: _ReadyResponseNon200())
        assert s._probe_tunnel_conns() == 0

    def test_process_dead_returns_zero(self, monkeypatch, tmp_path):
        s = _make_sup_v2(monkeypatch, tmp_path)
        s.tunnel.started = False
        assert s._probe_tunnel_conns() == 0


class TestTunnelExpectedConnections:
    """Expected connections track observed peak and reset on restart."""

    def test_peak_tracking(self, monkeypatch, tmp_path):
        s = _make_sup_v2(monkeypatch, tmp_path)
        s.tunnel.started = True
        s.gateway.started = True

        # First observation: 4 connections
        monkeypatch.setattr(s._client, "get", lambda *a, **k: _ReadyResponse(4))
        monkeypatch.setattr(s, "_probe_gateway_once", lambda: True)
        monkeypatch.setattr(s, "_probe_tunnel_e2e", lambda **kw: True)
        s._run_probe_cycle()
        assert s._tunnel_conns_expected == 4

        # Drop to 3: expected stays at 4
        monkeypatch.setattr(s._client, "get", lambda *a, **k: _ReadyResponse(3))
        s._run_probe_cycle()
        assert s._tunnel_conns_expected == 4
        assert s._tunnel_status() == "degraded"

    def test_only_seen_two_is_running(self, monkeypatch, tmp_path):
        s = _make_sup_v2(monkeypatch, tmp_path)
        s.tunnel.started = True
        s.gateway.started = True

        monkeypatch.setattr(s._client, "get", lambda *a, **k: _ReadyResponse(2))
        monkeypatch.setattr(s, "_probe_gateway_once", lambda: True)
        monkeypatch.setattr(s, "_probe_tunnel_e2e", lambda **kw: True)
        s._run_probe_cycle()
        assert s._tunnel_conns_expected == 2
        assert s._tunnel_conns == 2
        assert s._tunnel_status() == "running"

    def test_expected_resets_on_restart(self, monkeypatch, tmp_path):
        s = _make_sup_v2(monkeypatch, tmp_path)
        s.tunnel.started = True
        with s._state_lock:
            s._tunnel_conns_expected = 4
        monkeypatch.setattr(s, "_reprovision_if_deleted", lambda: False)
        s._restart_tunnel_process("test")
        assert s._tunnel_conns_expected == 0

    def test_four_then_three_degraded(self, monkeypatch, tmp_path):
        s = _make_sup_v2(monkeypatch, tmp_path)
        s.tunnel.started = True
        with s._state_lock:
            s._tunnel_conns = 3
            s._tunnel_conns_expected = 4
            s._tunnel_e2e_ok = True
        assert s._tunnel_status() == "degraded"
        detail = s._tunnel_detail()
        assert "3/4" in detail


class TestEndToEndProbe:
    """Test _probe_tunnel_e2e behavior."""

    def test_e2e_failure_with_local_200_is_degraded(self, monkeypatch, tmp_path):
        s = _make_sup_v2(monkeypatch, tmp_path)
        s.tunnel.started = True
        with s._state_lock:
            s._tunnel_conns = 4
            s._tunnel_conns_expected = 4
            s._tunnel_e2e_ok = False
        assert s._tunnel_status() == "degraded"
        detail = s._tunnel_detail()
        assert "边缘不可达" in detail

    def test_e2e_throttled_after_success(self, monkeypatch, tmp_path):
        s = _make_sup_v2(monkeypatch, tmp_path)
        with s._state_lock:
            s._last_e2e_success_ts = time.time()
        # Should return True immediately due to throttle
        result = s._probe_tunnel_e2e()
        assert result.ok is True
        assert result.skipped is True

    def test_e2e_no_hostname_returns_true(self, monkeypatch, tmp_path):
        """Unregistered tunnel (no hostname) should not make network requests."""
        monkeypatch.setenv("KIRO_GATEWAY_TRAY_HOME", str(tmp_path))
        cfg = appconfig.load()
        cfg.cloudflare.hostname = ""
        appconfig.save(cfg)
        s = supervisor.Supervisor(
            gateway=_FakeGateway(), tunnel=_FakeTunnelWithReconnect()
        )
        # If it tries to make a request, httpx would fail because we didn't mock it
        result = s._probe_tunnel_e2e()
        assert result.ok is True
        assert result.skipped is True

    def test_e2e_ignore_throttle(self, monkeypatch, tmp_path):
        """ignore_throttle=True should probe even within the cooldown."""
        s = _make_sup_v2(monkeypatch, tmp_path)
        with s._state_lock:
            s._last_e2e_success_ts = time.time()

        probe_called = {"n": 0}

        class _FakeClient:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def get(self, url, **kw):
                probe_called["n"] += 1

                class _R:
                    status_code = 200
                return _R()

        monkeypatch.setattr(httpx, "Client", lambda **kw: _FakeClient())
        monkeypatch.setattr(supervisor.httpx, "Client", lambda **kw: _FakeClient())
        result = s._probe_tunnel_e2e(ignore_throttle=True)
        assert result.ok is True
        assert result.skipped is False
        assert probe_called["n"] == 1


class TestSoftReconnect:
    """Test request_tunnel_reconnect escalation logic."""

    def test_soft_reconnect_preferred_no_process_kill(self, monkeypatch, tmp_path):
        s = _make_sup_v2(monkeypatch, tmp_path)
        s.tunnel.started = True
        with s._state_lock:
            s._tunnel_conns_expected = 4

        s.request_tunnel_reconnect("network_change")
        assert s.tunnel.reconnect_calls == [4]
        # Process still alive
        assert s.tunnel.started is True

    def test_soft_reconnect_failure_escalates(self, monkeypatch, tmp_path):
        tun = _FakeTunnelWithReconnect()
        tun.request_reconnect = lambda count: False  # simulate failure
        s = _make_sup_v2(monkeypatch, tmp_path, tunnel=tun)
        s.tunnel.started = True
        monkeypatch.setattr(s, "_reprovision_if_deleted", lambda: False)

        restart_calls = []
        original_restart = s._restart_tunnel_process

        def _track_restart(reason):
            restart_calls.append(reason)
            original_restart(reason)

        monkeypatch.setattr(s, "_restart_tunnel_process", _track_restart)
        s.request_tunnel_reconnect("network_change")
        assert "network_change" in restart_calls

    def test_cooldown_prevents_duplicate(self, monkeypatch, tmp_path):
        s = _make_sup_v2(monkeypatch, tmp_path)
        s.tunnel.started = True
        with s._state_lock:
            s._tunnel_conns_expected = 2

        s.request_tunnel_reconnect("test1")
        assert len(s.tunnel.reconnect_calls) == 1
        # Second call within cooldown
        s.request_tunnel_reconnect("test2")
        assert len(s.tunnel.reconnect_calls) == 1

    def test_cooldown_expires_allows_reconnect(self, monkeypatch, tmp_path):
        s = _make_sup_v2(monkeypatch, tmp_path)
        s.tunnel.started = True
        with s._state_lock:
            s._tunnel_conns_expected = 2
            s._last_tunnel_reconnect_ts = time.time() - 20

        s.request_tunnel_reconnect("test")
        assert len(s.tunnel.reconnect_calls) == 1


class TestTunnelTimeoutBehavior:
    """Short zero-conn timeout must escalate to process restart."""

    def test_timeout_triggers_restart(self, monkeypatch, tmp_path):
        s = _make_sup_v2(monkeypatch, tmp_path)
        s.start()
        s._stop_health_loop()
        s.gateway.started = True
        s.tunnel.started = True
        s.tunnel.reconnect_calls.clear()
        with s._state_lock:
            s._tunnel_disconnected_since = None
            s._last_tunnel_reconnect_ts = 0.0

        monkeypatch.setattr(s, "_probe_gateway_once", lambda: True)
        monkeypatch.setattr(s._client, "get", lambda *a, **k: _ReadyResponse(0))
        monkeypatch.setattr(s, "_probe_tunnel_e2e", lambda **kw: True)
        monkeypatch.setattr(s, "_reprovision_if_deleted", lambda: False)

        # First probe: soft reconnect + sets disconnected_since
        s._run_probe_cycle()
        assert s._tunnel_disconnected_since is not None
        initial_ts = s._tunnel_disconnected_since
        assert s.tunnel.reconnect_calls

        restart_calls = []
        monkeypatch.setattr(
            s, "_restart_tunnel_process",
            lambda reason: restart_calls.append(reason),
        )

        # Time jump beyond the short timeout (5s)
        fake_time = initial_ts + s._TUNNEL_RECONNECT_TIMEOUT + 1
        monkeypatch.setattr(time, "time", lambda: fake_time)

        s._run_probe_cycle()
        assert "tunnel_timeout" in restart_calls
        s.close()


class TestStopStopsWatcher:
    """stop() must shut down the network watcher."""

    def test_stop_cleans_watcher(self, monkeypatch, tmp_path):
        s = _make_sup_v2(monkeypatch, tmp_path)
        s.start()

        class _FakeWatcher:
            stopped = False

            def stop(self):
                _FakeWatcher.stopped = True

        s._network_watcher = _FakeWatcher()
        s.stop()
        assert _FakeWatcher.stopped is True
        assert s._network_watcher is None


class TestStatusDict:
    """status() dict includes tunnel_detail."""

    def test_status_includes_tunnel_detail(self, monkeypatch, tmp_path):
        s = _make_sup_v2(monkeypatch, tmp_path)
        s.tunnel.started = True
        with s._state_lock:
            s._tunnel_conns = 4
            s._tunnel_conns_expected = 4
            s._tunnel_e2e_ok = True
        result = s.status()
        assert "tunnel_detail" in result
        assert "4/4" in result["tunnel_detail"]

    def test_status_degraded_detail(self, monkeypatch, tmp_path):
        s = _make_sup_v2(monkeypatch, tmp_path)
        s.tunnel.started = True
        with s._state_lock:
            s._tunnel_conns = 1
            s._tunnel_conns_expected = 4
            s._tunnel_e2e_ok = True
        result = s.status()
        assert result["tunnel"] == "degraded"
        assert "1/4" in result["tunnel_detail"]

    def test_status_connecting_no_detail(self, monkeypatch, tmp_path):
        s = _make_sup_v2(monkeypatch, tmp_path)
        s.tunnel.started = True
        with s._state_lock:
            s._tunnel_conns = 0
        result = s.status()
        assert result["tunnel"] == "connecting"
        assert result["tunnel_detail"] == ""


class _StubE2EClient:
    """Stand-in for httpx.Client used by the end-to-end probe.

    Args:
        statuses: Status codes to return in order; the last one repeats. Use
            ``None`` to raise ``RuntimeError`` (kind=unexpected) if no
            ``error_factory`` is set.
        error_factory: If set, each ``get`` raises a fresh exception from this
            callable (supports ``__cause__`` chains).
    """

    def __init__(
        self,
        statuses: list[int | None] | None = None,
        *,
        error_factory=None,
    ) -> None:
        self._statuses = list(statuses) if statuses is not None else []
        self._error_factory = error_factory
        self.calls = 0

    def __call__(self, **_kw) -> "_StubE2EClient":
        return self

    def __enter__(self) -> "_StubE2EClient":
        return self

    def __exit__(self, *_exc) -> bool:
        return False

    def get(self, _url: str, **_kw):
        self.calls += 1
        if self._error_factory is not None:
            raise self._error_factory()
        if not self._statuses:
            raise RuntimeError("edge unreachable")
        status = self._statuses[0] if len(self._statuses) == 1 else self._statuses.pop(0)
        if status is None:
            raise RuntimeError("edge unreachable")

        class _Resp:
            status_code = status
        return _Resp()


def _install_e2e_stub(
    monkeypatch,
    statuses: list[int | None] | None = None,
    *,
    error_factory=None,
) -> _StubE2EClient:
    """Point httpx.Client at a stub so e2e probes never touch the network."""
    stub = _StubE2EClient(statuses, error_factory=error_factory)
    monkeypatch.setattr(httpx, "Client", stub)
    monkeypatch.setattr(supervisor.httpx, "Client", stub)
    return stub


def _quiesce(s) -> None:
    """Stop the background loop and reset reconnect bookkeeping for a test.

    The health loop would otherwise race the explicit ``_run_probe_cycle``
    calls and make probe counts non-deterministic.
    """
    s._stop_health_loop()
    s.tunnel.reconnect_calls.clear()
    with s._state_lock:
        s._tunnel_disconnected_since = None
        s._last_tunnel_reconnect_ts = 0.0
        s._last_e2e_success_ts = 0.0
        s._e2e_consecutive_failures = 0
        s._e2e_recovery_attempted = False
        s._e2e_dns_repair_attempted = False
        s._e2e_backoff_until = 0.0
        s._e2e_backoff_step = 0
        s._tunnel_e2e_kind = None
        s._tunnel_e2e_status_code = None
        s._consecutive_ok = 0
        s._consecutive_failures = 0
        s._tunnel_conns = 0
        s._tunnel_conns_expected = 0


class TestE2EFailureCounter:
    """_probe_tunnel_e2e must count only probes that actually ran."""

    def test_failure_increments_and_success_resets(self, monkeypatch, tmp_path):
        s = _make_sup_v2(monkeypatch, tmp_path)
        _install_e2e_stub(monkeypatch, [None, 502, 200])

        assert s._probe_tunnel_e2e(ignore_throttle=True).ok is False
        assert s._e2e_consecutive_failures == 1
        assert s._probe_tunnel_e2e(ignore_throttle=True).ok is False
        assert s._e2e_consecutive_failures == 2
        assert s._probe_tunnel_e2e(ignore_throttle=True).ok is True
        assert s._e2e_consecutive_failures == 0

    def test_success_records_the_throttle_timestamp(self, monkeypatch, tmp_path):
        s = _make_sup_v2(monkeypatch, tmp_path)
        _install_e2e_stub(monkeypatch, [200])
        with s._state_lock:
            s._last_e2e_success_ts = 0.0

        assert s._probe_tunnel_e2e().ok is True
        assert s._last_e2e_success_ts > 0

    def test_failure_does_not_arm_the_throttle(self, monkeypatch, tmp_path):
        """A failing probe must be retried next cycle, not thrott(led) away."""
        s = _make_sup_v2(monkeypatch, tmp_path)
        stub = _install_e2e_stub(monkeypatch, [None])

        assert s._probe_tunnel_e2e().ok is False
        assert s._last_e2e_success_ts == 0.0
        assert s._probe_tunnel_e2e().ok is False
        assert stub.calls == 2
        assert s._e2e_consecutive_failures == 2

    def test_throttled_skip_does_not_clear_the_streak(self, monkeypatch, tmp_path):
        """The skip also returns True; it must not look like a success."""
        s = _make_sup_v2(monkeypatch, tmp_path)
        stub = _install_e2e_stub(monkeypatch, [200])
        with s._state_lock:
            s._e2e_consecutive_failures = 1
            s._last_e2e_success_ts = time.time()

        result = s._probe_tunnel_e2e()
        assert result.ok is True
        assert result.skipped is True
        assert stub.calls == 0
        assert s._e2e_consecutive_failures == 1

    def test_missing_hostname_skip_does_not_clear_the_streak(self, monkeypatch, tmp_path):
        monkeypatch.setenv("KIRO_GATEWAY_TRAY_HOME", str(tmp_path))
        cfg = appconfig.load()
        cfg.cloudflare.hostname = ""
        appconfig.save(cfg)
        s = supervisor.Supervisor(
            gateway=_FakeGateway(), tunnel=_FakeTunnelWithReconnect()
        )
        with s._state_lock:
            s._e2e_consecutive_failures = 2

        result = s._probe_tunnel_e2e()
        assert result.ok is True
        assert result.skipped is True
        assert s._e2e_consecutive_failures == 2

    def test_ignore_throttle_failure_still_counts(self, monkeypatch, tmp_path):
        """Post-reconnect verification failures are real failures too."""
        s = _make_sup_v2(monkeypatch, tmp_path)
        _install_e2e_stub(monkeypatch, [None])
        with s._state_lock:
            s._last_e2e_success_ts = time.time()

        assert s._probe_tunnel_e2e(ignore_throttle=True).ok is False
        assert s._e2e_consecutive_failures == 1

    def test_tunnel_restart_clears_the_streak_but_not_the_latch(self, monkeypatch, tmp_path):
        s = _make_sup_v2(monkeypatch, tmp_path)
        monkeypatch.setattr(s, "_reprovision_if_deleted", lambda: False)
        with s._state_lock:
            s._e2e_consecutive_failures = 2
            s._e2e_recovery_attempted = True

        s._restart_tunnel_process("test")
        assert s._e2e_consecutive_failures == 0
        assert s._e2e_recovery_attempted is True

    def test_stop_clears_the_streak(self, monkeypatch, tmp_path):
        s = _make_sup_v2(monkeypatch, tmp_path)
        with s._state_lock:
            s._e2e_consecutive_failures = 2
            s._e2e_recovery_attempted = True

        s.stop()
        assert s._e2e_consecutive_failures == 0
        assert s._e2e_recovery_attempted is False


class TestE2ESoftReconnect:
    """Repeated end-to-end failures must drive recovery, not just a label."""

    def _prepare(self, monkeypatch, tmp_path):
        s = _make_sup_v2(monkeypatch, tmp_path)
        s.start()
        _quiesce(s)
        s.gateway.started = True
        s.tunnel.started = True
        monkeypatch.setattr(s, "_probe_gateway_once", lambda: True)
        monkeypatch.setattr(s._client, "get", lambda *a, **k: _ReadyResponse(4))
        monkeypatch.setattr(s, "_reprovision_if_deleted", lambda: False)
        return s

    def test_single_failure_does_not_reconnect(self, monkeypatch, tmp_path):
        """One blip on the public path is noise, not a reason to churn."""
        s = self._prepare(monkeypatch, tmp_path)
        _install_e2e_stub(monkeypatch, [None])

        s._run_probe_cycle()
        assert s._e2e_consecutive_failures == 1
        assert s.tunnel.reconnect_calls == []
        assert s._tunnel_e2e_ok is False
        s.close()

    def test_two_consecutive_failures_soft_reconnect(self, monkeypatch, tmp_path):
        s = self._prepare(monkeypatch, tmp_path)
        _install_e2e_stub(monkeypatch, [None])

        s._run_probe_cycle()
        s._run_probe_cycle()
        # Expected connection count learned from /ready is passed through.
        assert s.tunnel.reconnect_calls == [4]
        # Latch is set after the reconnect; further failures must not reconnect
        # until success or start/stop/request_tunnel_reconnect.
        assert s._e2e_consecutive_failures == 0
        assert s._e2e_recovery_attempted is True
        s.close()

    def test_recovery_between_failures_resets_the_streak(self, monkeypatch, tmp_path):
        """fail, succeed, fail must not add up to a reconnect."""
        s = self._prepare(monkeypatch, tmp_path)
        _install_e2e_stub(monkeypatch, [None, 200, None])

        s._run_probe_cycle()
        s._run_probe_cycle()
        assert s._e2e_consecutive_failures == 0
        # The success armed the 60s throttle; expire it so the third cycle
        # really probes instead of being skipped.
        with s._state_lock:
            s._last_e2e_success_ts = time.time() - s._E2E_PROBE_INTERVAL - 1
        s._run_probe_cycle()
        assert s._e2e_consecutive_failures == 1
        assert s.tunnel.reconnect_calls == []
        s.close()

    def test_throttled_cycles_do_not_reach_the_threshold(self, monkeypatch, tmp_path):
        """Cycles skipped by the 60s throttle must neither count nor reset."""
        s = self._prepare(monkeypatch, tmp_path)
        stub = _install_e2e_stub(monkeypatch, [None])
        with s._state_lock:
            s._e2e_consecutive_failures = 1
            s._last_e2e_success_ts = time.time()

        s._run_probe_cycle()
        s._run_probe_cycle()
        assert stub.calls == 0
        assert s._e2e_consecutive_failures == 1
        assert s.tunnel.reconnect_calls == []
        s.close()

    def test_zero_conns_skips_the_e2e_path(self, monkeypatch, tmp_path):
        """No edge connections: the zero-conn path owns recovery, not e2e."""
        s = self._prepare(monkeypatch, tmp_path)
        monkeypatch.setattr(s._client, "get", lambda *a, **k: _ReadyResponse(0))
        stub = _install_e2e_stub(monkeypatch, [None])

        s._run_probe_cycle()
        assert stub.calls == 0
        assert s._e2e_consecutive_failures == 0
        # The zero-conn branch still soft-reconnects on the first sighting.
        assert s.tunnel.reconnect_calls == [1]
        s.close()

    def test_cooldown_suppresses_the_e2e_reconnect(self, monkeypatch, tmp_path):
        s = self._prepare(monkeypatch, tmp_path)
        _install_e2e_stub(monkeypatch, [None])
        with s._state_lock:
            s._last_tunnel_reconnect_ts = time.time()

        s._run_probe_cycle()
        s._run_probe_cycle()
        assert s.tunnel.reconnect_calls == []
        s.close()

    def test_dead_control_channel_escalates_to_restart(self, monkeypatch, tmp_path):
        tun = _FakeTunnelWithReconnect()
        tun.request_reconnect = lambda count: False
        s = _make_sup_v2(monkeypatch, tmp_path, tunnel=tun)
        s.start()
        _quiesce(s)
        s.gateway.started = True
        s.tunnel.started = True
        monkeypatch.setattr(s, "_probe_gateway_once", lambda: True)
        monkeypatch.setattr(s._client, "get", lambda *a, **k: _ReadyResponse(4))
        _install_e2e_stub(monkeypatch, [None])

        restarts: list[str] = []
        monkeypatch.setattr(
            s, "_restart_tunnel_process", lambda reason: restarts.append(reason)
        )

        s._run_probe_cycle()
        s._run_probe_cycle()
        assert restarts == ["tunnel_e2e_failed"]
        # The restarted process starts from "connecting", and the zero-conn
        # escalation clock must not have been armed off the dead child's count.
        assert s._tunnel_conns == 0
        assert s._tunnel_connected is False
        assert s._tunnel_disconnected_since is None
        s.close()


class TestProbeCadence:
    """Cadence must stay tight whenever the tunnel is not healthily running."""

    def _prepare(self, monkeypatch, tmp_path, conns: int):
        s = _make_sup_v2(monkeypatch, tmp_path)
        _quiesce(s)
        s.gateway.started = True
        s.tunnel.started = True
        monkeypatch.setattr(s, "_probe_gateway_once", lambda: True)
        monkeypatch.setattr(s._client, "get", lambda *a, **k: _ReadyResponse(conns))
        monkeypatch.setattr(s, "_probe_tunnel_e2e", lambda **kw: True)
        monkeypatch.setattr(s, "_reprovision_if_deleted", lambda: False)
        return s

    def test_healthy_gateway_and_tunnel_relaxes(self, monkeypatch, tmp_path):
        s = self._prepare(monkeypatch, tmp_path, conns=4)

        # First cycle is never relaxed: the gateway has not proven stable yet.
        assert s._run_probe_cycle() is False
        assert s._run_probe_cycle() is True
        assert s._tunnel_status() == "running"

    def test_zero_connections_stays_active(self, monkeypatch, tmp_path):
        s = self._prepare(monkeypatch, tmp_path, conns=0)

        assert s._run_probe_cycle() is False
        assert s._run_probe_cycle() is False
        # The gateway on its own has proven stable by now, so the tunnel is the
        # only thing still holding the cadence tight.
        assert s._consecutive_ok >= 2
        assert s._run_probe_cycle() is False
        assert s._tunnel_status() == "connecting"

    def test_degraded_connections_stay_active(self, monkeypatch, tmp_path):
        s = self._prepare(monkeypatch, tmp_path, conns=3)
        with s._state_lock:
            s._tunnel_conns_expected = 4

        s._run_probe_cycle()
        assert s._tunnel_status() == "degraded"
        assert s._run_probe_cycle() is False

    def test_degraded_edge_stays_active(self, monkeypatch, tmp_path):
        """Full connection count but an unreachable edge is still unsettled."""
        s = self._prepare(monkeypatch, tmp_path, conns=4)
        monkeypatch.setattr(s, "_probe_tunnel_e2e", lambda **kw: False)
        monkeypatch.setattr(s, "_issue_soft_reconnect", lambda reason: "skipped")

        s._run_probe_cycle()
        assert s._run_probe_cycle() is False
        assert s._tunnel_status() == "degraded"

    def test_dead_tunnel_with_healthy_gateway_stays_active(self, monkeypatch, tmp_path):
        s = self._prepare(monkeypatch, tmp_path, conns=0)
        s.tunnel.started = False

        s._run_probe_cycle()
        assert s._tunnel_status() == "stopped"
        assert s._run_probe_cycle() is False

    def test_dead_gateway_relaxes_regardless_of_tunnel(self, monkeypatch, tmp_path):
        """Nothing is settling when the gateway is intentionally down."""
        s = self._prepare(monkeypatch, tmp_path, conns=0)
        s.gateway.started = False

        assert s._run_probe_cycle() is True
        assert s._tunnel_status() in ("connecting", "stopped")

    def test_unhealthy_gateway_stays_active(self, monkeypatch, tmp_path):
        s = self._prepare(monkeypatch, tmp_path, conns=4)
        monkeypatch.setattr(s, "_probe_gateway_once", lambda: False)

        assert s._run_probe_cycle() is False


def _chained(outer: BaseException, inner: BaseException) -> BaseException:
    outer.__cause__ = inner
    return outer


def _dns_error() -> BaseException:
    return _chained(
        httpx.ConnectError("Failed to resolve host"),
        socket.gaierror(8, "nodename nor servname provided, or not known"),
    )


def _tls_error() -> BaseException:
    return _chained(
        httpx.ConnectError("TLS handshake failed"),
        ssl.SSLError("CERTIFICATE_VERIFY_FAILED"),
    )


class TestE2EClassification:
    """Module-level exception-chain classification, no network involved."""

    def test_gaierror_cause_is_dns(self):
        assert supervisor._classify_e2e_exception(_dns_error()) == "dns"

    def test_sslerror_cause_is_tls(self):
        assert supervisor._classify_e2e_exception(_tls_error()) == "tls"

    def test_sslerror_context_is_tls(self):
        outer = httpx.ConnectError("failed")
        inner = ssl.SSLError("CERTIFICATE_VERIFY_FAILED")
        outer.__context__ = inner
        assert supervisor._classify_e2e_exception(outer) == "tls"

    def test_proxyerror_is_proxy(self):
        assert supervisor._classify_e2e_exception(httpx.ProxyError("proxy failed")) == "proxy"

    def test_timeout_is_timeout(self):
        assert supervisor._classify_e2e_exception(httpx.TimeoutException("timed out")) == "timeout"

    def test_connect_error_without_cause_is_connect(self):
        assert supervisor._classify_e2e_exception(httpx.ConnectError("refused")) == "connect"

    def test_runtime_error_is_unexpected(self):
        assert supervisor._classify_e2e_exception(RuntimeError("edge unreachable")) == "unexpected"

    def test_dns_wins_over_connect_in_the_same_chain(self):
        assert supervisor._classify_e2e_exception(_dns_error()) == "dns"

    def test_http_5xx_is_recoverable(self):
        result = supervisor.E2EProbeResult(
            ok=False, kind="http_status", status_code=502
        )
        assert supervisor._e2e_is_recoverable(result) is True

    def test_http_4xx_is_not_recoverable(self):
        result = supervisor.E2EProbeResult(
            ok=False, kind="http_status", status_code=404
        )
        assert supervisor._e2e_is_recoverable(result) is False

    def test_dns_is_not_recoverable(self):
        result = supervisor.E2EProbeResult(ok=False, kind="dns")
        assert supervisor._e2e_is_recoverable(result) is False

    def test_sanitize_strips_query_and_secrets_and_truncates(self):
        raw = (
            "GET https://x.example/health?token=abc123 "
            "Authorization: Bearer secret-value " + ("B" * 400)
        )
        out = supervisor._sanitize_e2e_detail(raw)
        assert "abc123" not in out
        assert "secret-value" not in out
        assert "Bearer" not in out
        assert len(out) <= supervisor._E2E_DETAIL_MAX_LEN + 3


class TestE2EKindReconnectPolicy:
    """Recoverable kinds reconnect once; local/config failures never do."""

    def _prepare(self, monkeypatch, tmp_path):
        s = _make_sup_v2(monkeypatch, tmp_path)
        s.start()
        _quiesce(s)
        s.gateway.started = True
        s.tunnel.started = True
        monkeypatch.setattr(s, "_probe_gateway_once", lambda: True)
        monkeypatch.setattr(s._client, "get", lambda *a, **k: _ReadyResponse(4))
        monkeypatch.setattr(s, "_reprovision_if_deleted", lambda: False)
        return s

    def test_dns_failures_do_not_reconnect(self, monkeypatch, tmp_path):
        s = self._prepare(monkeypatch, tmp_path)
        _install_e2e_stub(monkeypatch, error_factory=_dns_error)

        s._run_probe_cycle()
        s._run_probe_cycle()
        s._run_probe_cycle()
        assert s.tunnel.reconnect_calls == []
        assert s._e2e_recovery_attempted is False
        assert s._e2e_consecutive_failures >= 1
        assert s.status()["tunnel"] == "degraded"
        assert s.status()["tunnel_detail"] == "DNS 解析失败"
        s.close()

    def test_tls_failures_do_not_reconnect(self, monkeypatch, tmp_path):
        s = self._prepare(monkeypatch, tmp_path)
        _install_e2e_stub(monkeypatch, error_factory=_tls_error)

        s._run_probe_cycle()
        s._run_probe_cycle()
        assert s.tunnel.reconnect_calls == []
        assert s._e2e_recovery_attempted is False
        assert s.status()["tunnel_detail"] == "TLS 失败"
        s.close()

    def test_http_404_does_not_reconnect(self, monkeypatch, tmp_path):
        s = self._prepare(monkeypatch, tmp_path)
        _install_e2e_stub(monkeypatch, [404])

        s._run_probe_cycle()
        s._run_probe_cycle()
        assert s.tunnel.reconnect_calls == []
        assert s._e2e_recovery_attempted is False
        assert s.status()["tunnel_detail"] == "HTTP 404"
        s.close()

    def test_two_502s_reconnect_once_then_latch(self, monkeypatch, tmp_path):
        s = self._prepare(monkeypatch, tmp_path)
        _install_e2e_stub(monkeypatch, [502])

        s._run_probe_cycle()
        s._run_probe_cycle()
        assert s.tunnel.reconnect_calls == [4]
        assert s._e2e_recovery_attempted is True
        assert s.status()["tunnel_detail"] == "HTTP 502"

        s._run_probe_cycle()
        s._run_probe_cycle()
        s._run_probe_cycle()
        assert s.tunnel.reconnect_calls == [4]
        s.close()

    def test_success_clears_latch_and_allows_another_recovery(self, monkeypatch, tmp_path):
        s = self._prepare(monkeypatch, tmp_path)
        stub = _install_e2e_stub(monkeypatch, [502, 502, 200, 502, 502])

        s._run_probe_cycle()
        s._run_probe_cycle()
        assert s.tunnel.reconnect_calls == [4]
        assert s._e2e_recovery_attempted is True

        s._run_probe_cycle()
        assert stub.calls == 3
        assert s._e2e_recovery_attempted is False
        with s._state_lock:
            s._last_e2e_success_ts = 0.0
            s._last_tunnel_reconnect_ts = 0.0

        s._run_probe_cycle()
        s._run_probe_cycle()
        assert s.tunnel.reconnect_calls == [4, 4]
        assert s._e2e_recovery_attempted is True
        s.close()

    def test_timeout_consecutive_failures_reconnect_once(self, monkeypatch, tmp_path):
        s = self._prepare(monkeypatch, tmp_path)
        _install_e2e_stub(
            monkeypatch,
            error_factory=lambda: httpx.TimeoutException("timed out"),
        )

        s._run_probe_cycle()
        s._run_probe_cycle()
        assert s.tunnel.reconnect_calls == [4]
        assert s.status()["tunnel_detail"] == "连接超时"

        s._run_probe_cycle()
        s._run_probe_cycle()
        assert s.tunnel.reconnect_calls == [4]
        s.close()

    def test_connect_consecutive_failures_reconnect_once(self, monkeypatch, tmp_path):
        s = self._prepare(monkeypatch, tmp_path)
        _install_e2e_stub(
            monkeypatch,
            error_factory=lambda: httpx.ConnectError("connection refused"),
        )

        s._run_probe_cycle()
        s._run_probe_cycle()
        assert s.tunnel.reconnect_calls == [4]
        assert s.status()["tunnel_detail"] == "连接失败"
        s._run_probe_cycle()
        assert s.tunnel.reconnect_calls == [4]
        s.close()

    def test_cooldown_skip_does_not_set_latch(self, monkeypatch, tmp_path):
        s = self._prepare(monkeypatch, tmp_path)
        _install_e2e_stub(monkeypatch, [502])
        with s._state_lock:
            s._last_tunnel_reconnect_ts = time.time()

        s._run_probe_cycle()
        s._run_probe_cycle()
        assert s.tunnel.reconnect_calls == []
        assert s._e2e_recovery_attempted is False
        assert s._e2e_consecutive_failures >= 2

        with s._state_lock:
            s._last_tunnel_reconnect_ts = 0.0
        s._run_probe_cycle()
        assert s.tunnel.reconnect_calls == [4]
        assert s._e2e_recovery_attempted is True
        s.close()

    def test_start_clears_latch_and_failure_count(self, monkeypatch, tmp_path):
        s = _make_sup_v2(monkeypatch, tmp_path)
        with s._state_lock:
            s._e2e_recovery_attempted = True
            s._e2e_dns_repair_attempted = True
            s._e2e_consecutive_failures = 3

        s.start()
        s._stop_health_loop()
        assert s._e2e_recovery_attempted is False
        assert s._e2e_dns_repair_attempted is False
        assert s._e2e_consecutive_failures == 0
        s.close()

    def test_request_tunnel_reconnect_clears_latch(self, monkeypatch, tmp_path):
        s = self._prepare(monkeypatch, tmp_path)
        _install_e2e_stub(monkeypatch, [502])
        s._run_probe_cycle()
        s._run_probe_cycle()
        assert s._e2e_recovery_attempted is True

        class _NoStartThread:
            def __init__(self, *args, **kwargs):
                pass

            def start(self):
                pass

        monkeypatch.setattr(supervisor.threading, "Thread", _NoStartThread)
        with s._state_lock:
            s._last_tunnel_reconnect_ts = 0.0
        s.request_tunnel_reconnect("network_change")
        assert s._e2e_recovery_attempted is False
        with s._state_lock:
            s._last_tunnel_reconnect_ts = 0.0

        s._run_probe_cycle()
        s._run_probe_cycle()
        assert len(s.tunnel.reconnect_calls) >= 3
        s.close()


class TestE2EDegradedReasons:
    """Menu copy follows e2e kind; unknown kind keeps the legacy fallback."""

    def test_kind_texts(self, monkeypatch, tmp_path):
        s = _make_sup_v2(monkeypatch, tmp_path)
        s.tunnel.started = True
        cases = (
            ("dns", None, "DNS 解析失败"),
            ("tls", None, "TLS 失败"),
            ("proxy", None, "代理失败"),
            ("timeout", None, "连接超时"),
            ("connect", None, "连接失败"),
            ("http_status", 502, "HTTP 502"),
            ("unexpected", None, "边缘不可达"),
            (None, None, "边缘不可达"),
        )
        for kind, status_code, expected in cases:
            with s._state_lock:
                s._tunnel_conns = 4
                s._tunnel_conns_expected = 4
                s._tunnel_e2e_ok = False
                s._tunnel_e2e_kind = kind
                s._tunnel_e2e_status_code = status_code
            assert s._tunnel_detail() == expected, kind

    def test_snapshot_includes_kind(self, monkeypatch, tmp_path):
        s = _make_sup_v2(monkeypatch, tmp_path)
        with s._state_lock:
            s._tunnel_e2e_ok = False
            s._tunnel_e2e_kind = "dns"
            first = s._tunnel_status_snapshot()
            s._tunnel_e2e_kind = "tls"
            second = s._tunnel_status_snapshot()
        assert first != second


class TestE2ELogSanitization:
    """Probe logs must not contain credentials or response bodies."""

    def test_logs_omit_authorization_and_long_body(self, monkeypatch, tmp_path):
        secret = "Authorization: Bearer super-secret-token-xyz"
        body = "<html>" + ("Z" * 400) + "</html>"
        message = f"GET https://kg-test.example.com/health?api_key=leak {secret} {body}"

        captured: list[str] = []

        def _debug(msg, *args, **kwargs):
            try:
                captured.append(str(msg).format(*args) if args else str(msg))
            except (IndexError, KeyError, ValueError):
                captured.append(f"{msg} {args}")

        s = _make_sup_v2(monkeypatch, tmp_path)
        monkeypatch.setattr(supervisor.logger, "debug", _debug)
        _install_e2e_stub(monkeypatch, error_factory=lambda: RuntimeError(message))

        result = s._probe_tunnel_e2e(ignore_throttle=True)
        assert result.ok is False
        joined = "\n".join(captured)
        assert "tunnel e2e probe failed: kind=unexpected" in joined
        assert "super-secret-token-xyz" not in joined
        assert "api_key=leak" not in joined
        assert body not in joined
        assert "super-secret-token-xyz" not in (result.detail or "")
        assert "api_key=leak" not in (result.detail or "")
        assert body not in (result.detail or "")


def test_register_first_provision_does_not_set_notice(monkeypatch, tmp_path):
    s = _make_sup(monkeypatch, tmp_path, provisioned=False)
    import kiro_gateway_tray.provision as pmod
    monkeypatch.setattr(pmod, "run", lambda cfg, secret: ("kg-new.example.com", "eyJ", ""))
    cfg = appconfig.load()
    s.register(cfg, "secret")
    again = appconfig.load()
    assert again.cloudflare.hostname == "kg-new.example.com"
    assert again.cloudflare.url_changed_notice is False


def test_register_sets_notice_when_hostname_changes(monkeypatch, tmp_path):
    s = _make_sup(monkeypatch, tmp_path, provisioned=True)
    import kiro_gateway_tray.provision as pmod
    monkeypatch.setattr(pmod, "run", lambda cfg, secret: ("kg-new.example.com", "eyJ_new", ""))
    cfg = appconfig.load()
    s.register(cfg, "secret")
    again = appconfig.load()
    assert again.cloudflare.hostname == "kg-new.example.com"
    assert again.cloudflare.url_changed_notice is True
    assert again.cloudflare.run_token == "eyJ_new"


def test_migrate_identity_reprovisions_when_slug_differs(monkeypatch, tmp_path):
    s = _make_sup(monkeypatch, tmp_path, provisioned=True)
    cfg = appconfig.load()
    cfg.cloudflare.shared_secret = "act-code"
    cfg.cloudflare.provision_url = "https://w.example.com"
    appconfig.save(cfg)

    import kiro_gateway_tray.provision as pmod
    monkeypatch.setattr(pmod, "_get_username", lambda cfg: "deviceabcdef")
    monkeypatch.setattr(
        pmod, "run",
        lambda cfg, secret: ("kg-deviceabcdef.example.com", "eyJ_new", ""),
    )
    s.start()
    again = appconfig.load()
    assert again.cloudflare.hostname == "kg-deviceabcdef.example.com"
    assert again.cloudflare.url_changed_notice is True
    assert again.cloudflare.run_token == "eyJ_new"


def test_migrate_identity_noop_when_slug_matches(monkeypatch, tmp_path):
    s = _make_sup(monkeypatch, tmp_path, provisioned=True)
    cfg = appconfig.load()
    cfg.cloudflare.shared_secret = "act-code"
    appconfig.save(cfg)
    calls = []
    import kiro_gateway_tray.provision as pmod
    monkeypatch.setattr(
        pmod, "run",
        lambda cfg, secret: calls.append(1) or ("kg-x.example.com", "t", ""),
    )
    s.start()
    assert calls == []
    again = appconfig.load()
    assert again.cloudflare.hostname == "kg-test.example.com"
    assert again.cloudflare.url_changed_notice is False


def test_migrate_identity_skipped_without_secret(monkeypatch, tmp_path):
    s = _make_sup(monkeypatch, tmp_path, provisioned=True)
    import kiro_gateway_tray.provision as pmod
    monkeypatch.setattr(pmod, "_get_username", lambda cfg: "deviceabcdef")
    calls = []
    monkeypatch.setattr(
        pmod, "run",
        lambda cfg, secret: calls.append(1) or ("kg-deviceabcdef.example.com", "t", ""),
    )
    s.start()
    assert calls == []
    assert appconfig.load().cloudflare.hostname == "kg-test.example.com"


class TestInterceptIp:
    """Clash/Surge fake-ip must not be treated as a public edge address."""

    def test_rfc2544_and_clash_v6(self):
        assert supervisor._is_intercept_ip("198.18.0.75") is True
        assert supervisor._is_intercept_ip("198.19.255.255") is True
        assert supervisor._is_intercept_ip("fdfe:dcba:9876::4a") is True
        assert supervisor._is_intercept_ip("1.1.1.1") is False
        assert supervisor._is_intercept_ip("not-an-ip") is False

    def test_all_intercept_requires_every_addr(self, monkeypatch):
        monkeypatch.setattr(
            supervisor, "_system_addrs", lambda _h: ["198.18.0.75", "1.1.1.1"]
        )
        assert supervisor._all_intercept_addrs("kg-test.example.com") is False
        monkeypatch.setattr(
            supervisor, "_system_addrs", lambda _h: ["198.18.0.75"]
        )
        assert supervisor._all_intercept_addrs("kg-test.example.com") is True
        monkeypatch.setattr(supervisor, "_system_addrs", lambda _h: [])
        assert supervisor._all_intercept_addrs("kg-test.example.com") is False


class TestE2EFakeIpFallback:
    """Local TUN fake-ip TLS failures are reclassified via DoH."""

    def test_nxdomain_over_fake_ip_is_dns(self, monkeypatch, tmp_path):
        s = _make_sup_v2(monkeypatch, tmp_path)
        _install_e2e_stub(monkeypatch, error_factory=_tls_error)
        monkeypatch.setattr(supervisor, "_system_addrs", lambda _h: ["198.18.0.75"])
        monkeypatch.setattr(supervisor, "_doh_lookup_a", lambda _h: [])
        result = s._probe_tunnel_e2e(ignore_throttle=True)
        assert result.ok is False
        assert result.kind == "dns"
        assert result.detail == "public DNS NXDOMAIN"
        s.close()

    def test_public_ip_retry_succeeds(self, monkeypatch, tmp_path):
        s = _make_sup_v2(monkeypatch, tmp_path)
        _install_e2e_stub(monkeypatch, error_factory=_tls_error)
        monkeypatch.setattr(supervisor, "_system_addrs", lambda _h: ["198.18.0.75"])
        monkeypatch.setattr(supervisor, "_doh_lookup_a", lambda _h: ["104.16.1.1"])
        monkeypatch.setattr(
            supervisor, "_tls_http_get_status", lambda *a, **k: 200
        )
        result = s._probe_tunnel_e2e(ignore_throttle=True)
        assert result.ok is True
        assert result.kind == "ok"
        s.close()

    def test_doh_unreachable_keeps_tls(self, monkeypatch, tmp_path):
        s = _make_sup_v2(monkeypatch, tmp_path)
        _install_e2e_stub(monkeypatch, error_factory=_tls_error)
        monkeypatch.setattr(supervisor, "_system_addrs", lambda _h: ["198.18.0.75"])

        def _boom(_h):
            raise httpx.ConnectError("doh down")

        monkeypatch.setattr(supervisor, "_doh_lookup_a", _boom)
        result = s._probe_tunnel_e2e(ignore_throttle=True)
        assert result.ok is False
        assert result.kind == "tls"
        s.close()

    def test_real_tls_without_fake_ip_stays_tls(self, monkeypatch, tmp_path):
        s = _make_sup_v2(monkeypatch, tmp_path)
        _install_e2e_stub(monkeypatch, error_factory=_tls_error)
        monkeypatch.setattr(supervisor, "_system_addrs", lambda _h: ["104.16.1.1"])
        result = s._probe_tunnel_e2e(ignore_throttle=True)
        assert result.kind == "tls"
        s.close()


class TestEnsureDnsRepair:
    """Missing public CNAME is repaired once per incident, then latched."""

    def _prepare_with_secret(self, monkeypatch, tmp_path):
        s = _make_sup_v2(monkeypatch, tmp_path)
        s.start()
        _quiesce(s)
        s.gateway.started = True
        s.tunnel.started = True
        cfg = appconfig.load()
        cfg.cloudflare.shared_secret = "act-code"
        appconfig.save(cfg)
        s._cached_secret = "act-code"
        monkeypatch.setattr(s, "_probe_gateway_once", lambda: True)
        monkeypatch.setattr(s._client, "get", lambda *a, **k: _ReadyResponse(4))
        monkeypatch.setattr(s, "_reprovision_if_deleted", lambda: False)
        monkeypatch.setattr(supervisor, "_system_addrs", lambda _h: ["198.18.0.75"])
        monkeypatch.setattr(supervisor, "_doh_lookup_a", lambda _h: [])
        return s

    def test_repairs_once_then_latches(self, monkeypatch, tmp_path):
        s = self._prepare_with_secret(monkeypatch, tmp_path)
        _install_e2e_stub(monkeypatch, error_factory=_tls_error)
        import kiro_gateway_tray.provision as pmod
        calls = []

        def _outcome(*_a, **_k):
            calls.append(1)
            return pmod.EnsureDnsOutcome(
                status=True, repaired=True, api_record=True, authoritative=False
            )

        monkeypatch.setattr(pmod, "ensure_dns_outcome", _outcome)
        # First cycle: classify as dns, repair, then probe again (still dns).
        s._run_probe_cycle()
        assert calls == [1]
        assert s._e2e_dns_repair_attempted is True
        assert s.status()["tunnel_detail"] == "DNS 解析失败"
        s._run_probe_cycle()
        assert calls == [1]
        s.close()

    def test_no_secret_skips_repair(self, monkeypatch, tmp_path):
        s = _make_sup_v2(monkeypatch, tmp_path)
        s.start()
        _quiesce(s)
        s.gateway.started = True
        s.tunnel.started = True
        monkeypatch.setattr(s, "_probe_gateway_once", lambda: True)
        monkeypatch.setattr(s._client, "get", lambda *a, **k: _ReadyResponse(4))
        monkeypatch.setattr(s, "_reprovision_if_deleted", lambda: False)
        monkeypatch.setattr(supervisor, "_system_addrs", lambda _h: ["198.18.0.75"])
        monkeypatch.setattr(supervisor, "_doh_lookup_a", lambda _h: [])
        _install_e2e_stub(monkeypatch, error_factory=_tls_error)
        import kiro_gateway_tray.provision as pmod
        calls = []
        monkeypatch.setattr(pmod, "ensure_dns", lambda *a, **k: calls.append(1) or True)
        s._run_probe_cycle()
        assert calls == []
        s.close()

