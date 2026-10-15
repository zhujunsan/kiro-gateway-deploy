"""Tray-side audit pipeline: dedup, sealing, spool cap, identity."""
from __future__ import annotations

import calendar
import json
import os
import stat
from pathlib import Path

import pytest

from kiro_gateway_tray.appconfig import AppCfg
from kiro_gateway_tray.audit_payload import canonical_hash
from kiro_gateway_tray.audit_pipeline import AuditPipeline, parse_user_info, read_zst_lines


def _ts(*parts: int) -> float:
    return float(calendar.timegm(parts))


def _line(body: str, rid: str) -> str:
    record = {
        "v": 1,
        "id": rid,
        "ts_start": 1.0,
        "ts_end": 2.0,
        "username": "alice",
        "app_version": "0.4.49",
        "upstream_sha": "abc",
        "os": "darwin",
        "via": "local",
        "remote_ip": None,
        "method": "POST",
        "path": "/v1/messages",
        "req_headers": {},
        "req_bytes": len(body.encode()),
        "req_body": body,
        "status": 200,
        "resp_headers": {},
        "resp_bytes": 2,
        "resp": {"raw": "ok", "truncated": False},
        "completed": True,
        "error": None,
    }
    return json.dumps(record)


def _pipeline(tmp_path: Path, box: list[float], **kwargs) -> AuditPipeline:
    cfg = kwargs.pop("cfg", AppCfg())
    calls = kwargs.pop("calls", None)
    identity = kwargs.pop("identity", ("user-1", None))

    def fetch():
        if calls is not None:
            calls.append(1)
        found = identity
        if isinstance(found, BaseException):
            raise found
        return found

    return AuditPipeline(
        tmp_path,
        clock=lambda: box[0],
        username_provider=lambda: "alice",
        config_provider=lambda: cfg,
        identity_fetcher=fetch,
    )


class Boom(RuntimeError):
    pass


def test_rotate_seals_at_600s_and_names_the_utc_day(tmp_path):
    box = [_ts(2026, 9, 30, 12, 0, 0)]
    cfg = AppCfg()
    pipe = _pipeline(tmp_path, box, cfg=cfg)
    ready = tmp_path / "spool" / "a.ready"
    ready.parent.mkdir(parents=True)
    ready.write_text(_line('{"model":"m","messages":[{"role":"user","content":"hi"}]}', "r1") + "\n", encoding="utf-8")
    pipe.tick()
    assert list((tmp_path / "current").glob("*.req.jsonl"))
    assert list((tmp_path / "outbox").rglob("*.zst")) == []

    box[0] += 599
    pipe.tick()
    assert list((tmp_path / "outbox").rglob("*.zst")) == []

    box[0] += 1
    pipe.tick()
    sealed = list((tmp_path / "outbox").rglob("*.jsonl.zst"))
    assert len(sealed) == 2
    assert any(p.parent.name == "alice" and p.parent.parent.name == "26-09-30" for p in sealed)
    req = next(p for p in sealed if p.name.endswith(".req.jsonl.zst"))
    stored = json.loads(read_zst_lines(req)[0])
    assert stored["id"] == "r1"
    assert stored["kiro_user_id"] == "user-1"
    assert stored["req"]["raw"] is None
    assert "req_body" not in stored


def test_sealed_directory_is_the_idc_user_id(tmp_path):
    box = [_ts(2026, 9, 30, 12, 0, 0)]
    user_id = "d-9567aedac7.a7141a78-2011-70e3-c737-1500af9bdb40"
    pipe = _pipeline(tmp_path, box, identity=(user_id, None))
    pipe.tick()
    pipe.open_batch()
    pipe.seal()
    sealed = list((tmp_path / "outbox").rglob("*.jsonl.zst"))
    assert sealed
    assert {p.parent.name for p in sealed} == {user_id}
    assert sealed[0].parent.parent.name == "26-09-30"


