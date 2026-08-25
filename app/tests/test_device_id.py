"""Unit tests for OS-install device fingerprinting."""
from __future__ import annotations

import hashlib
from types import SimpleNamespace

from kiro_gateway_tray import device_id


def test_fingerprint_hashes_normalized_raw_id(monkeypatch):
    monkeypatch.setattr(device_id, "read_raw_id", lambda: " ABC-DEF ")
    assert device_id.fingerprint() == hashlib.sha1(b"abc-def").hexdigest()[:12]


def test_fingerprint_empty_when_raw_id_missing(monkeypatch):
    monkeypatch.setattr(device_id, "read_raw_id", lambda: "  ")
    assert device_id.fingerprint() == ""


def test_darwin_parses_ioreg_uuid(monkeypatch):
    result = SimpleNamespace(
        returncode=0,
        stdout='    "IOPlatformUUID" = "AAAA-BBBB-CCCC"\n',
    )
    monkeypatch.setattr(device_id.subprocess, "run", lambda *a, **k: result)
    assert device_id._read_darwin() == "AAAA-BBBB-CCCC"


def test_darwin_empty_on_ioreg_failure(monkeypatch):
    result = SimpleNamespace(returncode=1, stdout="")
    monkeypatch.setattr(device_id.subprocess, "run", lambda *a, **k: result)
    assert device_id._read_darwin() == ""


def test_linux_reads_machine_id(tmp_path, monkeypatch):
    path = tmp_path / "machine-id"
    path.write_text("deadbeefcafebabe\n", encoding="utf-8")
    monkeypatch.setattr(device_id, "_LINUX_MACHINE_ID_PATHS", (path,))
    assert device_id._read_linux() == "deadbeefcafebabe"


def test_linux_skips_unreadable_then_reads_next(tmp_path, monkeypatch):
    missing = tmp_path / "missing"
    present = tmp_path / "machine-id"
    present.write_text("abc123\n", encoding="utf-8")
    monkeypatch.setattr(device_id, "_LINUX_MACHINE_ID_PATHS", (missing, present))
    assert device_id._read_linux() == "abc123"


def test_read_raw_id_swallows_platform_errors(monkeypatch):
    monkeypatch.setattr(device_id.sys, "platform", "darwin")
    monkeypatch.setattr(device_id, "_read_darwin", lambda: (_ for _ in ()).throw(OSError("boom")))
    assert device_id.read_raw_id() == ""
