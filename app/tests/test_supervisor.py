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

    # Patch provision.run to avoid real HTTP call
    import kiro_gateway_tray.provision as pmod
    monkeypatch.setattr(pmod, "run", lambda cfg, secret: ("kg-cb.example.com", "eyJ_cb"))
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
