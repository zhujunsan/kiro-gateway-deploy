"""Rebuild stored audit batches back into request bodies."""
from __future__ import annotations

import calendar
import json
from pathlib import Path

from kiro_gateway_tray.appconfig import AppCfg
from kiro_gateway_tray.audit_payload import canonical_json, strip_media
from kiro_gateway_tray.audit_pipeline import AuditPipeline
from scripts.audit_rebuild import select
from scripts.audit_stats import summarize


def _ts(*parts: int) -> float:
    return float(calendar.timegm(parts))


def _ready(spool: Path, name: str, body: dict, rid: str) -> None:
    spool.mkdir(parents=True, exist_ok=True)
    text = json.dumps(body)
    record = {
        "v": 1, "id": rid, "ts_start": 1_700_000_000.0, "ts_end": 1_700_000_001.0,
        "username": "alice", "app_version": "0", "upstream_sha": "abc", "os": "darwin",
        "via": "local", "remote_ip": None, "method": "POST", "path": "/v1/messages",
        "req_headers": {}, "req_bytes": len(text.encode()), "req_body": text,
        "status": 200, "resp_headers": {}, "resp_bytes": 2,
        "resp": {"raw": "ok", "truncated": False}, "completed": True, "error": None,
    }
    (spool / name).write_text(json.dumps(record) + "\n", encoding="utf-8")


def test_rebuild_joins_blobs_across_batches_and_reports_gaps(tmp_path):
    box = [_ts(2026, 9, 30, 12, 0, 0)]
    cfg = AppCfg()
    pipe = AuditPipeline(
        tmp_path,
        clock=lambda: box[0],
        username_provider=lambda: "alice",
        config_provider=lambda: cfg,
        identity_fetcher=lambda: ("d-1.abc", None),
    )
    message = {"role": "user", "content": "same question"}
    body = {"model": "m", "messages": [message]}
    _ready(tmp_path / "spool", "a.ready", body, "a")
    pipe.tick()
    box[0] += 600
    pipe.tick()  # seals batch one, which holds the blob
    _ready(tmp_path / "spool", "b.ready", body, "b")
    pipe.tick()  # same message: hash already known, no second blob write
    box[0] += 600
    pipe.tick()

    records, missing = select(tmp_path / "outbox", kiro_user_id="d-1.abc")
    assert missing == []
    assert len(records) == 2
    stripped, _drops = strip_media(body)
    for record in records:
        assert canonical_json(record["body"]) == canonical_json(stripped)

    other, _ = select(tmp_path / "outbox", user="bob")
    assert other == []
    early, _ = select(tmp_path / "outbox", end=1.0)
    assert early == []

    blobs = list((tmp_path / "outbox").rglob("*.blob.jsonl.zst"))
    assert blobs
    for blob in blobs:
        blob.unlink()
    _records, missing = select(tmp_path / "outbox")
    assert missing
    assert all(isinstance(item, str) and len(item) == 64 for item in missing)

    stats = summarize(tmp_path)
    assert stats["requests"] == 2
    assert stats["raw_request_bytes"] > 0
    assert stats["compressed_bytes"] > 0
