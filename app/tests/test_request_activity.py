# app/tests/test_request_activity.py
"""Local in-flight / recent request snapshots for the tray menu."""
from __future__ import annotations

import asyncio
import json
import time

from kiro_gateway_tray import request_activity as ra
from kiro_gateway_tray.request_activity import (
    RequestActivityMiddleware,
    RequestActivityStore,
    extract_answer_preview_from_json,
    extract_question_preview,
    feed_sse_text,
    format_active_line,
    format_duration,
    format_finished_active_line,
    format_recent_line,
    load_snapshot,
    truncate_preview,
    wrap_app,
)


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


async def _drive_http(middleware, *, method, path, body, response_messages):
    scope = {"type": "http", "method": method, "path": path, "headers": []}
    half = max(1, len(body) // 2)
    frames = [
        {"type": "http.request", "body": body[:half], "more_body": True},
        {"type": "http.request", "body": body[half:], "more_body": False},
    ]
    idx = {"i": 0}
    sent: list[dict] = []

    async def receive():
        i = idx["i"]
        idx["i"] += 1
        return frames[i]

    async def send(message):
        sent.append(message)

    async def inner_app(scope, recv, snd):
        while True:
            msg = await recv()
            if msg["type"] == "http.request" and not msg.get("more_body", False):
                break
        for m in response_messages:
            await snd(m)

    # Swap the middleware's inner app for our stub.
    middleware.app = inner_app
    await middleware(scope, receive, send)
    return sent


# --- helpers -----------------------------------------------------------------

def test_truncate_preview():
    assert truncate_preview("hello", 10) == "hello"
    assert truncate_preview("a" * 50, 10).endswith("…")
    assert len(truncate_preview("a" * 50, 10)) == 10


def test_extract_question_openai_and_anthropic_blocks():
    openai_body = json.dumps({
        "model": "gpt-test",
        "messages": [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "第一问"},
            {"role": "assistant", "content": "答"},
            {"role": "user", "content": "请帮我检查网关是否卡住了，谢谢"},
        ],
    }).encode()
    assert "检查网关" in extract_question_preview(openai_body)

    anth_body = json.dumps({
        "model": "claude-test",
        "messages": [{
            "role": "user",
            "content": [{"type": "text", "text": "用一句话解释异步"}],
        }],
    }).encode()
    assert extract_question_preview(anth_body).startswith("用一句话")


def test_extract_question_responses_input():
    as_str = json.dumps({
        "model": "gpt-test",
        "input": "Responses 用字符串当提问",
    }).encode()
    assert "字符串" in extract_question_preview(as_str)

    as_list = json.dumps({
        "model": "gpt-test",
        "input": [
            {"role": "user", "content": "第一轮"},
            {"role": "assistant", "content": "答"},
            {
                "role": "user",
                "content": [{"type": "input_text", "text": "请总结网关预览逻辑"}],
            },
        ],
    }).encode()
    assert "预览逻辑" in extract_question_preview(as_list)


def test_extract_answer_json_openai_and_anthropic():
    openai = json.dumps({
        "choices": [{"message": {"role": "assistant", "content": "这是一段回复内容"}}],
    }).encode()
    assert "回复" in extract_answer_preview_from_json(openai)

    anth = json.dumps({
        "content": [{"type": "text", "text": "Anthropic 风格回复"}],
    }).encode()
    assert "Anthropic" in extract_answer_preview_from_json(anth)


def test_extract_answer_json_responses_output():
    body = json.dumps({
        "output": [{
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "Responses 非流式回复"}],
        }],
    }).encode()
    assert "非流式" in extract_answer_preview_from_json(body)


def test_feed_sse_openai_and_anthropic():
    acc: list[str] = []
    openai_chunk = (
        'data: {"choices":[{"delta":{"content":"你好"}}]}\n\n'
        'data: {"choices":[{"delta":{"content":"世界"}}]}\n\n'
        "data: [DONE]\n\n"
    ).encode()
    feed_sse_text(acc, openai_chunk)
    assert "".join(acc) == "你好世界"

    acc2: list[str] = []
    anth_chunk = (
        'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"流式"}}\n\n'
    ).encode()
    feed_sse_text(acc2, anth_chunk)
    assert "".join(acc2) == "流式"


def test_feed_sse_responses_output_text_delta():
    acc: list[str] = []
    chunk = (
        'data: {"type":"response.output_text.delta","delta":"你好"}\n\n'
        'data: {"type":"response.output_text.delta","delta":"Responses"}\n\n'
    ).encode()
    feed_sse_text(acc, chunk)
    assert "".join(acc) == "你好Responses"


