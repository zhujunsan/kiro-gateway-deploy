# -*- coding: utf-8 -*-
"""Tests for incident_report: chunking, spool, upload, and enqueue safety."""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from kiro_gateway_tray import incident_report as ir


def test_encode_decode_utf8_and_binary():
    enc, payload = ir.encode_artifact("你好".encode("utf-8"))
    assert enc == "utf-8"
    assert ir.decode_artifact(enc, payload) == "你好".encode("utf-8")

    blob = bytes(range(256))
    enc, payload = ir.encode_artifact(blob)
    assert enc == "base64"
    assert ir.decode_artifact(enc, payload) == blob


def test_split_artifact_respects_budget_and_roundtrips():
    data = (b"abcdefghij" * 20000)  # 200 KB
    parts = ir.split_artifact_parts("inc-1", "request_body.json", data, budget=8 * 1024)
    assert len(parts) >= 2
    for p in parts:
        assert p["part_total"] == len(parts)
        assert p["sha256"] == ir._sha256_hex(data)
        body = {"schema_version": 1, "record": p}
        assert len(json.dumps(body).encode("utf-8")) <= ir.CLIENT_RECORD_MAX_BYTES

    rebuilt = b"".join(ir.decode_artifact(p["encoding"], p["data"]) for p in parts)
    assert rebuilt == data


def test_build_records_manifest_first():
    snap = {
        "incident_id": "abc",
        "ts": 100,
        "duration_ms": 12,
        "path": "/v1/chat/completions",
        "model": "m",
        "stream": False,
        "status_code": 400,
        "gateway_status": 400,
        "upstream_status": 400,
        "source": "kiro_upstream",
        "code": "INVALID_MODEL_ID",
        "phase": "response_parse",
        "client_disconnected": False,
        "error_message": "bad",
        "artifacts": {
            "request_body.json": b'{"model":"m"}',
            "app_logs.txt": b"line\n",
        },
    }
    records = ir.build_records(snap, username="userhash12ab", app_version="0.3.12",
                               upstream_sha="7f25d0f")
    assert records[0]["record_type"] == "manifest"
    assert records[0]["total_parts"] == len(records) - 1
    assert set(records[0]["artifact_names"]) == {"request_body.json", "app_logs.txt"}
    assert all(r["incident_id"] == "abc" for r in records)
    assert all("part_id" in r for r in records)


def test_pending_store_dedup_age_and_size(tmp_path):
    store = ir.PendingErrorStore(tmp_path / "pending-errors.jsonl",
                                 max_bytes=500, max_days=7)
    store.append([{"part_id": "a", "x": 1}])
    store.append([{"part_id": "b", "x": 2}])
    rows = store.load_all()
    assert len(rows) == 2
    assert rows[0]["part_id"] == "a"
    store.clear()
    assert store.load_all() == []


def test_uploader_ok_and_too_large(monkeypatch):
    calls = []

    class FakeResp:
        def __init__(self, status_code):
            self.status_code = status_code

    def fake_post(url, content=None, headers=None, timeout=None, proxy=None):
        calls.append(json.loads(content))
        return FakeResp(200)

    monkeypatch.setattr(ir.httpx, "post", fake_post)
    up = ir.IncidentUploader("https://example.test/telemetry/errors", "sec")
    rec = {"kind": ir.KIND, "record_type": "manifest", "part_id": "p",
           "incident_id": "i", "username": "u", "error_message": "e"}
    assert up.upload_record(rec) == ir.UPLOAD_OK
    assert calls[0]["schema_version"] == 1
    assert calls[0]["record"]["incident_id"] == "i"

    # Force too-large client-side
    huge = {"kind": ir.KIND, "record_type": "artifact_chunk", "part_id": "p",
            "incident_id": "i", "artifact": "x", "part_index": 0, "part_total": 1,
            "sha256": "0" * 64, "artifact_bytes": 1, "encoding": "utf-8",
            "data": "x" * (ir.CLIENT_RECORD_MAX_BYTES)}
    assert up.upload_record(huge) == ir.UPLOAD_TOO_LARGE


def test_uploader_401(monkeypatch):
    class FakeResp:
        status_code = 401

    monkeypatch.setattr(ir.httpx, "post", lambda *a, **k: FakeResp())
    up = ir.IncidentUploader("https://example.test/telemetry/errors", "sec")
    assert up.upload_record({"kind": ir.KIND, "record_type": "manifest",
                             "part_id": "p", "incident_id": "i"}) == ir.UPLOAD_UNAUTHORIZED


def test_reporter_enqueue_and_tick_uploads(tmp_path, monkeypatch):
    uploaded = []

    class FakeUploader:
        secret = "s"

        def upload_record(self, record):
            uploaded.append(record)
            return ir.UPLOAD_OK

    cfg = ir.IncidentConfig(
        endpoint_url="https://example.test/telemetry/errors",
        secret="s",
        username="userhash12ab",
        app_version="0.3.12",
        upstream_sha="7f25d0f",
    )
    pending = ir.PendingErrorStore(tmp_path / "pending-errors.jsonl")
    rep = ir.IncidentReporter(cfg, FakeUploader(), pending)
    # Avoid sleeping in rate limiter during tests
    monkeypatch.setattr(ir.time, "sleep", lambda *_: None)

    snap = {
        "incident_id": "inc-1",
        "ts": int(time.time()),
        "duration_ms": 5,
        "path": "/v1/messages",
        "model": "m",
        "stream": True,
        "status_code": 500,
        "gateway_status": 500,
        "upstream_status": None,
        "source": "gateway",
        "code": "streaming_error",
        "phase": "streaming",
        "client_disconnected": False,
        "error_message": "boom",
        "artifacts": {"app_logs.txt": b"err\n"},
    }
    rep.enqueue_snapshot(snap)
    rep.tick(now=time.time())
    assert uploaded
    assert uploaded[0]["record_type"] == "manifest"
    assert uploaded[0]["incident_id"] == "inc-1"
    assert any(r.get("record_type") == "artifact_chunk" for r in uploaded)