def test_utc_midnight_seals_and_resets_dedup(tmp_path):
    box = [_ts(2026, 9, 30, 23, 50, 0)]
    pipe = _pipeline(tmp_path, box)
    body = json.dumps({"messages": [{"role": "user", "content": "same"}]})
    spool = tmp_path / "spool"
    spool.mkdir(parents=True)
    (spool / "1.ready").write_text(_line(body, "a") + "\n", encoding="utf-8")
    pipe.tick()
    box[0] = _ts(2026, 10, 1, 0, 0, 30)
    pipe.tick()
    (spool / "2.ready").write_text(_line(body, "b") + "\n", encoding="utf-8")
    pipe.tick()

    sep = list((tmp_path / "outbox" / "26-09-30").rglob("*.blob.jsonl.zst"))
    assert sep
    assert read_zst_lines(sep[0])
    current_blobs = list((tmp_path / "current").glob("*.blob.jsonl"))
    assert current_blobs
    assert canonical_hash({"role": "user", "content": "same"}) in current_blobs[0].read_text(encoding="utf-8")


def test_restart_does_not_rewrite_known_blobs_and_ignores_corrupt_files(tmp_path):
    box = [_ts(2026, 9, 30, 12, 0, 0)]
    body = json.dumps({"messages": [{"role": "user", "content": "keep"}]})
    spool = tmp_path / "spool"
    spool.mkdir(parents=True)
    (spool / "1.ready").write_text(_line(body, "a") + "\n", encoding="utf-8")
    pipe = _pipeline(tmp_path, box)
    pipe.tick()
    box[0] += 600
    pipe.tick()

    day = tmp_path / "outbox" / "26-09-30" / "alice"
    (day / "20260930T120000Z-deadbeef.blob.jsonl.zst").write_bytes(b"this is not zstd")

    again = _pipeline(tmp_path, box)
    again.start(background=False)  # must not raise on the corrupt batch
    (spool / "2.ready").write_text(_line(body, "b") + "\n", encoding="utf-8")
    again.tick()
    # The message was already sealed today, so the new batch stores no blob row.
    for path in (tmp_path / "current").glob("*.blob.jsonl"):
        assert path.read_text(encoding="utf-8") == ""


def test_truncated_tail_is_dropped_and_reprocess_keeps_ids(tmp_path):
    box = [_ts(2026, 9, 30, 12, 0, 0)]
    pipe = _pipeline(tmp_path, box)
    spool = tmp_path / "spool"
    spool.mkdir(parents=True)
    complete = _line("{}", "keep")
    partial = _line("{}", "chopped")[:12]
    text = complete + "\n" + partial
    (spool / "1.ready").write_text(text, encoding="utf-8")
    pipe.tick()
    req = next((tmp_path / "current").glob("*.req.jsonl"))
    first = json.loads(req.read_text(encoding="utf-8"))
    assert first["id"] == "keep"

    (spool / "2.ready").write_text(complete + "\n", encoding="utf-8")
    pipe.tick()
    ids = [json.loads(line)["id"] for line in req.read_text(encoding="utf-8").splitlines()]
    assert ids == ["keep", "keep"]


def test_empty_batch_seals_to_two_files(tmp_path):
    box = [_ts(2026, 9, 30, 12, 0, 0)]
    pipe = _pipeline(tmp_path, box)
    pipe.open_batch()
    pipe.seal()
    files = list((tmp_path / "outbox").rglob("*.jsonl.zst"))
    assert sorted(p.name.split(".")[1] for p in files) == ["blob", "req"]
    for path in files:
        assert read_zst_lines(path) == []


def test_spool_cap_drops_oldest_batch_only_when_over(tmp_path):
    box = [_ts(2026, 9, 30, 12, 0, 0)]
    cfg = AppCfg()
    cfg.audit.max_spool_mb = 1
    pipe = _pipeline(tmp_path, box, cfg=cfg)
    out = tmp_path / "outbox" / "26-09-30" / "alice"
    out.mkdir(parents=True)
    half = 256 * 1024
    for name, payload in (("20260930T100000Z-aaaaaaa1", b"a"), ("20260930T110000Z-aaaaaaa2", b"b")):
        for kind in ("req", "blob"):
            (out / f"{name}.{kind}.jsonl.zst").write_bytes(payload * half)
    # 4 * 256 KiB = 1 MiB exactly. Nothing is dropped.
    pipe.tick()
    assert pipe.dropped_batches == 0
    assert len(list(out.glob("*.zst"))) == 4

    (tmp_path / "spool").mkdir(exist_ok=True)
    # An open segment (not yet .ready) still counts toward the cap.
    (tmp_path / "spool" / "open.jsonl").write_bytes(b"x")
    pipe.tick()
    assert pipe.dropped_batches == 1
    assert not (out / "20260930T100000Z-aaaaaaa1.req.jsonl.zst").exists()
    assert (out / "20260930T110000Z-aaaaaaa2.req.jsonl.zst").exists()
    state = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert state["dropped_batches"] == 1


