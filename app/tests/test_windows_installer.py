from pathlib import Path


ISS = Path(__file__).resolve().parents[1] / "packaging" / "kiro_gateway_tray.iss"


def test_windows_installer_stops_running_app_before_copying_files():
    script = ISS.read_text(encoding="utf-8")

    assert "CloseApplications=yes" in script
    assert "RestartApplications=yes" in script
    assert "function PrepareToInstall" in script
    assert "KiroGatewayTray.exe" in script
    assert "cloudflared.exe" in script
    assert "taskkill" in script.lower()
