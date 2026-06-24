# app/kiro_gateway_tray/autostart.py
"""Cross-platform "launch at login / boot" integration.

Each OS has its own per-user mechanism; we keep all the branching here so the
tray UI only sees ``is_supported`` / ``is_enabled`` / ``set_enabled``.

  - macOS:   a LaunchAgent plist in ``~/Library/LaunchAgents``. This works for
             unsigned apps. macOS 13+ shows its own "added a login item" system
             notification and lists us under 系统设置 → 通用 → 登录项, so we do
             NOT pop a custom dialog — the OS already tells the user.
  - Windows: an ``HKCU\\...\\CurrentVersion\\Run`` registry value (per-user, no
             admin rights needed).
  - Linux:   an XDG autostart ``.desktop`` file in ``~/.config/autostart``.

The launch target is derived from how we're running:
  - frozen Linux AppImage: the ``$APPIMAGE`` path (``sys.executable`` points at
    a per-run ``/tmp/.mount_*`` path that does not survive a reboot);
  - frozen macOS/Windows: ``sys.executable`` (the bundled binary);
  - from source: re-exec the interpreter with ``-m kiro_gateway_tray``.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from .log import logger

BUNDLE_ID = "top.botsonny.kiro-gateway-tray"
_DISPLAY_NAME = "Kiro Gateway Tray"
_WIN_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_WIN_VALUE_NAME = "KiroGatewayTray"


def _launch_argv() -> list[str]:
    """Argv that should be run at login to start this app."""
    if not getattr(sys, "frozen", False):
        return [sys.executable, "-m", "kiro_gateway_tray"]
    if sys.platform.startswith("linux"):
        # AppImage mount path changes every run; $APPIMAGE is the stable file.
        return [os.environ.get("APPIMAGE") or sys.executable]
    return [sys.executable]


def _quote(arg: str) -> str:
    """Minimal shell-style quoting for a single argv element."""
    if arg and not any(c in arg for c in ' \t"\\'):
        return arg
    return '"' + arg.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _launch_command() -> str:
    return " ".join(_quote(a) for a in _launch_argv())


# --- macOS: LaunchAgent plist -------------------------------------------------

def _macos_plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{BUNDLE_ID}.plist"


def _macos_plist_contents() -> str:
    args_xml = "\n".join(
        f"        <string>{_xml_escape(a)}</string>" for a in _launch_argv()
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0">\n'
        "<dict>\n"
        "    <key>Label</key>\n"
        f"    <string>{BUNDLE_ID}</string>\n"
        "    <key>ProgramArguments</key>\n"
        "    <array>\n"
        f"{args_xml}\n"
        "    </array>\n"
        "    <key>RunAtLoad</key>\n"
        "    <true/>\n"
        "    <key>ProcessType</key>\n"
        "    <string>Interactive</string>\n"
        "</dict>\n"
        "</plist>\n"
    )


def _macos_is_enabled() -> bool:
    return _macos_plist_path().exists()


def _macos_set_enabled(enabled: bool) -> None:
    p = _macos_plist_path()
    if enabled:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(_macos_plist_contents(), encoding="utf-8")
    elif p.exists():
        p.unlink()


# --- Windows: HKCU Run registry value ----------------------------------------

def _win_open_run_key(write: bool):
    import winreg

    access = winreg.KEY_WRITE if write else winreg.KEY_READ
    return winreg.OpenKey(winreg.HKEY_CURRENT_USER, _WIN_RUN_KEY, 0, access)


def _win_is_enabled() -> bool:
    import winreg

    try:
        with _win_open_run_key(write=False) as key:
            winreg.QueryValueEx(key, _WIN_VALUE_NAME)
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return False


def _win_set_enabled(enabled: bool) -> None:
    import winreg

    if enabled:
        with _win_open_run_key(write=True) as key:
            winreg.SetValueEx(
                key, _WIN_VALUE_NAME, 0, winreg.REG_SZ, _launch_command()
            )
    else:
        try:
            with _win_open_run_key(write=True) as key:
                winreg.DeleteValue(key, _WIN_VALUE_NAME)
        except FileNotFoundError:
            pass


# --- Linux: XDG autostart .desktop -------------------------------------------

def _linux_desktop_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "autostart" / "kiro-gateway-tray.desktop"


def _linux_desktop_contents() -> str:
    return (
        "[Desktop Entry]\n"
        "Type=Application\n"
        f"Name={_DISPLAY_NAME}\n"
        f"Exec={_launch_command()}\n"
        "Terminal=false\n"
        "X-GNOME-Autostart-enabled=true\n"
    )


def _linux_is_enabled() -> bool:
    return _linux_desktop_path().exists()


def _linux_set_enabled(enabled: bool) -> None:
    p = _linux_desktop_path()
    if enabled:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(_linux_desktop_contents(), encoding="utf-8")
    elif p.exists():
        p.unlink()


# --- small helpers ------------------------------------------------------------

def _xml_escape(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


# --- public API ---------------------------------------------------------------

def is_supported() -> bool:
    """Whether we can manage a login item on this platform."""
    return sys.platform in ("darwin", "win32") or sys.platform.startswith("linux")


def is_enabled() -> bool:
    """Whether launch-at-login is currently configured. Never raises."""
    try:
        if sys.platform == "darwin":
            return _macos_is_enabled()
        if sys.platform == "win32":
            return _win_is_enabled()
        if sys.platform.startswith("linux"):
            return _linux_is_enabled()
    except Exception:
        logger.debug("autostart.is_enabled failed", exc_info=True)
    return False


def set_enabled(enabled: bool) -> None:
    """Enable or disable launch-at-login. Raises on failure so callers can
    surface the error to the user."""
    if sys.platform == "darwin":
        _macos_set_enabled(enabled)
    elif sys.platform == "win32":
        _win_set_enabled(enabled)
    elif sys.platform.startswith("linux"):
        _linux_set_enabled(enabled)
    else:
        raise RuntimeError("当前平台不支持开机自启。")


__all__ = ["is_supported", "is_enabled", "set_enabled", "BUNDLE_ID"]