def test_mode_override_is_published_to_control(tmp_path):
    box = [_ts(2026, 9, 30, 12, 0, 0)]
    cfg = AppCfg()
    pipe = _pipeline(tmp_path, box, cfg=cfg)
    pipe.tick()
    control = json.loads((tmp_path / "control.json").read_text(encoding="utf-8"))
    assert control["mode"] == "off"
    cfg.audit.mode_override = "best_effort"
    pipe.tick()
    control = json.loads((tmp_path / "control.json").read_text(encoding="utf-8"))
    assert control["mode"] == "best_effort"
    assert control["max_spool_bytes"] == 1024 * 1024 * 1024


def test_identity_failure_does_not_retry_until_the_interval(tmp_path):
    box = [_ts(2026, 9, 30, 12, 0, 0)]
    calls: list[int] = []

    def fetch():
        calls.append(1)
        raise Boom("down")

    pipe = AuditPipeline(
        tmp_path,
        clock=lambda: box[0],
        username_provider=lambda: "alice",
        config_provider=AppCfg,
        identity_fetcher=fetch,
    )
    pipe.tick()
    pipe.tick()
    assert calls == [1]
    box[0] += 3600
    pipe.tick()
    assert len(calls) == 2


def test_missing_user_info_is_stored_as_null(tmp_path):
    box = [_ts(2026, 9, 30, 12, 0, 0)]
    pipe = _pipeline(tmp_path, box, identity=(None, None))
    spool = tmp_path / "spool"
    spool.mkdir(parents=True)
    (spool / "1.ready").write_text(_line("{}", "r") + "\n", encoding="utf-8")
    pipe.tick()
    req = next((tmp_path / "current").glob("*.req.jsonl"))
    stored = json.loads(req.read_text(encoding="utf-8"))
    assert stored["kiro_user_id"] is None
    assert stored["email"] is None


def test_parse_user_info_shapes():
    assert parse_user_info({}) == (None, None)
    assert parse_user_info({"userInfo": {}}) == (None, None)
    assert parse_user_info({"userInfo": {"userId": "d-1.abc", "email": None}}) == ("d-1.abc", None)
    assert parse_user_info({"userInfo": {"userId": "", "email": "a@b.c"}}) == (None, "a@b.c")


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits")
def test_batch_files_are_private(tmp_path):
    box = [_ts(2026, 9, 30, 12, 0, 0)]
    pipe = _pipeline(tmp_path, box)
    pipe.open_batch()
    pipe.seal()
    assert stat.S_IMODE(tmp_path.stat().st_mode) == 0o700
    for path in (tmp_path / "outbox").rglob("*"):
        if path.is_dir():
            assert stat.S_IMODE(path.stat().st_mode) == 0o700
        else:
            assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_supervisor_stop_flushes_ready_files(monkeypatch, tmp_path):
    from kiro_gateway_tray import supervisor

    flushed: list[str] = []
    monkeypatch.setattr(supervisor.audit_runtime, "flush", lambda: flushed.append("flush"))
    monkeypatch.setenv("KIRO_GATEWAY_TRAY_HOME", str(tmp_path))

    class _Fake:
        def start(self, cfg): pass
        def stop(self): pass
        def is_alive(self): return False

    sup = supervisor.Supervisor(gateway=_Fake(), tunnel=_Fake())
    sup.stop()
    assert flushed == ["flush"]


def test_appconfig_audit_defaults_roundtrip(monkeypatch, tmp_path):
    from kiro_gateway_tray import appconfig

    monkeypatch.setenv("KIRO_GATEWAY_TRAY_HOME", str(tmp_path))
    cfg = appconfig.load()
    assert cfg.audit.mode_override == ""
    assert cfg.audit.max_spool_mb == 1024
    assert cfg.audit.rotate_seconds == 600
    cfg.audit.mode_override = "best_effort"
    appconfig.save(cfg)
    again = appconfig.load()
    assert again.audit.mode_override == "best_effort"
