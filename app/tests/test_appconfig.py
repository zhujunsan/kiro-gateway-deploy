from kiro_tray import appconfig


def test_defaults_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("KIRO_TRAY_HOME", str(tmp_path))
    cfg = appconfig.load()
    assert cfg.gateway.port == 18000
    assert cfg.cloudflare.hostname == ""
    assert cfg.cloudflare.run_token == ""
    assert appconfig.path().exists()


def test_edit_and_reload(tmp_path, monkeypatch):
    monkeypatch.setenv("KIRO_TRAY_HOME", str(tmp_path))
    cfg = appconfig.load()
    cfg.gateway.proxy_api_key = "secret123"
    cfg.cloudflare.hostname = "kg-alice.botsonny.top"
    cfg.cloudflare.run_token = "eyJ_test"
    appconfig.save(cfg)
    again = appconfig.load()
    assert again.gateway.proxy_api_key == "secret123"
    assert again.cloudflare.hostname == "kg-alice.botsonny.top"
    assert again.cloudflare.run_token == "eyJ_test"


def test_to_env_maps_known_keys(tmp_path, monkeypatch):
    monkeypatch.setenv("KIRO_TRAY_HOME", str(tmp_path))
    cfg = appconfig.load()
    cfg.gateway.profile_arn = "arn:x"
    cfg.gateway.proxy_api_key = "k"
    env = appconfig.to_gateway_env(cfg)
    assert env["PROFILE_ARN"] == "arn:x"
    assert env["PROXY_API_KEY"] == "k"
    assert env["SERVER_HOST"] == "127.0.0.1"
    assert env["SERVER_PORT"] == "18000"
    assert env["FAKE_REASONING"] == "false"


def test_is_provisioned(tmp_path, monkeypatch):
    monkeypatch.setenv("KIRO_TRAY_HOME", str(tmp_path))
    cfg = appconfig.load()
    assert appconfig.is_provisioned(cfg) is False
    cfg.cloudflare.hostname = "kg-alice.botsonny.top"
    cfg.cloudflare.run_token = "eyJ_test"
    assert appconfig.is_provisioned(cfg) is True
