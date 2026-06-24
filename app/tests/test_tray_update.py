# app/tests/test_tray_update.py
from kiro_gateway_tray import updates
from kiro_gateway_tray.tray import TrayApp


def test_ensure_update_info_sync_from_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("KIRO_GATEWAY_TRAY_HOME", str(tmp_path))
    updates._write_cache(latest="v9.9.9")

    app = TrayApp()
    assert app._update_info is None

    app._ensure_update_info_sync()
    assert app._update_info is not None
    assert app._update_info.latest == "v9.9.9"


def test_update_visible_peeks_cache_before_async(tmp_path, monkeypatch):
    monkeypatch.setenv("KIRO_GATEWAY_TRAY_HOME", str(tmp_path))
    updates._write_cache(latest="v9.9.9")

    app = TrayApp()
    assert app._update_visible(None) is True
    assert app._update_info is not None


def test_version_line_shows_ahead_of_release(tmp_path, monkeypatch):
    monkeypatch.setenv("KIRO_GATEWAY_TRAY_HOME", str(tmp_path))
    updates._write_cache(latest="v0.1.17")

    app = TrayApp()
    line = app._version_line(None)
    assert "高于发布版 0.1.17" in line


def test_version_line_shows_upgrade_available(tmp_path, monkeypatch):
    monkeypatch.setenv("KIRO_GATEWAY_TRAY_HOME", str(tmp_path))
    updates._write_cache(latest="v9.9.9")
    monkeypatch.setattr("kiro_gateway_tray.tray.__version__", "0.1.0")

    app = TrayApp()
    line = app._version_line(None)
    assert "可升级 9.9.9" in line
