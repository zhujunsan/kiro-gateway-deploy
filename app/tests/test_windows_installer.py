from pathlib import Path


PACKAGING = Path(__file__).resolve().parents[1] / "packaging"
ISS = PACKAGING / "kiro_gateway_tray.iss"
ZH_ISL = PACKAGING / "languages" / "ChineseSimplified.isl"


def test_windows_installer_stops_running_app_before_copying_files():
    script = ISS.read_text(encoding="utf-8")

    assert "function PrepareToInstall" in script
    assert "KiroGatewayTray.exe" in script
    assert "cloudflared.exe" in script
    assert "taskkill" in script.lower()


def test_windows_installer_prompts_before_closing_running_app():
    script = ISS.read_text(encoding="utf-8")

    # The Restart Manager must not silently close/restart the app; we ask first.
    assert "CloseApplications=no" in script
    assert "RestartApplications=no" in script
    # The prompt and the cancel-on-No path must be present.
    assert "MB_YESNO" in script
    assert "function IsProcessRunning" in script
    assert "Setup was cancelled" in script


def test_windows_installer_sets_uninstall_menu_icon():
    script = ISS.read_text(encoding="utf-8")

    assert "UninstallDisplayName=Kiro Gateway Tray" in script
    assert "UninstallDisplayIcon={app}\\KiroGatewayTray.exe" in script


def test_windows_installer_has_fixed_appid_for_clean_upgrades():
    script = ISS.read_text(encoding="utf-8")

    # A stable AppId GUID is what lets Inno recognise upgrades / clean uninstalls.
    assert "AppId={{8A76AFC5-28C9-450B-94DF-1DE5BECB6EAF}" in script


def test_windows_installer_stamps_version_and_platform_guards():
    script = ISS.read_text(encoding="utf-8")

    assert "VersionInfoVersion={#AppVersion}" in script
    assert "ArchitecturesAllowed=x64compatible" in script
    assert "MinVersion=10.0" in script
    assert "PrivilegesRequiredOverridesAllowed=dialog" in script


def test_windows_installer_bundles_simplified_chinese_language():
    script = ISS.read_text(encoding="utf-8")

    assert 'MessagesFile: "languages\\ChineseSimplified.isl"' in script
    # The isl must be vendored next to the script (not Inno's built-ins, which
    # don't ship Simplified Chinese) and decode as UTF-8.
    assert ZH_ISL.is_file()
    ZH_ISL.read_bytes().decode("utf-8")
    assert "chinesesimplified.AppRunningPrompt=" in script
