# app/tests/test_gateway.py
from pathlib import Path
from kiro_gateway_tray import gateway, appconfig


def test_vendor_root_missing_raises(monkeypatch):
    monkeypatch.setattr(gateway, "_candidate_vendor_roots", lambda: [Path("/no/such")])
    try:
        gateway._vendor_root()
        assert False, "expected RuntimeError"
    except RuntimeError as e:
        assert "vendor" in str(e).lower()


def test_apply_env_sets_process_env(tmp_path, monkeypatch):
    monkeypatch.setenv("KIRO_GATEWAY_TRAY_HOME", str(tmp_path))
    cfg = appconfig.load()
    cfg.gateway.profile_arn = "arn:test"
    cfg.gateway.proxy_api_key = "k123"
    gateway._apply_env(cfg)
    import os
    assert os.environ["PROFILE_ARN"] == "arn:test"
    assert os.environ["PROXY_API_KEY"] == "k123"
    assert os.environ["SERVER_HOST"] == "127.0.0.1"


def test_child_command_source_mode(monkeypatch):
    monkeypatch.setattr(gateway.sys, "frozen", False, raising=False)
    cmd = gateway._child_command()
    assert cmd[1:] == ["-m", "kiro_gateway_tray", "--run-gateway"]


def test_child_command_frozen_mode(monkeypatch):
    monkeypatch.setattr(gateway.sys, "frozen", True, raising=False)
    monkeypatch.setattr(gateway.sys, "executable", "/Apps/KiroGatewayTray", raising=False)
    cmd = gateway._child_command()
    assert cmd == ["/Apps/KiroGatewayTray", "--run-gateway"]
