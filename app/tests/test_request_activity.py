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


def test_format_unknown_completion_as_dash_but_preserve_real_zero():
    active = ra.ActiveRequest(
        id="unknown",
        started_at=0,
        model="m",
        path="/v1/chat/completions",
        phase="streaming",
        question_preview="q",
        completion_tokens=0,
        completion_known=False,
    )
    assert "⬇ —" in format_active_line(active, now=1)

    complete = ra.RecentRequest(
        id="zero",
        started_at=0,
        finished_at=1,
        model="m",
        path="/v1/chat/completions",
        ok=True,
        duration_ms=1,
        question_preview="q",
        answer_preview="",
        completion_tokens=0,
        completion_known=True,
    )
    assert "⬇ 0" in format_recent_line(complete)


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


def test_feed_sse_chunk_keeps_estimating_after_initial_anthropic_zero_usage():
    """Anthropic message_start's output zero must not freeze live ⬇ at zero."""
    acc: list[str] = []
    start = (
        'data: {"type":"message_start","message":'
        '{"usage":{"input_tokens":40,"output_tokens":0}}}\n\n'
    ).encode()
    prompt, completion = ra.feed_sse_chunk(acc, start)
    assert prompt == 40
    assert completion is None

    reasoning = (
        'data: {"choices":[{"delta":{"reasoning_content":"先分析一下"}}]}\n\n'
    ).encode()
    prompt, completion = ra.feed_sse_chunk(acc, reasoning)
    assert prompt is None
    assert completion is None
    assert "".join(acc) == "先分析一下"


def test_feed_sse_chunk_counts_anthropic_thinking_delta():
    acc: list[str] = []
    chunk = (
        'data: {"type":"content_block_delta","delta":'
        '{"type":"thinking_delta","thinking":"推理过程"}}\n\n'
    ).encode()
    ra.feed_sse_chunk(acc, chunk)
    assert "".join(acc) == "推理过程"


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


def test_middleware_recovers_split_reasoning_and_final_usage(tmp_path):
    """ASGI body boundaries must not drop split SSE JSON events."""
    store = RequestActivityStore(tmp_path / "request_activity.json")
    mw = RequestActivityMiddleware(None, store)
    body = json.dumps({
        "model": "gpt-5.6-sol",
        "messages": [{"role": "user", "content": "分析一下"}],
        "stream": True,
    }).encode()
    responses = [
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-type", b"text/event-stream")],
        },
        {
            "type": "http.response.body",
            "body": b'data: {"choices":[{"delta":{"reasoning_content":"\xe6\x8e\xa8',
            "more_body": True,
        },
        {
            "type": "http.response.body",
            "body": b'\xe7\x90\x86"}}]}\n\n',
            "more_body": True,
        },
        {
            "type": "http.response.body",
            "body": b'data: {"choices":[{"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":11,"completion_tokens":',
            "more_body": True,
        },
        {
            "type": "http.response.body",
            "body": b'123}}\n\n',
            "more_body": True,
        },
        {"type": "http.response.body", "body": b"", "more_body": False},
    ]
    input_sent = False
    mid_completion = {"value": 0}

    async def receive():
        nonlocal input_sent
        if not input_sent:
            input_sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(_message):
        return None

    async def inner_app(_scope, recv, snd):
        while True:
            message = await recv()
            if message["type"] == "http.request" and not message.get("more_body", False):
                break
        await snd(responses[0])
        await snd(responses[1])
        await snd(responses[2])
        mid_completion["value"] = store.snapshot().active[0].completion_tokens
        await snd(responses[3])
        await snd(responses[4])

    mw.app = inner_app
    _run(mw(
        {"type": "http", "method": "POST", "path": "/v1/chat/completions", "headers": []},
        receive,
        send,
    ))
    recent = store.snapshot().recent[0]
    assert mid_completion["value"] > 0
    assert recent.completion_tokens == 123
    assert "推理" in recent.answer_preview


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


def test_extract_model_from_truncated_json():
    """Oversized captures truncate mid-JSON; model must still resolve."""
    truncated = (
        b'{"model":"claude-opus-4.8","messages":[{"role":"user","content":"'
        + ("上下文" * 200).encode()  # incomplete string / object
    )
    assert ra.extract_model(truncated) == "claude-opus-4.8"
    assert ra.extract_model(b"") == "unknown"
    assert ra.extract_model(b'{"messages":[]}') == "unknown"


def test_extract_question_from_truncated_user_query():
    """Rolling-tail style fragment still surfaces Cursor <user_query> text."""
    fragment = (
        b'...injected noise...",{"role":"user","content":"'
        + "<user_query>请检查网关是否卡住</user_query>".encode()
    )
    preview = extract_question_preview(fragment)
    assert "检查网关" in preview

    # Incomplete closing tag at end of capture.
    open_only = "prefix <user_query>只看到半截提问".encode()
    assert "半截提问" in extract_question_preview(open_only)


