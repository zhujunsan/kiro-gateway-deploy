import time
from kiro_gateway_tray import supervisor, appconfig


class _FakeGateway:
    def __init__(self): self.started = False
    def start(self, cfg): self.started = True
    def stop(self): self.started = False
    def is_alive(self): return self.started


class _FakeTunnel:
    def __init__(self): self.started = False
    def start(self, cfg): self.started = True
    def stop(self): self.started = False
    def is_alive(self): return self.started


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


def test_run_probe_cycle_auto_restarts_tunnel_on_timeout(monkeypatch, tmp_path):
    s = _make_sup(monkeypatch, tmp_path)
    s.start()

    # Gateway is alive and healthy, tunnel is alive but not ready
    s.gateway.started = True
    s.tunnel.started = True

    class _Resp:
        status_code = 200

    monkeypatch.setattr(s._client, "get", lambda *a, **k: _Resp())
    monkeypatch.setattr(s, "_probe_tunnel_ready", lambda: False)

    # Mock _reprovision_if_deleted to return False (tunnel exists, just restart)
    monkeypatch.setattr(s, "_reprovision_if_deleted", lambda: False)

    # First probe: sets the disconnected timestamp
    s._run_probe_cycle()
    assert s._tunnel_disconnected_since is not None
    initial_ts = s._tunnel_disconnected_since

    # Check that it didn't restart yet
    restarted = []
    monkeypatch.setattr(s.tunnel, "stop", lambda: restarted.append("stop"))
    monkeypatch.setattr(s.tunnel, "start", lambda cfg: restarted.append("start"))

    # Run probe again with no time advancement: should not restart
    s._run_probe_cycle()
    assert not restarted
    assert s._tunnel_disconnected_since == initial_ts

    # Mock time advancement beyond _TUNNEL_RECONNECT_TIMEOUT
    fake_time = initial_ts + s._TUNNEL_RECONNECT_TIMEOUT + 5
    monkeypatch.setattr(time, "time", lambda: fake_time)

    s._run_probe_cycle()
    assert "stop" in restarted
    assert "start" in restarted
    assert s._tunnel_disconnected_since is None


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
        self._metrics_port = 20241

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
        assert result is True

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
        assert result is True

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

        import httpx
        monkeypatch.setattr(httpx, "Client", lambda **kw: _FakeClient())
        result = s._probe_tunnel_e2e(ignore_throttle=True)
        assert result is True
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


class TestTunnelTimeout60sBehavior:
    """60s fallback behavior must not regress."""

    def test_timeout_triggers_restart(self, monkeypatch, tmp_path):
        s = _make_sup_v2(monkeypatch, tmp_path)
        s.start()
        s.gateway.started = True
        s.tunnel.started = True

        monkeypatch.setattr(s, "_probe_gateway_once", lambda: True)
        monkeypatch.setattr(s._client, "get", lambda *a, **k: _ReadyResponse(0))
        monkeypatch.setattr(s, "_probe_tunnel_e2e", lambda **kw: True)
        monkeypatch.setattr(s, "_reprovision_if_deleted", lambda: False)

        # First probe: sets disconnected_since
        s._run_probe_cycle()
        assert s._tunnel_disconnected_since is not None
        initial_ts = s._tunnel_disconnected_since

        restart_calls = []
        monkeypatch.setattr(
            s, "_restart_tunnel_process",
            lambda reason: restart_calls.append(reason),
        )

        # Time jump beyond timeout
        fake_time = initial_ts + s._TUNNEL_RECONNECT_TIMEOUT + 5
        monkeypatch.setattr(time, "time", lambda: fake_time)

        s._run_probe_cycle()
        assert "tunnel_timeout" in restart_calls


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