def test_reporter_spools_on_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(ir.time, "sleep", lambda *_: None)

    class FailUploader:
        secret = "s"

        def upload_record(self, record):
            return ir.UPLOAD_ERROR

    cfg = ir.IncidentConfig(endpoint_url="https://x/telemetry/errors", secret="s",
                            username="userhash12ab")
    pending = ir.PendingErrorStore(tmp_path / "pending-errors.jsonl")
    rep = ir.IncidentReporter(cfg, FailUploader(), pending)
    rep.enqueue_snapshot({
        "incident_id": "inc-2",
        "ts": 1,
        "duration_ms": 1,
        "path": "/v1/chat/completions",
        "model": "m",
        "stream": False,
        "status_code": 400,
        "gateway_status": 400,
        "upstream_status": 400,
        "source": "kiro_upstream",
        "code": "x",
        "phase": "response_parse",
        "client_disconnected": False,
        "error_message": "e",
        "artifacts": {"request_body.json": b"{}"},
    })
    rep.tick(now=time.time())
    assert pending.load_all()


def test_reporter_stop_flushes_queue_to_spool(tmp_path):
    """Unsent in-memory records must land in the spool on stop (no silent drop)."""
    class NeverUploader:
        secret = "s"

        def upload_record(self, record):
            raise AssertionError("stop flush must not upload")

    cfg = ir.IncidentConfig(endpoint_url="https://x/telemetry/errors", secret="s",
                            username="userhash12ab")
    pending = ir.PendingErrorStore(tmp_path / "pending-errors.jsonl")
    rep = ir.IncidentReporter(cfg, NeverUploader(), pending)
    # Do not start the background thread; enqueue then stop → flush only.
    rep.enqueue_snapshot({
        "incident_id": "inc-stop",
        "ts": 1,
        "duration_ms": 1,
        "path": "/v1/chat/completions",
        "model": "m",
        "stream": False,
        "status_code": 400,
        "gateway_status": 400,
        "upstream_status": 400,
        "source": "kiro_upstream",
        "code": "x",
        "phase": "response_parse",
        "client_disconnected": False,
        "error_message": "e",
        "artifacts": {"request_body.json": b"{}", "app_logs.txt": b"log\n"},
    })
    with rep._lock:
        queued = len(rep._queue)
    assert queued >= 2  # manifest + chunks
    rep.stop(timeout=0.1)
    with rep._lock:
        assert rep._queue == []
    rows = pending.load_all()
    assert len(rows) == queued
    assert rows[0]["record_type"] == "manifest"
    assert rows[0]["incident_id"] == "inc-stop"
    assert any(r.get("record_type") == "artifact_chunk" for r in rows)


def test_enqueue_never_raises(tmp_path):
    cfg = ir.IncidentConfig(endpoint_url="")  # disabled
    pending = ir.PendingErrorStore(tmp_path / "p.jsonl")
    rep = ir.IncidentReporter(cfg, ir.IncidentUploader("", ""), pending)
    rep.enqueue_snapshot(None)  # type: ignore[arg-type]


def test_config_from_env_derives_fields():
    cfg = ir.config_from_env({
        "INCIDENT_URL": "https://w.example/telemetry/errors",
        "TELEMETRY_SECRET": "sec",
        "TELEMETRY_USERNAME": "abc123def456",
        "APP_VERSION": "0.3.12",
        "GATEWAY_UPSTREAM_SHA": "7f25d0f",
    })
    assert cfg.enabled
    assert cfg.username == "abc123def456"
    assert cfg.upstream_sha == "7f25d0f"


def test_appconfig_injects_incident_url(monkeypatch, tmp_path):
    from kiro_gateway_tray import appconfig

    cfg = appconfig.AppCfg()
    cfg.cloudflare.provision_url = "https://prov.example"
    cfg.telemetry.secret = "s"
    monkeypatch.setattr(
        "kiro_gateway_tray.provision._get_username",
        lambda _cfg: "abc123def456",
    )
    env = {}
    appconfig._inject_telemetry_env(cfg, env)
    assert env["TELEMETRY_URL"] == "https://prov.example/telemetry"
    assert env["INCIDENT_URL"] == "https://prov.example/telemetry/errors"
    assert env["GATEWAY_UPSTREAM_SHA"]
    assert env["TELEMETRY_USERNAME"] == "abc123def456"


def test_incident_and_usage_reporters_share_child_runtime_secret(tmp_path, monkeypatch):
    """A refreshed usage secret is immediately used by incident uploads too."""
    from kiro_gateway_tray import telemetry

    monkeypatch.setattr(telemetry, "_RUNTIME_SECRET", None)
    usage = telemetry.build_reporter(
        telemetry.TelemetryConfig(endpoint_url="https://w/telemetry", secret="old"),
        tmp_path,
    )
    config = ir.IncidentConfig(endpoint_url="https://w/telemetry/errors", secret="old")
    incident = ir.build_reporter(config, tmp_path)
    usage.runtime_secret_store.set("rotated")

    assert incident.runtime_secret_store is usage.runtime_secret_store
    assert incident.runtime_secret_store.get() == "rotated"