def test_request_body_buffer_keeps_head_model_and_tail_query():
    """Head+tail budget retains model prefix and trailing user_query."""
    # Shrink caps so the test stays small/fast.
    old_head, old_tail = ra._REQ_BODY_HEAD_CAP, ra._REQ_BODY_TAIL_CAP
    try:
        ra._REQ_BODY_HEAD_CAP = 200
        ra._REQ_BODY_TAIL_CAP = 120
        buf = ra._RequestBodyBuffer()
        head = b'{"model":"gpt-5.6-sol","messages":[{"role":"user","content":"'
        middle = b"M" * 800
        tail = ("<user_query>尾部真实问题</user_query>" + '"}]}').encode()
        buf.extend(head)
        buf.extend(middle)
        buf.extend(tail)
        assert buf.truncated is True
        assert buf.total == len(head) + len(middle) + len(tail)
        preview = buf.preview_bytes()
        assert ra.extract_model(preview) == "gpt-5.6-sol"
        assert "尾部真实问题" in extract_question_preview(preview)
        # Token fallback uses full on-wire size, not just the retained window.
        est = ra.estimate_prompt_tokens_from_body(
            preview, total_bytes=buf.total,
        )
        assert est == ra.estimate_tokens_from_bytes(buf.total)
        assert est > ra.estimate_tokens_from_bytes(len(preview))
    finally:
        ra._REQ_BODY_HEAD_CAP = old_head
        ra._REQ_BODY_TAIL_CAP = old_tail


def test_middleware_recovers_model_from_oversized_body(tmp_path):
    """Activity menu must not show unknown when the body exceeds the cap."""
    path = tmp_path / "request_activity.json"
    store = RequestActivityStore(path)
    middleware = RequestActivityMiddleware(None, store)

    old_head, old_tail = ra._REQ_BODY_HEAD_CAP, ra._REQ_BODY_TAIL_CAP
    try:
        ra._REQ_BODY_HEAD_CAP = 180
        ra._REQ_BODY_TAIL_CAP = 100
        # Build a body larger than head+tail with model at front and query at end.
        prefix = b'{"model":"claude-opus-4.8","stream":true,"messages":[{"role":"user","content":"'
        suffix = ("<user_query>超大上下文里的真实提问</user_query>" + '"}]}').encode()
        filler = b"Z" * 500
        body = prefix + filler + suffix

        _run(_drive_http(
            middleware,
            method="POST",
            path="/v1/chat/completions",
            body=body,
            response_messages=[
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [(b"content-type", b"text/event-stream")],
                },
                {
                    "type": "http.response.body",
                    "body": b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n',
                    "more_body": True,
                },
                {"type": "http.response.body", "body": b"", "more_body": False},
            ],
        ))
        active_or_recent = store.snapshot().active or store.snapshot().recent
        assert active_or_recent
        entry = active_or_recent[0]
        assert entry.model == "claude-opus-4.8"
        assert "真实提问" in entry.question_preview
        assert entry.prompt_tokens == ra.estimate_tokens_from_bytes(len(body))
    finally:
        ra._REQ_BODY_HEAD_CAP = old_head
        ra._REQ_BODY_TAIL_CAP = old_tail


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



def test_store_throttled_update_eventually_persists(tmp_path, monkeypatch):
    """A lone update inside the throttle window must reach disk later."""
    monkeypatch.setattr(ra, "_TOKEN_PERSIST_INTERVAL", 0.03)
    path = tmp_path / "request_activity.json"
    store = RequestActivityStore(path)
    rid = store.begin(model="m", path="/v1/messages", question_preview="q")

    store.update_tokens(rid, completion_tokens=17, completion_known=True)
    assert load_snapshot(path).active[0].completion_tokens == 0

    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        loaded = load_snapshot(path)
        if loaded.active and loaded.active[0].completion_tokens == 17:
            break
        time.sleep(0.01)
    assert loaded.active[0].completion_tokens == 17
    assert loaded.active[0].completion_known is True


def test_store_finish_cancels_stale_delayed_flush(tmp_path, monkeypatch):
    """A pre-finish timer cannot overwrite the newer recent-only snapshot."""
    monkeypatch.setattr(ra, "_TOKEN_PERSIST_INTERVAL", 0.04)
    path = tmp_path / "request_activity.json"
    store = RequestActivityStore(path)
    rid = store.begin(model="m", path="/v1/messages", question_preview="q")
    store.update_tokens(rid, completion_tokens=23, completion_known=True)
    store.finish(rid, ok=True, answer_preview="done")

    time.sleep(0.09)
    loaded = load_snapshot(path)
    assert loaded.active == []
    assert loaded.recent[0].completion_tokens == 23
    assert loaded.recent[0].completion_known is True


