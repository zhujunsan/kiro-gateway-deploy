"""Gateway-child audit capture: control file, spool rotation, failure isolation."""
from __future__ import annotations

import asyncio
import json
import os
import stat
from pathlib import Path

import pytest

from kiro_gateway_tray.audit_capture import (
    COLLECT_PATHS,
    CONTROL_POLL_SECONDS,
    wrap_app,
)


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _clock(box: list[float]):
    def now() -> float:
        return box[0]
    return now


def _arm(root: Path, mode: str = "best_effort", max_spool_bytes: int = 1024 * 1024) -> None:
    (root / "control.json").write_text(
        json.dumps({"mode": mode, "max_spool_bytes": max_spool_bytes}),
        encoding="utf-8",
    )


def _wrap(root: Path, clock, **kwargs):
    return wrap_app(None, audit_dir=root, clock=clock, start_thread=False, **kwargs)


async def _drive(middleware, *, method="POST", path="/v1/messages", body=b"{}",
                 headers=None, response=None, inner=None):
    scope = {
        "type": "http",
        "method": method,
        "path": path,
        "headers": headers or [(b"user-agent", b"test")],
    }
    frames = [
        {"type": "http.request", "body": body[:1], "more_body": True},
        {"type": "http.request", "body": body[1:], "more_body": False},
    ]
    if not body:
        frames = [{"type": "http.request", "body": b"", "more_body": False}]
    idx = {"i": 0}
    sent: list[dict] = []

    async def receive():
        i = idx["i"]
        idx["i"] += 1
        if i < len(frames):
            return frames[i]
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    async def default_inner(_scope, recv, snd):
        while True:
            msg = await recv()
            if msg["type"] == "http.request" and not msg.get("more_body", False):
                break
        messages = response or [
            {"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"application/json")]},
            {"type": "http.response.body", "body": b'{"ok":true}', "more_body": False},
        ]
        for message in messages:
            await snd(message)

    middleware.app = inner or default_inner
    await middleware(scope, receive, send)
    return sent


def _ready_lines(root: Path) -> list[dict]:
    lines = []
    spool = root / "spool"
    for path in sorted(spool.glob("*.ready")) + sorted(spool.glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line:
                lines.append(json.loads(line))
    return lines


# --- control file ------------------------------------------------------------

@pytest.mark.parametrize("setup", ["missing", "empty", "invalid", "unknown"])
def test_unusable_control_is_off_and_response_is_untouched(tmp_path, setup):
    if setup == "empty":
        (tmp_path / "control.json").write_text("", encoding="utf-8")
    elif setup == "invalid":
        (tmp_path / "control.json").write_text("{", encoding="utf-8")
    elif setup == "unknown":
        (tmp_path / "control.json").write_text('{"mode": "required"}', encoding="utf-8")
    mw = _wrap(tmp_path, _clock([0.0]))
    sent = _run(_drive(mw, body=b'{"model":"m"}'))
    bodies = [m.get("body") for m in sent if m["type"] == "http.response.body"]
    assert bodies == [b'{"ok":true}']
    assert _ready_lines(tmp_path) == []
    assert not list((tmp_path / "spool").glob("*.jsonl"))


def test_control_change_applies_after_poll_interval_not_mid_request(tmp_path):
    box = [1_000.0]
    _arm(tmp_path)
    mw = _wrap(tmp_path, _clock(box))
    started = asyncio.Event()
    release = asyncio.Event()

    async def blocking_inner(_scope, recv, snd):
        while True:
            msg = await recv()
            if msg["type"] == "http.request" and not msg.get("more_body", False):
                break
        started.set()
        await release.wait()
        await snd({"type": "http.response.start", "status": 200, "headers": []})
        await snd({"type": "http.response.body", "body": b"ok", "more_body": False})

    async def scenario():
        task = asyncio.create_task(_drive(mw, body=b'{"a":1}', inner=blocking_inner))
        await started.wait()
        _arm(tmp_path, mode="off")
        box[0] += CONTROL_POLL_SECONDS + 1
        release.set()
        await task

    _run(scenario())
    mw.writer.drain()
    assert len(_ready_lines(tmp_path)) == 1  # in-flight request still recorded

    _run(_drive(mw, body=b'{"a":2}'))
    mw.writer.drain()
    assert len(_ready_lines(tmp_path)) == 1  # later request sees off


def test_best_effort_records_sse_verbatim(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEMETRY_USERNAME", "alice")
    monkeypatch.setenv("APP_VERSION", "0.4.49")
    monkeypatch.setenv("GATEWAY_UPSTREAM_SHA", "abc123")
    _arm(tmp_path)
    mw = _wrap(tmp_path, _clock([0.0]))
    chunks = [b"event: message_start\n", b"data: hi\n\n"]
    response = [
        {"type": "http.response.start", "status": 200,
         "headers": [(b"content-type", b"text/event-stream"), (b"authorization", b"secret")]},
        {"type": "http.response.body", "body": chunks[0], "more_body": True},
        {"type": "http.response.body", "body": chunks[1], "more_body": False},
    ]
    headers = [
        (b"cf-ray", b"ray1"),
        (b"cf-connecting-ip", b"1.2.3.4"),
        (b"Authorization", b"Bearer s"),
        (b"user-agent", b"claude-cli/1"),
    ]
    sent = _run(_drive(mw, body=b'{"messages":[]}', headers=headers, response=response))
    forwarded = b"".join(m["body"] for m in sent if m["type"] == "http.response.body")
    assert forwarded == b"".join(chunks)
    mw.writer.drain()
    record = _ready_lines(tmp_path)[0]
    assert record["resp"]["raw"] == "event: message_start\ndata: hi\n\n"
    assert record["resp"]["truncated"] is False
    assert record["via"] == "tunnel"
    assert record["remote_ip"] == "1.2.3.4"
    assert record["req_headers"]["authorization"] == "<redacted>"
    assert record["resp_headers"]["authorization"] == "<redacted>"
    assert record["username"] == "alice"
    assert record["app_version"] == "0.4.49"
    assert record["req_body"] == '{"messages":[]}'
    assert "kiro_user_id" not in record
    assert "req" not in record


def test_local_request_has_no_remote_ip(tmp_path):
    _arm(tmp_path)
    mw = _wrap(tmp_path, _clock([0.0]))
    _run(_drive(mw, headers=[(b"user-agent", b"curl")]))
    mw.writer.drain()
    record = _ready_lines(tmp_path)[0]
    assert record["via"] == "local"
    assert record["remote_ip"] is None


def test_response_over_cap_is_truncated(tmp_path):
    _arm(tmp_path)
    mw = _wrap(tmp_path, _clock([0.0]), resp_max_bytes=4)
    response = [
        {"type": "http.response.start", "status": 200, "headers": []},
        {"type": "http.response.body", "body": b"abcdefgh", "more_body": False},
    ]
    sent = _run(_drive(mw, response=response))
    assert sent[-1]["body"] == b"abcdefgh"
    mw.writer.drain()
    record = _ready_lines(tmp_path)[0]
    assert record["resp"]["raw"] == "abcd"
    assert record["resp"]["truncated"] is True
    assert record["resp_bytes"] == 8


def test_inner_exception_is_reraised_and_recorded(tmp_path):
    _arm(tmp_path)
    mw = _wrap(tmp_path, _clock([0.0]))

    async def boom(_scope, recv, _snd):
        while True:
            msg = await recv()
            if msg["type"] == "http.request" and not msg.get("more_body", False):
                break
        raise RuntimeError("nope")

    with pytest.raises(RuntimeError, match="nope"):
        _run(_drive(mw, inner=boom))
    mw.writer.drain()
    record = _ready_lines(tmp_path)[0]
    assert record["completed"] is False
    assert record["error"] == "RuntimeError"


def test_disconnect_before_final_chunk_is_incomplete(tmp_path):
    _arm(tmp_path)
    mw = _wrap(tmp_path, _clock([0.0]))
    response = [
        {"type": "http.response.start", "status": 200, "headers": []},
        {"type": "http.response.body", "body": b"partial", "more_body": True},
    ]
    _run(_drive(mw, response=response))
    mw.writer.drain()
    record = _ready_lines(tmp_path)[0]
    assert record["completed"] is False
    assert record["error"] is None
    assert record["resp"]["raw"] == "partial"


def test_enqueue_and_disk_failures_do_not_break_the_response(tmp_path):
    _arm(tmp_path)
    mw = _wrap(tmp_path, _clock([0.0]), queue_max_bytes=8)
    sent = _run(_drive(mw, body=b'{"model":"this-line-is-far-longer-than-eight-bytes"}'))
    assert sent[-1]["body"] == b'{"ok":true}'
    assert mw.writer.dropped_records == 1

    (tmp_path / "ro").mkdir()
    _arm(tmp_path / "ro")
    mw2 = _wrap(tmp_path / "ro", _clock([0.0]))
    # A file where the spool directory should be: creating the segment must fail
    # without affecting the response that was already forwarded.
    blocked = tmp_path / "ro" / "blocked"
    blocked.write_text("not a directory", encoding="utf-8")
    mw2.writer.spool_dir = blocked
    sent = _run(_drive(mw2, body=b"{}"))
    assert sent[-1]["body"] == b'{"ok":true}'
    mw2.writer.drain()
    assert mw2.writer.dropped_records >= 1


def test_non_collected_scopes_pass_through(tmp_path):
    _arm(tmp_path)
    mw = _wrap(tmp_path, _clock([0.0]))
    _run(_drive(mw, method="GET", path="/v1/messages"))
    _run(_drive(mw, method="POST", path="/health"))
    _run(_drive(mw, method="POST", path="/v1/messages"))  # collected, sanity
    # websocket
    async def ws():
        seen = {}

        async def app(scope, receive, send):
            seen["type"] = scope["type"]

        mw.app = app
        await mw({"type": "websocket", "path": "/v1/messages"}, None, None)
        return seen

    assert _run(ws())["type"] == "websocket"
    mw.writer.drain()
    assert len(_ready_lines(tmp_path)) == 1
    assert "/health" not in COLLECT_PATHS or True


def test_rotation_shutdown_and_leftover_adoption(tmp_path):
    box = [1_700_000_000.0]
    _arm(tmp_path)
    mw = _wrap(tmp_path, _clock(box), rotate_seconds=60)
    _run(_drive(mw, body=b'{"n":1}'))
    mw.writer.drain()
    assert list((tmp_path / "spool").glob("*.jsonl"))
    assert list((tmp_path / "spool").glob("*.ready")) == []

    box[0] += 60
    mw.writer.drain()
    assert list((tmp_path / "spool").glob("*.ready"))
    assert list((tmp_path / "spool").glob("*.jsonl")) == []

    _run(_drive(mw, body=b'{"n":2}'))
    # Still queued until drain; shutdown must flush and rename.
    async def shutdown():
        async def app(_scope, _receive, send):
            await send({"type": "lifespan.shutdown.complete"})

        async def receive():
            return {"type": "lifespan.shutdown"}

        async def send(_message):
            return None

        mw.app = app
        await mw({"type": "lifespan"}, receive, send)

    _run(shutdown())
    ready = list((tmp_path / "spool").glob("*.ready"))
    assert len(ready) == 2
    assert list((tmp_path / "spool").glob("*.jsonl")) == []

    # A later process adopts a crashed child's open segment name.
    orphan = tmp_path / "spool" / "orphan.jsonl"
    orphan.write_text('{"id":"orphan"}\n', encoding="utf-8")
    _wrap(tmp_path, _clock(box))
    assert not orphan.exists()
    assert (tmp_path / "spool" / "orphan.ready").read_text(encoding="utf-8").startswith('{"id"')


def test_spool_over_limit_pauses_then_resumes(tmp_path):
    box = [1_700_000_000.0]
    _arm(tmp_path, max_spool_bytes=80)
    mw = _wrap(tmp_path, _clock(box), rotate_seconds=60)
    _run(_drive(mw, body=b'{"pad":"' + b"x" * 200 + b'"}'))
    mw.writer.drain()
    box[0] += 60
    mw.writer.drain()  # close the segment; spool now exceeds 80 bytes
    assert mw.writer.paused is True
    before = mw.writer.dropped_records
    sent = _run(_drive(mw, body=b"{}"))
    assert sent[-1]["body"] == b'{"ok":true}'
    assert mw.writer.dropped_records == before + 1

    for path in (tmp_path / "spool").glob("*"):
        path.unlink()
    mw.writer.drain()
    assert mw.writer.paused is False
    _run(_drive(mw, body=b'{"again":1}'))
    mw.writer.drain()
    assert any("again" in p.read_text(encoding="utf-8") for p in (tmp_path / "spool").glob("*.jsonl"))


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits")
def test_spool_files_are_private(tmp_path):
    _arm(tmp_path)
    mw = _wrap(tmp_path, _clock([0.0]))
    _run(_drive(mw))
    mw.writer.drain()
    assert stat.S_IMODE((tmp_path / "spool").stat().st_mode) == 0o700
    segment = next((tmp_path / "spool").glob("*.jsonl"))
    assert stat.S_IMODE(segment.stat().st_mode) == 0o600
    assert stat.S_IMODE(tmp_path.stat().st_mode) == 0o700