def test_format_duration_and_menu_lines():
    assert format_duration(1.2) == "1.2s"
    assert format_duration(47) == "47s"
    assert format_duration(83) == "1m23s"

    active = ra.ActiveRequest(
        id="a1",
        started_at=time.time() - 12,
        model="claude-sonnet-4-very-long-name",
        path="/v1/messages",
        phase="streaming",
        question_preview="帮我改代码",
        prompt_tokens=1200,
        completion_tokens=56,
    )
    line = format_active_line(active, now=active.started_at + 12)
    assert "生成中" in line
    assert "帮我改代码" in line
    assert "\n" in line
    assert "⬆ 1.2k" in line
    assert "⬇ 56" in line
    assert "问: 帮我改代码" in line
    lines = line.split("\n")
    assert len(lines) == 3

    recent = ra.RecentRequest(
        id="r1",
        started_at=1,
        finished_at=2,
        model="m",
        path="/v1/chat/completions",
        ok=True,
        duration_ms=1200,
        question_preview="问什么",
        answer_preview="答什么",
        prompt_tokens=10,
        completion_tokens=3,
    )
    rline = format_recent_line(recent)
    assert "✓" in rline
    assert "问: 问什么" in rline
    assert "答: 答什么" in rline
    assert "⬆ 10" in rline
    assert "⬇ 3" in rline
    assert "\n" in rline
    lines = rline.split("\n")
    assert len(lines) == 4
    assert lines[0].startswith("0") or "✓" in lines[0]  # HH:MM ✓ …

    finished_ok = format_finished_active_line(recent)
    assert finished_ok.startswith("已完成 ·")
    assert "问什么" in finished_ok
    assert "⬆ 10" in finished_ok
    assert "⬇ 3" in finished_ok
    assert finished_ok.count("\n") == 2
    finished_fail = format_finished_active_line(ra.RecentRequest(
        id="r2",
        started_at=1,
        finished_at=2,
        model="m",
        path="/v1/messages",
        ok=False,
        duration_ms=3200,
        question_preview="坏了",
        answer_preview="",
        error_preview="HTTP 500",
    ))
    assert finished_fail.startswith("失败 ·")
    assert "坏了" in finished_fail


def test_format_token_n():
    assert ra.format_token_n(0) == "0"
    assert ra.format_token_n(999) == "999"
    assert ra.format_token_n(1200) == "1.2k"
    assert ra.format_token_n(1_500_000) == "1.5M"


def test_feed_sse_chunk_parses_usage():
    acc: list[str] = []
    chunk = (
        'data: {"type":"message_start","message":{"usage":{"input_tokens":40,"output_tokens":0}}}\n\n'
        'data: {"type":"content_block_delta","delta":{"text":"hi"}}\n\n'
        'data: {"type":"message_delta","usage":{"output_tokens":12}}\n\n'
    ).encode()
    prompt, completion = ra.feed_sse_chunk(acc, chunk)
    assert "".join(acc) == "hi"
    assert prompt == 40
    assert completion == 12


def test_feed_sse_chunk_parses_responses_nested_usage():
    """Responses API puts usage under response.completed → response.usage."""
    acc: list[str] = []
    chunk = (
        'data: {"type":"response.output_text.delta","delta":"找到了"}\n\n'
        'data: {"type":"response.completed","response":{'
        '"usage":{"input_tokens":121203,"output_tokens":312,"total_tokens":121515}'
        '}}\n\n'
    ).encode()
    prompt, completion = ra.feed_sse_chunk(acc, chunk)
    assert "".join(acc) == "找到了"
    assert prompt == 121203
    assert completion == 312


def test_estimate_tokens_from_text_uses_tiktoken_not_bytes_div4():
    """CJK live estimates must not use UTF-8 bytes÷4 (undercounts badly)."""
    text = (
        "找到了——三个文件都在废纸篓 (~/.Trash) 里：\n"
        "- Additional_Tools_for_Xcode_27_beta_3.dmg - 62M\n"
        "- Command_Line_Tools_27_beta_3.dmg - 499M\n"
        "- Xcode_27_beta_3.xip - 1.8G\n"
        "所以是被移到废纸篓了（不是我干的）。"
    )
    est = ra.estimate_tokens_from_text(text)
    naive = ra.estimate_tokens_from_bytes(len(text.encode("utf-8")))
    # Full short reply should land well above the old capped bytes÷4 ≈78 trap.
    assert est > 100
    assert est > naive
    # Empty / whitespace-only
    assert ra.estimate_tokens_from_text("") == 0


