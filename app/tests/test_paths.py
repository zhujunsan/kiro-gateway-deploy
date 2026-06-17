# app/tests/test_paths.py
from kiro_gateway_tray import paths


def test_dirs_are_absolute_and_namespaced():
    cfg = paths.config_dir()
    data = paths.data_dir()
    log = paths.log_dir()
    for p in (cfg, data, log):
        assert p.is_absolute()
        assert "KiroGatewayTray" in str(p) or "kiro-gateway-tray" in str(p).lower()


def test_config_file_lives_in_config_dir():
    assert paths.config_file().parent == paths.config_dir()
    assert paths.config_file().name == "config.toml"


def test_ensure_dirs_creates_them(tmp_path, monkeypatch):
    monkeypatch.setenv("KIRO_GATEWAY_TRAY_HOME", str(tmp_path))
    monkeypatch.setattr(paths, "_OVERRIDE", None, raising=False)
    paths.ensure_dirs()
    assert paths.data_dir().exists()
    assert paths.log_dir().exists()
