# app/tests/test_autostart.py
import sys

from kiro_gateway_tray import autostart


def test_is_supported_true_on_known_platforms():
    assert autostart.is_supported() is True


def test_launch_argv_from_source(monkeypatch):
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    monkeypatch.setattr(sys, "executable", "/usr/bin/python3")
    argv = autostart._launch_argv()
    assert argv == ["/usr/bin/python3", "-m", "kiro_gateway_tray"]


def test_launch_argv_frozen_linux_prefers_appimage(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(sys, "executable", "/tmp/.mount_abc/run")
    monkeypatch.setenv("APPIMAGE", "/home/u/Apps/kiro.AppImage")
    assert autostart._launch_argv() == ["/home/u/Apps/kiro.AppImage"]


def test_launch_argv_frozen_macos_uses_executable(monkeypatch):
    # macOS frozen: launch the bare executable directly so the login item shows
    # "KiroGatewayTray" (from ProgramArguments[0]) rather than "open".
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(sys, "executable", "/Applications/KiroGatewayTray.app/Contents/MacOS/KiroGatewayTray")
    assert autostart._launch_argv() == ["/Applications/KiroGatewayTray.app/Contents/MacOS/KiroGatewayTray"]


def test_quote_handles_spaces():
    assert autostart._quote("/no/space") == "/no/space"
    assert autostart._quote("/has space/x") == '"/has space/x"'


def test_macos_roundtrip(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(autostart.Path, "home", lambda: tmp_path)
    calls = []
    monkeypatch.setattr(autostart, "_launchctl", lambda *a: calls.append(a))
    monkeypatch.setattr(autostart.os, "getuid", lambda: 501, raising=False)
    assert autostart.is_enabled() is False
    autostart.set_enabled(True)
    plist = tmp_path / "Library" / "LaunchAgents" / f"{autostart.BUNDLE_ID}.plist"
    assert plist.exists()
    body = plist.read_text()
    assert autostart.BUNDLE_ID in body
    assert "<key>RunAtLoad</key>" in body
    assert "<key>AssociatedBundleIdentifiers</key>" not in body
    assert autostart.is_enabled() is True
    assert calls == [("bootstrap", "gui/501", str(plist))]
    calls.clear()
    autostart.set_enabled(False)
    assert plist.exists() is False
    assert autostart.is_enabled() is False
    # bootout must run before the file is removed so launchd/登录项 drop the job.
    assert calls == [("bootout", f"gui/501/{autostart.BUNDLE_ID}")]


def test_macos_set_enabled_false_is_idempotent(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(autostart.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(autostart, "_launchctl", lambda *a: None)
    monkeypatch.setattr(autostart.os, "getuid", lambda: 501, raising=False)
    autostart.set_enabled(False)  # nothing there yet
    assert autostart.is_enabled() is False


def test_linux_roundtrip(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert autostart.is_enabled() is False
    autostart.set_enabled(True)
    desktop = tmp_path / "autostart" / "kiro-gateway-tray.desktop"
    assert desktop.exists()
    body = desktop.read_text()
    assert "X-GNOME-Autostart-enabled=true" in body
    assert "Exec=" in body
    assert autostart.is_enabled() is True
    autostart.set_enabled(False)
    assert autostart.is_enabled() is False


def test_xml_escape():
    assert autostart._xml_escape("a&b<c>") == "a&amp;b&lt;c&gt;"