def test_estimate_prompt_tokens_from_body_uses_text_not_raw_bytes():
    """⬆ should tiktoken message text, not inflate on JSON/base64 padding."""
    body = json.dumps({
        "model": "m",
        "messages": [
            {"role": "system", "content": "你是助手"},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "请用中文解释一下废纸篓里的三个文件"},
                    {
                        "type": "image_url",
                        "image_url": {"url": "data:image/png;base64," + ("A" * 8000)},
                    },
                ],
            },
        ],
    }, ensure_ascii=False).encode()
    est = ra.estimate_prompt_tokens_from_body(body)
    naive = ra.estimate_tokens_from_bytes(len(body))
    # Image stub (+100) + Chinese text — far below raw body bytes÷4.
    assert est < naive
    assert est > 50
    text, images = ra.extract_prompt_text_for_estimate(body)
    assert images == 1
    assert "废纸篓" in text
    assert "AAAA" not in text


def test_middleware_json_completion_uses_tiktoken_when_no_usage(tmp_path):
    store = RequestActivityStore(tmp_path / "request_activity.json")
    mw = RequestActivityMiddleware(None, store)
    body = json.dumps({
        "model": "claude-x",
        "messages": [{"role": "user", "content": "你好"}],
    }).encode()
    answer = "找到了——三个文件都在废纸篓里，可以直接清空回收空间。" * 3
    resp = json.dumps({
        "choices": [{"message": {"role": "assistant", "content": answer}}],
    }, ensure_ascii=False).encode()
    responses = [
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-type", b"application/json")],
        },
        {"type": "http.response.body", "body": resp, "more_body": False},
    ]
    scope = {"type": "http", "method": "POST", "path": "/v1/chat/completions", "headers": []}
    frames = [{"type": "http.request", "body": body, "more_body": False}]
    idx = {"i": 0}

    async def receive():
        i = idx["i"]
        idx["i"] += 1
        if i < len(frames):
            return frames[i]
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(_m):
        return None

    async def inner_app(_scope, recv, snd):
        while True:
            msg = await recv()
            if msg["type"] == "http.request" and not msg.get("more_body", False):
                break
        for frame in responses:
            await snd(frame)

    mw.app = inner_app
    _run(mw(scope, receive, send))
    recent = store.snapshot().recent[0]
    # No usage in body → tiktoken on answer text, not response bytes÷4.
    assert recent.completion_tokens == ra.estimate_tokens_from_text(answer)
    assert recent.completion_tokens > ra.estimate_tokens_from_bytes(len(resp))
    # Prompt also started from tiktoken text estimate (may stay if no usage).
    assert recent.prompt_tokens == ra.estimate_prompt_tokens_from_body(body)


def test_middleware_applies_responses_completed_usage(tmp_path):
    """Streaming /v1/responses must adopt response.usage, not live estimate."""
    store = RequestActivityStore(tmp_path / "request_activity.json")
    mw = RequestActivityMiddleware(None, store)
    body = json.dumps({
        "model": "kiro-o-4.8",
        "input": "废纸篓里有什么",
        "stream": True,
    }).encode()
    long_delta = "找到了——" + ("三个文件都在废纸篓里。" * 20)
    responses = [
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-type", b"text/event-stream")],
        },
        {
            "type": "http.response.body",
            "body": (
                f'data: {{"type":"response.output_text.delta","delta":{json.dumps(long_delta, ensure_ascii=False)}}}\n\n'
            ).encode(),
            "more_body": True,
        },
        {
            "type": "http.response.body",
            "body": (
                'data: {"type":"response.completed","response":{'
                '"usage":{"input_tokens":5000,"output_tokens":280,"total_tokens":5280}'
                '}}\n\n'
            ).encode(),
            "more_body": True,
        },
        {"type": "http.response.body", "body": b"", "more_body": False},
    ]

    scope = {"type": "http", "method": "POST", "path": "/v1/responses", "headers": []}
    frames = [{"type": "http.request", "body": body, "more_body": False}]
    idx = {"i": 0}

    async def receive():
        i = idx["i"]
        idx["i"] += 1
        if i < len(frames):
            return frames[i]
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(_message):
        return None

    async def inner_app(_scope, recv, snd):
        while True:
            msg = await recv()
            if msg["type"] == "http.request" and not msg.get("more_body", False):
                break
        for frame in responses:
            await snd(frame)

    mw.app = inner_app
    _run(mw(scope, receive, send))

    recent = store.snapshot().recent[0]
    assert recent.path == "/v1/responses"
    assert recent.prompt_tokens == 5000
    assert recent.completion_tokens == 280
    assert "找到了" in recent.answer_preview


