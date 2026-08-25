"""Stable per-machine identity for tunnel naming.

The slug is ``sha1(raw-id)[:12]``. Raw IDs are OS install identifiers, not
disk serials: they stay put across Kiro re-login and data-disk swaps, and
change on OS reinstall (accepted).
"""
from __future__ import annotations

import hashlib
import re
import subprocess
import sys
from pathlib import Path

_FINGERPRINT_LEN = 12
_DARWIN_UUID_RE = re.compile(r'"IOPlatformUUID"\s*=\s*"([^"]+)"')
_LINUX_MACHINE_ID_PATHS = (
    Path("/etc/machine-id"),
    Path("/var/lib/dbus/machine-id"),
)


def fingerprint() -> str:
    """Return a 12-char hex slug for this OS install, or ``""`` if unavailable."""
    raw = read_raw_id().strip().lower()
    if not raw:
        return ""
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:_FINGERPRINT_LEN]


def read_raw_id() -> str:
    """Best-effort platform machine id. Never raises."""
    try:
        if sys.platform == "darwin":
            return _read_darwin()
        if sys.platform == "win32":
            return _read_win32()
        return _read_linux()
    except Exception:
        return ""


def _read_darwin() -> str:
    result = subprocess.run(
        ["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    if result.returncode != 0:
        return ""
    match = _DARWIN_UUID_RE.search(result.stdout or "")
    return match.group(1).strip() if match else ""


def _read_win32() -> str:
    import winreg

    access = winreg.KEY_READ
    if hasattr(winreg, "KEY_WOW64_64KEY"):
        access |= winreg.KEY_WOW64_64KEY
    key = winreg.OpenKey(
        winreg.HKEY_LOCAL_MACHINE,
        r"SOFTWARE\Microsoft\Cryptography",
        0,
        access,
    )
    try:
        value, _typ = winreg.QueryValueEx(key, "MachineGuid")
    finally:
        winreg.CloseKey(key)
    return str(value or "").strip()


def _read_linux() -> str:
    for path in _LINUX_MACHINE_ID_PATHS:
        try:
            text = path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if text:
            return text
    return ""