def test_completion_known_round_trip_and_legacy_compatibility(tmp_path):
    """Explicit booleans survive disk, while an old missing field stays unknown."""
    path = tmp_path / "request_activity.json"
    now = time.time()
    path.write_text(json.dumps({
        "active": [{
            "id": "false", "started_at": now, "model": "m", "path": "/v1/messages",
            "phase": "streaming", "question_preview": "q", "completion_known": False,
        }],
        "recent": [
            {
                "id": "true", "started_at": 1, "finished_at": 2, "model": "m",
                "path": "/v1/messages", "ok": True, "duration_ms": 1,
                "question_preview": "q", "answer_preview": "", "completion_known": True,
            },
            {
                "id": "legacy", "started_at": 1, "finished_at": 2, "model": "m",
                "path": "/v1/messages", "ok": True, "duration_ms": 1,
                "question_preview": "q", "answer_preview": "",
            },
        ],
    }), encoding="utf-8")

    snap = load_snapshot(path, now=now)
    assert snap.active[0].completion_known is False
    assert snap.recent[0].completion_known is True
    assert snap.recent[1].completion_known is None
    assert "⬇ 0" in format_recent_line(snap.recent[0])
    assert "⬇ —" in format_recent_line(snap.recent[1])


def test_feed_sse_tool_output_is_counted_without_polluting_preview():
    """All streaming protocols feed a separate tool-only token accumulator."""
    cases = [
        (
            'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":'
            '{"name":"lookup","arguments":"{\\"id\\":1}"}}]}}]}\n\n',
            "lookup", '{"id":1}',
        ),
        (
            'data: {"type":"content_block_start","index":0,"content_block":'
            '{"type":"tool_use","id":"toolu_1","name":"lookup","input":{}}}\n\n'
            'data: {"type":"content_block_delta","index":0,"delta":'
            '{"type":"input_json_delta","partial_json":"{\\"id\\":1}"}}\n\n',
            "lookup", '{"id":1}',
        ),
        (
            'data: {"type":"response.output_item.added","item":'
            '{"id":"fc_1","type":"function_call","name":"lookup","arguments":""}}\n\n'
            'data: {"type":"response.function_call_arguments.delta","item_id":"fc_1",'
            '"delta":"{\\"id\\":1}"}\n\n'
            'data: {"type":"response.function_call_arguments.done","item_id":"fc_1",'
            '"name":"lookup","arguments":"{\\"id\\":1}"}\n\n',
            "lookup", '{"id":1}',
        ),
    ]
    for payload, name, arguments in cases:
        preview: list[str] = []
        output: list[str] = []
        ra.feed_sse_chunk(preview, payload.encode(), output_acc=output, tool_state={})
        joined = "".join(output)
        assert preview == []
        assert joined.count(name) == 1
        assert joined.count(arguments) == 1
        assert ra.estimate_tokens_from_text(joined) > 0


def test_sse_tail_is_bounded_without_newline():
    """An oversized malformed SSE line cannot grow retained memory forever."""
    _, _, tail = ra.feed_sse_buffered_chunk([], b"x" * (ra._SSE_TAIL_CAP + 50), b"")
    assert len(tail) == ra._SSE_TAIL_CAP


def test_middleware_openai_tool_estimate_is_overridden_by_final_usage(tmp_path):
    """Tool-only live estimation yields to authoritative terminal usage."""
    store = RequestActivityStore(tmp_path / "request_activity.json")
    mw = RequestActivityMiddleware(None, store)
    body = json.dumps({
        "model": "m", "messages": [{"role": "user", "content": "use tool"}], "stream": True,
    }).encode()
    frames = [
        {"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"text/event-stream")]},
        {"type": "http.response.body", "body": (
            'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":'
            '{"name":"lookup","arguments":"{\\"id\\":1}"}}]},"finish_reason":null}]}\n\n'
        ).encode(), "more_body": True},
        {"type": "http.response.body", "body": (
            'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}],'
            '"usage":{"prompt_tokens":9,"completion_tokens":31}}\n\n'
        ).encode(), "more_body": True},
        {"type": "http.response.body", "body": b"", "more_body": False},
    ]
    live = {"tokens": 0, "preview": ""}

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(_message):
        return None

    async def app(_scope, recv, snd):
        await recv()
        await snd(frames[0])
        await snd(frames[1])
        active = store.snapshot().active[0]
        live["tokens"] = active.completion_tokens
        live["preview"] = active.question_preview
        await snd(frames[2])
        await snd(frames[3])

    mw.app = app
    _run(mw({"type": "http", "method": "POST", "path": "/v1/chat/completions", "headers": []}, receive, send))
    recent = store.snapshot().recent[0]
    assert live["tokens"] > 0
    assert recent.answer_preview == ""
    assert recent.completion_tokens == 31
    assert recent.completion_known is True
