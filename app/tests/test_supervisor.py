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