def test_middleware_updates_tokens_during_stream(tmp_path):
    store = RequestActivityStore(tmp_path / "request_activity.json")
    mw = RequestActivityMiddleware(None, store)
    body = json.dumps({
        "model": "claude-x",
        "messages": [{"role": "user", "content": "慢不慢"}],
    }).encode()
    responses = [
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-type", b"text/event-stream")],
        },
        {
            "type": "http.response.body",
            "body": (
                'data: {"type":"message_start","message":'
                '{"usage":{"input_tokens":100,"output_tokens":0}}}\n\n'
            ).encode(),
            "more_body": True,
        },
        {
            "type": "http.response.body",
            "body": (
                'data: {"type":"content_block_delta","delta":{"text":"还行"}}\n\n'
                'data: {"type":"message_delta","usage":{"output_tokens":7}}\n\n'
            ).encode(),
            "more_body": True,
        },
        {"type": "http.response.body", "body": b"", "more_body": False},
    ]

    # Drive until mid-stream so we can observe active tokens, then finish.
    scope = {"type": "http", "method": "POST", "path": "/v1/messages", "headers": []}
    frames = [
        {"type": "http.request", "body": body, "more_body": False},
    ]
    idx = {"i": 0}
    mid_snap = {"active": None}

    async def receive():
        i = idx["i"]
        idx["i"] += 1
        if i < len(frames):
            return frames[i]
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(_message):
        return None

    async def inner_app(scope, recv, snd):
        while True:
            msg = await recv()
            if msg["type"] == "http.request" and not msg.get("more_body", False):
                break
        await snd(responses[0])
        await snd(responses[1])
        mid_snap["active"] = store.snapshot().active
        await snd(responses[2])
        await snd(responses[3])

    mw.app = inner_app
    _run(mw(scope, receive, send))

    assert mid_snap["active"]
    mid = mid_snap["active"][0]
    assert mid.prompt_tokens == 100
    # After first usage event completion may still be 0; after second body it
    # should land on the real usage before finish.
    recent = store.snapshot().recent[0]
    assert recent.prompt_tokens == 100
    assert recent.completion_tokens == 7
    assert "还行" in recent.answer_preview


def test_extract_question_skips_system_reminder_noise():
    body = json.dumps({
        "model": "m",
        "messages": [{
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": "<system-reminder data-role=\"user-context\">secret</system-reminder>",
                },
                {"type": "text", "text": "黑洞是怎么形成的？"},
            ],
        }],
    }).encode()
    preview = extract_question_preview(body)
    assert "黑洞" in preview
    assert "system-reminder" not in preview


# --- store -------------------------------------------------------------------

def test_store_begin_finish_ring_and_persist(tmp_path):
    path = tmp_path / "request_activity.json"
    store = RequestActivityStore(path, recent_limit=3)

    r1 = store.begin(model="m1", path="/v1/messages", question_preview="q1")
    snap = store.snapshot()
    assert len(snap.active) == 1
    assert snap.active[0].question_preview == "q1"

    store.set_phase(r1, "streaming")
    assert store.snapshot().active[0].phase == "streaming"

    store.finish(r1, ok=True, answer_preview="a1")
    snap = store.snapshot()
    assert snap.active == []
    assert len(snap.recent) == 1
    assert snap.recent[0].answer_preview == "a1"

    for i in range(5):
        rid = store.begin(model="m", path="/v1/messages", question_preview=f"q{i}")
        store.finish(rid, ok=True, answer_preview=f"a{i}")
    assert len(store.snapshot().recent) == 3
    assert store.snapshot().recent[0].question_preview == "q4"

    # Reload from disk keeps recent, clear_active on wrap drops active only.
    store2 = RequestActivityStore(path, recent_limit=3)
    assert len(store2.snapshot().recent) == 3

    loaded = load_snapshot(path)
    assert len(loaded.recent) == 3


def test_store_clear_active_on_wrap(tmp_path):
    path = tmp_path / "request_activity.json"
    store = RequestActivityStore(path)
    store.begin(model="m", path="/v1/messages", question_preview="orphan")
    assert len(store.snapshot().active) == 1

    sentinel = object()

    def fake_app(scope, receive, send):
        raise AssertionError("should not be called")

    wrapped = wrap_app(fake_app, data_dir=tmp_path)
    assert isinstance(wrapped, RequestActivityMiddleware)
    assert wrapped is not sentinel
    assert load_snapshot(path).active == []


# --- middleware --------------------------------------------------------------

def test_middleware_tracks_streaming_success(tmp_path):
    store = RequestActivityStore(tmp_path / "request_activity.json")
    mw = RequestActivityMiddleware(None, store)
    body = json.dumps({
        "model": "claude-x",
        "messages": [{"role": "user", "content": "慢不慢"}],
    }).encode()
    responses = [
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-type", b"text/event-stream")],
        },
        {
            "type": "http.response.body",
            "body": 'data: {"type":"content_block_delta","delta":{"text":"还行"}}\n\n'.encode(),
            "more_body": True,
        },
        {"type": "http.response.body", "body": b"", "more_body": False},
    ]
    _run(_drive_http(mw, method="POST", path="/v1/messages", body=body, response_messages=responses))
    snap = store.snapshot()
    assert snap.active == []
    assert len(snap.recent) == 1
    r = snap.recent[0]
    assert r.ok is True
    assert r.model == "claude-x"
    assert "慢不慢" in r.question_preview
    assert "还行" in r.answer_preview


def test_middleware_tracks_json_error(tmp_path):
    store = RequestActivityStore(tmp_path / "request_activity.json")
    mw = RequestActivityMiddleware(None, store)
    body = json.dumps({
        "model": "m",
        "messages": [{"role": "user", "content": "hello"}],
    }).encode()
    err = json.dumps({"error": {"message": "upstream timeout"}}).encode()
    responses = [
        {
            "type": "http.response.start",
            "status": 504,
            "headers": [(b"content-type", b"application/json")],
        },
        {"type": "http.response.body", "body": err, "more_body": False},
    ]
    _run(_drive_http(
        mw, method="POST", path="/v1/chat/completions", body=body, response_messages=responses,
    ))
    r = store.snapshot().recent[0]
    assert r.ok is False
    assert "timeout" in r.error_preview


def test_middleware_ignores_health(tmp_path):
    store = RequestActivityStore(tmp_path / "request_activity.json")
    mw = RequestActivityMiddleware(None, store)
    called = {"n": 0}

    async def inner(scope, receive, send):
        called["n"] += 1
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok", "more_body": False})

    mw.app = inner

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(_m):
        return None

    _run(mw({"type": "http", "method": "GET", "path": "/health", "headers": []}, receive, send))
    assert called["n"] == 1
    assert store.snapshot().active == []
    assert store.snapshot().recent == []


def test_collect_paths_includes_responses():
    assert "/v1/chat/completions" in ra.COLLECT_PATHS
    assert "/v1/messages" in ra.COLLECT_PATHS
    assert "/v1/responses" in ra.COLLECT_PATHS


def test_middleware_tracks_responses_path(tmp_path):
    store = RequestActivityStore(tmp_path / "request_activity.json")
    mw = RequestActivityMiddleware(None, store)
    body = json.dumps({
        "model": "kiro-s-4.6",
        "input": "ping",
    }).encode()
    responses = [
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-type", b"application/json")],
        },
        {
            "type": "http.response.body",
            "body": json.dumps({
                "output": [{
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "pong"}],
                }],
            }).encode(),
            "more_body": False,
        },
    ]
    _run(_drive_http(
        mw, method="POST", path="/v1/responses", body=body, response_messages=responses,
    ))
    snap = store.snapshot()
    assert snap.active == []
    assert len(snap.recent) == 1
    assert snap.recent[0].path == "/v1/responses"
    assert snap.recent[0].model == "kiro-s-4.6"
    assert snap.recent[0].ok is True
    assert "ping" in snap.recent[0].question_preview
    assert "pong" in snap.recent[0].answer_preview


def test_stale_active_filtered_on_load(tmp_path):
    path = tmp_path / "request_activity.json"
    path.write_text(json.dumps({
        "active": [{
            "id": "old",
            "started_at": time.time() - 9999,
            "model": "m",
            "path": "/v1/messages",
            "phase": "streaming",
            "question_preview": "gone",
        }],
        "recent": [],
    }), encoding="utf-8")
    snap = load_snapshot(path)
    assert snap.active == []
