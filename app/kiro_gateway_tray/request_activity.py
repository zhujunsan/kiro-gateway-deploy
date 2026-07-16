# app/kiro_gateway_tray/request_activity.py
"""Local in-flight / recent request snapshots for the tray menu.

Lives beside (not inside) usage telemetry: same ASGI wrap pattern as
``telemetry.py`` / ``speedtest.py``, but **never uploads** prompt or reply
text. The gateway child writes ``{data_dir}/request_activity.json``; the tray
parent reads it on a short refresh loop.

Tracks only ``POST /v1/chat/completions``, ``POST /v1/messages``, and
``POST /v1/responses``.
"""
from __future__ import annotations

import json
import re
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .log import logger

COLLECT_PATHS = frozenset({
    "/v1/chat/completions",
    "/v1/messages",
    "/v1/responses",
})

RECENT_LIMIT = 10
PREVIEW_CHARS = 72  # multi-line recent items can show a bit more than one tight row
# Accumulate enough assistant text for live tiktoken estimates before final
# usage arrives. Preview display still truncates via truncate_preview().
ANSWER_ACCUM_CAP = 100_000
# Match kiro.tokenizer.CLAUDE_CORRECTION_FACTOR so live ⬇ tracks gateway usage.
_CLAUDE_CORRECTION_FACTOR = 1.15
# Same stub the gateway tokenizer uses for image blocks in prompt estimates.
_IMAGE_TOKEN_ESTIMATE = 100
# Lazy tiktoken Encoding: None=untried, False=unavailable, else Encoding.
_tiktoken_encoding: Any = None
# Claude Code / IDE wrappers inject these into user turns; skip for menu preview.
_NOISE_PREFIXES = (
    "<system-reminder",
    "<system_reminder",
    "<user-context",
)
# Cursor/Claude Code wrap the *actual* prompt in a <user_query> tag, preceded
# by large injected-context blocks (open files, attachments, timestamp, ...).
# Those wrapper blocks live in the SAME text block as the real query rather
# than as a separate message part, so a simple startswith() check on the
# whole block misses them. Prefer whatever is inside the last <user_query>
# tag; otherwise strip known noise blocks from anywhere in the text.
_USER_QUERY_RE = re.compile(r"<user_query>(.*?)</user_query>", re.DOTALL | re.IGNORECASE)
_NOISE_BLOCK_TAGS = (
    "system-reminder",
    "system_reminder",
    "user-context",
    "open_and_recently_viewed_files",
    "image_files",
    "attached_files",
    "attachment",
    "timestamp",
)
_NOISE_BLOCK_RE = re.compile(
    r"<(" + "|".join(_NOISE_BLOCK_TAGS) + r")\b[^>]*>.*?</\1>",
    re.DOTALL | re.IGNORECASE,
)
# Buffer budget for activity previews (~4 MB total). Keep a large head (model
# is near the front) and a rolling tail (Cursor ``<user_query>`` is near the end).
_REQ_BODY_HEAD_CAP = 3_750_000
_REQ_BODY_TAIL_CAP = 250_000
_REQ_BODY_CAP = _REQ_BODY_HEAD_CAP + _REQ_BODY_TAIL_CAP  # backward-compat alias
_STALE_ACTIVE_SECONDS = 600  # drop orphaned in-flight rows after 10 minutes
_ACTIVITY_FILENAME = "request_activity.json"
_TOKEN_PERSIST_INTERVAL = 0.45  # throttle disk writes while tokens climb
# Bound an incomplete SSE line. Normal gateway events are far smaller; an
# oversized unterminated line is degraded instead of growing memory forever.
_SSE_TAIL_CAP = 1_000_000
# Truncated / non-JSON bodies: ``"model": "…"`` is almost always near the start.
_MODEL_FIELD_RE = re.compile(rb'"model"\s*:\s*"((?:\\.|[^"\\]){1,256})"')
# Incomplete ``<user_query>`` at the end of a truncated capture (no close tag).
_USER_QUERY_OPEN_RE = re.compile(
    r"<user_query>\s*(.*)\Z", re.DOTALL | re.IGNORECASE
)

_PHASE_WAITING = "waiting"
_PHASE_STREAMING = "streaming"
_PHASE_RESPONDING = "responding"

_PHASE_ZH = {
    _PHASE_WAITING: "等待响应",
    _PHASE_STREAMING: "生成中",
    _PHASE_RESPONDING: "生成中",
}


# --- data shapes -------------------------------------------------------------

@dataclass
class ActiveRequest:
    id: str
    started_at: float
    model: str
    path: str
    phase: str
    question_preview: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    # False=the stream has not yielded countable output; None=legacy snapshot.
    completion_known: bool | None = None


@dataclass
class RecentRequest:
    id: str
    started_at: float
    finished_at: float
    model: str
    path: str
    ok: bool
    duration_ms: int
    question_preview: str
    answer_preview: str
    error_preview: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    # False=the response ended without countable output; None=legacy snapshot.
    completion_known: bool | None = None


@dataclass
class ActivitySnapshot:
    active: list[ActiveRequest] = field(default_factory=list)
    recent: list[RecentRequest] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "active": [asdict(a) for a in self.active],
            "recent": [asdict(r) for r in self.recent],
        }


@dataclass
class _RequestBodyBuffer:
    """Capture request bytes with a fixed head + rolling tail budget.

    Large Cursor payloads exceed the old single 4 MB cap mid-JSON. Keeping only
    the prefix made ``json.loads`` fail (model → unknown) and dropped the
    trailing ``<user_query>``. Head retains ``model``; tail retains the query.
    """

    head: bytearray = field(default_factory=bytearray)
    tail: bytearray = field(default_factory=bytearray)
    total: int = 0
    truncated: bool = False

    def extend(self, chunk: bytes) -> None:
        if not chunk:
            return
        self.total += len(chunk)
        if not self.truncated:
            self.head.extend(chunk)
            budget = _REQ_BODY_HEAD_CAP + _REQ_BODY_TAIL_CAP
            if len(self.head) <= budget:
                return
            spill = bytes(self.head[_REQ_BODY_HEAD_CAP:])
            del self.head[_REQ_BODY_HEAD_CAP:]
            self.truncated = True
            self._extend_tail(spill)
            return
        self._extend_tail(chunk)

    def _extend_tail(self, chunk: bytes) -> None:
        self.tail.extend(chunk)
        overflow = len(self.tail) - _REQ_BODY_TAIL_CAP
        if overflow > 0:
            del self.tail[:overflow]

    def preview_bytes(self) -> bytes:
        """Bytes for model / question / token preview extraction."""
        if not self.truncated:
            return bytes(self.head)
        return bytes(self.head) + bytes(self.tail)


# --- text helpers ------------------------------------------------------------

def truncate_preview(text: str, limit: int = PREVIEW_CHARS) -> str:
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)] + "…"


def _is_injected_noise(text: str) -> bool:
    t = (text or "").lstrip()
    return any(t.startswith(p) for p in _NOISE_PREFIXES)


def _extract_real_user_text(text: str) -> str:
    """Pull the actual user prompt out of IDE-injected wrapper text.

    Prefers the content of the last ``<user_query>`` tag (Cursor/Claude Code
    append it after big open-files/attachment/timestamp blocks). Falls back
    to stripping known noise blocks from anywhere in the text.
    """
    if not text:
        return ""
    matches = list(_USER_QUERY_RE.finditer(text))
    if matches:
        return matches[-1].group(1).strip()
    if _is_injected_noise(text):
        return ""
    return _NOISE_BLOCK_RE.sub("", text).strip()


def _flatten_content(content: Any, *, skip_noise: bool = False) -> str:
    if isinstance(content, str):
        if skip_noise:
            return _extract_real_user_text(content)
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            piece = ""
            if isinstance(block, str):
                piece = block
            elif isinstance(block, dict):
                if block.get("type") == "text" or "text" in block:
                    piece = str(block.get("text") or "")
                elif block.get("type") in ("input_text", "output_text"):
                    piece = str(block.get("text") or "")
            if not piece:
                continue
            if skip_noise:
                piece = _extract_real_user_text(piece)
            if not piece:
                continue
            parts.append(piece)
        return "".join(parts)
    return ""


def _collect_text_and_images(content: Any, parts: list[str], images: list[int]) -> None:
    """Append visible text / image stubs from a message content value.

    Skips base64 / URL payloads so prompt estimates are not dominated by
    raw image bytes (gateway also stubs images at a fixed token cost).
    """
    if content is None:
        return
    if isinstance(content, str):
        if content:
            parts.append(content)
        return
    if isinstance(content, list):
        for block in content:
            _collect_text_and_images(block, parts, images)
        return
    if not isinstance(content, dict):
        return
    btype = content.get("type")
    if btype in {"image_url", "image", "input_image"}:
        images[0] += 1
        return
    if btype in {"text", "input_text", "output_text"} or "text" in content:
        t = content.get("text")
        if isinstance(t, str) and t:
            parts.append(t)
        return
    # Nested content (Responses message items, tool results, …)
    nested = content.get("content")
    if nested is not None:
        _collect_text_and_images(nested, parts, images)


def _collect_messages_prompt_parts(messages: list[Any], parts: list[str], images: list[int]) -> None:
    for msg in messages:
        if isinstance(msg, str):
            if msg:
                parts.append(msg)
            continue
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        if isinstance(role, str) and role:
            parts.append(role)
        _collect_text_and_images(msg.get("content"), parts, images)
        # Assistant tool_calls / Responses function_call names+args (text only)
        tool_calls = msg.get("tool_calls")
        if isinstance(tool_calls, list):
            for tc in tool_calls:
                if not isinstance(tc, dict):
                    continue
                fn = tc.get("function") if isinstance(tc.get("function"), dict) else tc
                if not isinstance(fn, dict):
                    continue
                name = fn.get("name")
                if isinstance(name, str) and name:
                    parts.append(name)
                args = fn.get("arguments")
                if isinstance(args, str) and args:
                    parts.append(args)
        name = msg.get("name")
        if isinstance(name, str) and name:
            parts.append(name)
        args = msg.get("arguments")
        if isinstance(args, str) and args:
            parts.append(args)
        # Some Responses items put text on the item itself
        if msg.get("type") in {"function_call", "function_call_output", "reasoning"}:
            _collect_text_and_images(msg.get("output"), parts, images)
            summary = msg.get("summary")
            if isinstance(summary, list):
                _collect_text_and_images(summary, parts, images)


def extract_prompt_text_for_estimate(body: bytes) -> tuple[str, int]:
    """Pull text (+ image count) from a Chat / Anthropic / Responses request body.

    Returns:
        ``(joined_text, image_count)``. Empty text when the body is not JSON.
    """
    if not body:
        return "", 0
    try:
        data = json.loads(body)
    except Exception:
        return "", 0
    if not isinstance(data, dict):
        return "", 0

    parts: list[str] = []
    images = [0]

    system = data.get("system")
    if isinstance(system, str) and system:
        parts.append(system)
    elif system is not None:
        _collect_text_and_images(system, parts, images)

    instructions = data.get("instructions")
    if isinstance(instructions, str) and instructions:
        parts.append(instructions)

    messages = data.get("messages")
    if isinstance(messages, list):
        _collect_messages_prompt_parts(messages, parts, images)

    inp = data.get("input")
    if isinstance(inp, str) and inp:
        parts.append(inp)
    elif isinstance(inp, list):
        _collect_messages_prompt_parts(inp, parts, images)

    tools = data.get("tools")
    if isinstance(tools, list) and tools:
        try:
            parts.append(json.dumps(tools, ensure_ascii=False, separators=(",", ":")))
        except (TypeError, ValueError):
            pass

    return "\n".join(parts), images[0]


def estimate_prompt_tokens_from_body(
    body: bytes,
    *,
    total_bytes: int | None = None,
) -> int:
    """tiktoken estimate of request prompt tokens (before upstream usage).

    Counts extracted text + a fixed stub per image. Falls back to byte÷4 only
    when no text/images could be recovered from the body.

    Args:
        body: Captured request bytes (may be head+tail after truncation).
        total_bytes: Full on-wire size when ``body`` was capped. Spliced
            head+tail can accidentally form valid JSON that undercounts the
            dropped middle; in that case prefer the wire-size estimate.
    """
    text, image_count = extract_prompt_text_for_estimate(body)
    n = estimate_tokens_from_text(text) if text else 0
    if image_count:
        n += image_count * _IMAGE_TOKEN_ESTIMATE
    if total_bytes is not None and total_bytes > len(body):
        return max(n, estimate_tokens_from_bytes(total_bytes))
    if n > 0:
        return n
    if body:
        return estimate_tokens_from_bytes(len(body))
    return 0


def _decode_json_string_fragment(raw: bytes) -> str:
    """Decode a JSON string fragment, including common escape sequences."""
    try:
        value = json.loads(b'"' + raw + b'"')
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError, TypeError):
        try:
            return raw.decode("utf-8", errors="replace")
        except Exception:
            return ""
    return value if isinstance(value, str) else ""


def extract_model(body: bytes) -> str:
    """Pull ``model`` from a request body, including truncated JSON.

    Full JSON parse is preferred. On failure (or a missing field), scan the
    leading bytes for ``"model": "…"`` — that field is near the front of
    OpenAI / Anthropic / Responses payloads, so a head-only capture still works.
    """
    if not body:
        return "unknown"
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError, TypeError):
        data = None
    if isinstance(data, dict):
        m = data.get("model")
        if isinstance(m, str) and m:
            return m
    match = _MODEL_FIELD_RE.search(body[:65_536])
    if match:
        decoded = _decode_json_string_fragment(match.group(1)).strip()
        if decoded:
            return decoded
    return "unknown"


def _last_user_text_from_messages(messages: list[Any]) -> str:
    for msg in reversed(messages):
        if not isinstance(msg, dict):
            continue
        if msg.get("role") != "user":
            continue
        text = _flatten_content(msg.get("content"), skip_noise=True).strip()
        if text:
            return text
    return ""


def _question_from_responses_input(inp: Any) -> str:
    """OpenAI Responses API ``input``: plain string or list of message items."""
    if isinstance(inp, str):
        return _extract_real_user_text(inp).strip()
    if not isinstance(inp, list):
        return ""
    # Prefer last user-role message (same idea as chat ``messages``).
    text = _last_user_text_from_messages(inp)
    if text:
        return text
    # Fallback: scan reversed items for any user/input_text content.
    for item in reversed(inp):
        if isinstance(item, str):
            piece = _extract_real_user_text(item).strip()
            if piece:
                return piece
            continue
        if not isinstance(item, dict):
            continue
        role = item.get("role")
        if role and role != "user":
            continue
        content = item.get("content", item.get("text"))
        piece = _flatten_content(content, skip_noise=True).strip()
        if piece:
            return piece
    return ""


def _question_from_partial_body(body: bytes, limit: int = PREVIEW_CHARS) -> str:
    """Best-effort question preview from truncated / non-JSON request bytes.

    Cursor / Claude Code embed the real prompt in ``<user_query>`` near the
    end of a large body; a rolling tail capture still contains that tag even
    when the middle of the JSON was dropped.
    """
    if not body:
        return ""
    try:
        text = body.decode("utf-8", errors="replace")
    except Exception:
        return ""
    matches = list(_USER_QUERY_RE.finditer(text))
    if matches:
        return truncate_preview(matches[-1].group(1).strip(), limit)
    open_match = _USER_QUERY_OPEN_RE.search(text)
    if open_match:
        piece = open_match.group(1).strip()
        if piece:
            return truncate_preview(piece, limit)
    return ""


def extract_question_preview(body: bytes, limit: int = PREVIEW_CHARS) -> str:
    """Last user text from ``messages`` or Responses ``input``.

    Falls back to scanning for ``<user_query>`` when the body is truncated
    mid-JSON (common for multi-MB Cursor agent requests).
    """
    if not body:
        return ""
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError, TypeError):
        return _question_from_partial_body(body, limit)
    if not isinstance(data, dict):
        return _question_from_partial_body(body, limit)
    messages = data.get("messages")
    if isinstance(messages, list):
        text = _last_user_text_from_messages(messages)
        if text:
            return truncate_preview(text, limit)
    # OpenAI Responses API uses ``input`` instead of ``messages``.
    text = _question_from_responses_input(data.get("input"))
    if text:
        return truncate_preview(text, limit)
    return _question_from_partial_body(body, limit)


def _answer_text_from_obj(data: dict[str, Any]) -> str:
    """Full assistant text from a parsed Chat / Anthropic / Responses JSON object."""
    # OpenAI chat completion
    choices = data.get("choices")
    if isinstance(choices, list) and choices:
        first = choices[0] if isinstance(choices[0], dict) else {}
        msg = first.get("message") if isinstance(first, dict) else None
        if isinstance(msg, dict):
            text = _flatten_content(msg.get("content"))
            if text:
                return text
        text = _flatten_content(first.get("text")) if isinstance(first, dict) else ""
        if text:
            return text
    # Anthropic messages API
    content = data.get("content")
    text = _flatten_content(content)
    if text:
        return text
    # OpenAI Responses API: output[] message items with output_text parts
    output = data.get("output")
    if isinstance(output, list):
        parts: list[str] = []
        for item in output:
            if not isinstance(item, dict):
                continue
            # Skip non-message items (reasoning, function_call, …)
            itype = item.get("type")
            if itype and itype not in ("message", "output_text"):
                continue
            if itype == "output_text":
                t = item.get("text")
                if isinstance(t, str) and t:
                    parts.append(t)
                continue
            piece = _flatten_content(item.get("content"))
            if piece:
                parts.append(piece)
        if parts:
            return "".join(parts)
    # Nested Responses wrapper: {"response": {...}}
    resp = data.get("response")
    if isinstance(resp, dict):
        return _answer_text_from_obj(resp)
    return ""


def extract_answer_text_from_json(buf: bytes) -> str:
    """Full assistant text from a non-stream JSON Chat / Anthropic / Responses body."""
    if not buf:
        return ""
    try:
        data = json.loads(buf)
    except Exception:
        return ""
    if not isinstance(data, dict):
        return ""
    return _answer_text_from_obj(data)


def _generated_output_text_from_obj_bytes(buf: bytes) -> str:
    """Parse a JSON body and return countable text/tool output."""
    try:
        data = json.loads(buf)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return ""
    return _generated_output_text_from_obj(data) if isinstance(data, dict) else ""


def extract_answer_preview_from_json(buf: bytes, limit: int = PREVIEW_CHARS) -> str:
    return truncate_preview(extract_answer_text_from_json(buf), limit)


def extract_error_preview(buf: bytes, limit: int = PREVIEW_CHARS) -> str:
    if not buf:
        return ""
    try:
        data = json.loads(buf)
    except Exception:
        return truncate_preview(buf.decode("utf-8", errors="replace"), limit)
    if not isinstance(data, dict):
        return ""
    err = data.get("error")
    if isinstance(err, dict):
        msg = err.get("message") or err.get("type") or ""
        return truncate_preview(str(msg), limit)
    if isinstance(err, str):
        return truncate_preview(err, limit)
    msg = data.get("message")
    if isinstance(msg, str):
        return truncate_preview(msg, limit)
    return ""


def _get_tiktoken_encoding() -> Any | None:
    """Lazy-load cl100k_base (same encoding the gateway uses for usage)."""
    global _tiktoken_encoding
    if _tiktoken_encoding is not None:
        return _tiktoken_encoding if _tiktoken_encoding else None
    try:
        import tiktoken

        _tiktoken_encoding = tiktoken.get_encoding("cl100k_base")
    except Exception:
        _tiktoken_encoding = False
        return None
    return _tiktoken_encoding


def estimate_tokens_from_bytes(n: int) -> int:
    """Last-resort estimate from opaque UTF-8 byte length (≈4 bytes/token).

    Prefer :func:`estimate_tokens_from_text` whenever the text is available —
    byte÷4 badly undercounts CJK.
    """
    return max(0, int(n) // 4)


def estimate_tokens_from_text(text: str) -> int:
    """Estimate tokens for visible text (live ⬇ before final usage).

    Uses tiktoken cl100k_base × Claude correction — same approach as the
    gateway's ``usage.completion_tokens`` / ``output_tokens``. Falls back to a
    CJK-aware character heuristic when tiktoken is unavailable.
    """
    if not text:
        return 0
    encoding = _get_tiktoken_encoding()
    if encoding is not None:
        try:
            return int(len(encoding.encode(text)) * _CLAUDE_CORRECTION_FACTOR)
        except Exception:
            pass
    # Fallback: CJK ≈ 1 token/char; other scripts ≈ 4 chars/token.
    cjk = 0
    other = 0
    for ch in text:
        o = ord(ch)
        if (
            0x4E00 <= o <= 0x9FFF
            or 0x3400 <= o <= 0x4DBF
            or 0xF900 <= o <= 0xFAFF
            or 0x3040 <= o <= 0x30FF
            or 0xAC00 <= o <= 0xD7AF
        ):
            cjk += 1
        elif not ch.isspace():
            other += 1
    return max(1, cjk + (other + 3) // 4)


def format_token_n(n: int) -> str:
    n = max(0, int(n))
    if n < 1000:
        return str(n)
    if n < 1_000_000:
        v = n / 1000.0
        s = f"{v:.1f}".rstrip("0").rstrip(".")
        return f"{s}k"
    v = n / 1_000_000.0
    s = f"{v:.1f}".rstrip("0").rstrip(".")
    return f"{s}M"


def parse_usage_tokens(usage: dict[str, Any] | None) -> tuple[int | None, int | None]:
    """Normalise Chat / Anthropic / Responses usage → (prompt, completion)."""
    if not usage or not isinstance(usage, dict):
        return None, None
    prompt = usage.get("prompt_tokens")
    if prompt is None:
        prompt = usage.get("input_tokens")
    completion = usage.get("completion_tokens")
    if completion is None:
        completion = usage.get("output_tokens")
    return (
        int(prompt) if prompt is not None else None,
        int(completion) if completion is not None else None,
    )


def _iter_usage_objects(obj: Any):
    """Yield usage dicts from Chat / Anthropic / Responses SSE or JSON bodies.

    - OpenAI Chat: top-level ``usage``
    - Anthropic: ``message.usage`` (message_start) and top-level ``usage``
      (message_delta)
    - OpenAI Responses: ``response.usage`` on ``response.completed`` (and
      top-level ``usage`` on non-stream JSON)
    """
    if not isinstance(obj, dict):
        return
    u = obj.get("usage")
    if isinstance(u, dict):
        yield u
    msg = obj.get("message")
    if isinstance(msg, dict):
        mu = msg.get("usage")
        if isinstance(mu, dict):
            yield mu
    resp = obj.get("response")
    if isinstance(resp, dict):
        ru = resp.get("usage")
        if isinstance(ru, dict):
            yield ru


def feed_sse_text(acc: list[str], chunk: bytes, cap: int = ANSWER_ACCUM_CAP) -> None:
    """Append assistant text deltas from an SSE chunk into ``acc`` (joined later).

    Stops once joined length reaches ``cap``. Mutates ``acc`` in place.
    """
    feed_sse_chunk(acc, chunk, cap=cap)


def _is_final_sse_usage_event(obj: dict[str, Any]) -> bool:
    """Return whether an SSE object carries terminal completion usage.

    Anthropic's ``message_start`` deliberately sends ``output_tokens: 0`` as
    an initial placeholder. That value must not disable the live estimate.
    """
    event_type = obj.get("type")
    if event_type in {"message_delta", "response.completed", "response.failed"}:
        return True
    choices = obj.get("choices")
    return bool(
        isinstance(choices, list)
        and choices
        and isinstance(choices[0], dict)
        and choices[0].get("finish_reason") is not None
    )


def feed_sse_chunk(
    acc: list[str],
    chunk: bytes,
    cap: int = ANSWER_ACCUM_CAP,
    *,
    output_acc: list[str] | None = None,
    tool_state: dict[str, dict[str, Any]] | None = None,
) -> tuple[int | None, int | None]:
    """Append visible and reasoning deltas and return latest usable token usage.

    The initial Anthropic ``output_tokens: 0`` is an API placeholder, rather
    than final usage. It is ignored until a terminal event so live ``⬇`` keeps
    increasing while the model is generating. Tool names and argument deltas
    are accumulated separately in ``output_acc`` and never enter previews.
    """
    prompt: int | None = None
    completion: int | None = None
    try:
        text = chunk.decode("utf-8", errors="replace")
    except Exception:
        return None, None
    capped = sum(len(p) for p in acc) >= cap
    output_capped = output_acc is not None and sum(len(p) for p in output_acc) >= cap
    tool_state = {} if tool_state is None else tool_state
    for raw in text.splitlines():
        line = raw.strip()
        if not line.startswith("data:"):
            continue
        payload = line[len("data:"):].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            obj = json.loads(payload)
        except Exception:
            continue
        if not isinstance(obj, dict):
            continue
        is_final_usage = _is_final_sse_usage_event(obj)
        for u in _iter_usage_objects(obj):
            p, c = parse_usage_tokens(u)
            if p is not None:
                prompt = p
            if c is not None and (c > 0 or is_final_usage):
                completion = c
        piece = _sse_delta_text(obj)
        if piece and not capped:
            acc.append(piece)
            if output_acc is not None and not output_capped:
                output_acc.append(piece)
                output_capped = sum(len(p) for p in output_acc) >= cap
            if sum(len(p) for p in acc) >= cap:
                capped = True
        if output_acc is not None and not output_capped:
            _append_sse_tool_output(obj, output_acc, tool_state)
            output_capped = sum(len(p) for p in output_acc) >= cap
    return prompt, completion


def feed_sse_buffered_chunk(
    acc: list[str],
    chunk: bytes,
    tail: bytes,
    cap: int = ANSWER_ACCUM_CAP,
    *,
    output_acc: list[str] | None = None,
    tool_state: dict[str, dict[str, Any]] | None = None,
) -> tuple[int | None, int | None, bytes]:
    """Parse complete SSE lines and retain an incomplete line for the next body.

    ASGI may split one ``data: {json}`` event between arbitrary response-body
    messages. Parsing such a fragment immediately discards it because it is
    not valid JSON, which previously lost both reasoning deltas and final usage.
    """
    combined = tail + chunk
    boundary = combined.rfind(b"\n")
    if boundary < 0:
        return None, None, combined[-_SSE_TAIL_CAP:]
    prompt, completion = feed_sse_chunk(
        acc,
        combined[: boundary + 1],
        cap,
        output_acc=output_acc,
        tool_state=tool_state,
    )
    return prompt, completion, combined[boundary + 1 :]


def _append_json_value(parts: list[str], value: Any) -> None:
    """Append a generated string or canonical JSON value to token input."""
    if isinstance(value, str):
        if value:
            parts.append(value)
    elif isinstance(value, (dict, list)) and value:
        try:
            parts.append(json.dumps(value, ensure_ascii=False))
        except (TypeError, ValueError):
            return


def _append_sse_tool_output(
    obj: dict[str, Any],
    parts: list[str],
    tool_state: dict[str, dict[str, Any]],
) -> None:
    """Accumulate generated tool names/arguments from supported SSE protocols."""
    choices = obj.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        delta = choices[0].get("delta")
        calls = delta.get("tool_calls") if isinstance(delta, dict) else None
        if isinstance(calls, list):
            for call in calls:
                if not isinstance(call, dict):
                    continue
                function = call.get("function")
                if isinstance(function, dict):
                    _append_json_value(parts, function.get("name"))
                    _append_json_value(parts, function.get("arguments"))

    event_type = obj.get("type")
    if event_type == "content_block_start":
        block = obj.get("content_block")
        if isinstance(block, dict) and block.get("type") in {"tool_use", "server_tool_use"}:
            _append_json_value(parts, block.get("name"))
            _append_json_value(parts, block.get("input"))
    elif event_type == "content_block_delta":
        delta = obj.get("delta")
        if isinstance(delta, dict) and delta.get("type") == "input_json_delta":
            _append_json_value(parts, delta.get("partial_json"))

    item = obj.get("item")
    if event_type in {"response.output_item.added", "response.output_item.done"} and isinstance(item, dict):
        if item.get("type") == "function_call":
            item_id = str(item.get("id") or item.get("call_id") or "")
            state = tool_state.setdefault(item_id, {"name": False, "arguments": ""})
            name = item.get("name")
            if isinstance(name, str) and name and not state["name"]:
                parts.append(name)
                state["name"] = True
            arguments = item.get("arguments")
            if isinstance(arguments, str) and arguments:
                parts.append(arguments)
                state["arguments"] += arguments
    elif event_type == "response.function_call_arguments.delta":
        item_id = str(obj.get("item_id") or "")
        state = tool_state.setdefault(item_id, {"name": False, "arguments": ""})
        delta = obj.get("delta")
        if isinstance(delta, str) and delta:
            parts.append(delta)
            state["arguments"] += delta
    elif event_type == "response.function_call_arguments.done":
        item_id = str(obj.get("item_id") or "")
        state = tool_state.setdefault(item_id, {"name": False, "arguments": ""})
        name = obj.get("name")
        if isinstance(name, str) and name and not state["name"]:
            parts.append(name)
            state["name"] = True
        arguments = obj.get("arguments")
        if isinstance(arguments, str) and arguments and not state["arguments"]:
            parts.append(arguments)
            state["arguments"] = arguments


def _generated_output_text_from_obj(data: dict[str, Any]) -> str:
    """Return countable generated text and tool data from a JSON response."""
    parts: list[str] = []
    answer = _answer_text_from_obj(data)
    if answer:
        parts.append(answer)

    choices = data.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        message = choices[0].get("message")
        calls = message.get("tool_calls") if isinstance(message, dict) else None
        if isinstance(calls, list):
            for call in calls:
                if not isinstance(call, dict):
                    continue
                function = call.get("function")
                if isinstance(function, dict):
                    _append_json_value(parts, function.get("name"))
                    _append_json_value(parts, function.get("arguments"))

    content = data.get("content")
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                _append_json_value(parts, block.get("name"))
                _append_json_value(parts, block.get("input"))

    output = data.get("output")
    if isinstance(output, list):
        for item in output:
            if isinstance(item, dict) and item.get("type") == "function_call":
                _append_json_value(parts, item.get("name"))
                _append_json_value(parts, item.get("arguments"))
    return "".join(parts)


def _sse_delta_text(obj: dict[str, Any]) -> str:
    """Extract answer or visible reasoning text from one parsed SSE event."""
    # OpenAI Chat: choices[0].delta.content / reasoning_content.
    choices = obj.get("choices")
    if isinstance(choices, list) and choices:
        first = choices[0]
        if isinstance(first, dict):
            delta = first.get("delta")
            if isinstance(delta, dict):
                for key in ("content", "reasoning_content", "reasoning"):
                    value = delta.get(key)
                    if isinstance(value, str):
                        return value
                    if key == "content":
                        flattened = _flatten_content(value)
                        if flattened:
                            return flattened
            # Some non-stream finals put message on the chunk.
            msg = first.get("message")
            if isinstance(msg, dict):
                return _flatten_content(msg.get("content"))

    event_type = obj.get("type")
    # Anthropic text and thinking deltas.
    if event_type == "content_block_delta":
        delta = obj.get("delta")
        if isinstance(delta, dict):
            for key in ("text", "thinking"):
                value = delta.get(key)
                if isinstance(value, str):
                    return value
    if event_type == "content_block_start":
        block = obj.get("content_block")
        if isinstance(block, dict):
            for key in ("text", "thinking"):
                value = block.get(key)
                if isinstance(value, str):
                    return value

    # OpenAI Responses API streaming, including reasoning deltas, uses top-level
    # string ``delta`` (for example ``response.output_text.delta``).
    delta = obj.get("delta")
    if isinstance(delta, str) and event_type in {
        "response.output_text.delta",
        "response.reasoning_summary_text.delta",
        "response.reasoning_text.delta",
    }:
        return delta
    if isinstance(delta, dict):
        for key in ("text", "content", "thinking", "reasoning"):
            value = delta.get(key)
            if isinstance(value, str):
                return value
            if key == "content":
                flattened = _flatten_content(value)
                if flattened:
                    return flattened
    return ""


def format_duration(seconds: float) -> str:
    if seconds < 0:
        seconds = 0.0
    if seconds < 60:
        if seconds < 10:
            return f"{seconds:.1f}s"
        return f"{int(seconds)}s"
    m = int(seconds) // 60
    s = int(seconds) % 60
    return f"{m}m{s:02d}s"


def short_model(model: str, limit: int = 18) -> str:
    model = (model or "unknown").strip() or "unknown"
    if len(model) <= limit:
        return model
    return model[: max(0, limit - 1)] + "…"


def _format_tokens_line(
    *,
    prompt_tokens: int,
    completion_tokens: int,
    completion_known: bool | None,
) -> str:
    """Format input and output tokens without confusing unknown with zero."""
    completion = (
        format_token_n(completion_tokens)
        if completion_known is True or (completion_known is None and completion_tokens > 0)
        else "—"
    )
    return f"⬆ {format_token_n(prompt_tokens)} · ⬇ {completion}"


def format_active_line(entry: ActiveRequest, *, now: float | None = None) -> str:
    """Multi-line in-progress item for tray menus.

    Line 1: phase / elapsed / short model
    Line 2: ⬆ prompt · ⬇ completion tokens (live while generating)
    Line 3: question preview
    """
    now = time.time() if now is None else now
    elapsed = format_duration(now - entry.started_at)
    phase = _PHASE_ZH.get(entry.phase, entry.phase)
    model = short_model(entry.model)
    q = entry.question_preview or "（无用户文本）"
    tokens = _format_tokens_line(
        prompt_tokens=entry.prompt_tokens,
        completion_tokens=entry.completion_tokens,
        completion_known=entry.completion_known,
    )
    return f"{phase} · {elapsed} · {model}\n{tokens}\n问: {q}"


def format_finished_active_line(entry: RecentRequest) -> str:
    """Multi-line title for a 进行中 slot that finished while the menu is open.

    Structural add/remove is deferred until the next full rebuild, so the row
    stays visible — only the phase label flips to 已完成 / 失败.
    """
    dur = format_duration(entry.duration_ms / 1000.0)
    model = short_model(entry.model)
    q = entry.question_preview or "（无用户文本）"
    status = "已完成" if entry.ok else "失败"
    tokens = _format_tokens_line(
        prompt_tokens=entry.prompt_tokens,
        completion_tokens=entry.completion_tokens,
        completion_known=entry.completion_known,
    )
    return f"{status} · {dur} · {model}\n{tokens}\n问: {q}"


def format_recent_line(entry: RecentRequest) -> str:
    """Multi-line recent item for tray menus.

    Line 1: time / status / duration / short model
    Line 2: ⬆ prompt · ⬇ completion tokens
    Line 3: question preview
    Line 4: answer preview (or error)
    """
    hhmm = time.strftime("%H:%M", time.localtime(entry.finished_at))
    mark = "✓" if entry.ok else "✗"
    dur = format_duration(entry.duration_ms / 1000.0)
    model = short_model(entry.model)
    q = entry.question_preview or "（无用户文本）"
    header = f"{hhmm} {mark} {dur} · {model}"
    tokens = _format_tokens_line(
        prompt_tokens=entry.prompt_tokens,
        completion_tokens=entry.completion_tokens,
        completion_known=entry.completion_known,
    )
    if entry.ok:
        a = entry.answer_preview or "…"
        return f"{header}\n{tokens}\n问: {q}\n答: {a}"
    err = entry.error_preview or "失败"
    return f"{header}\n{tokens}\n问: {q}\n错: {err}"


def activity_file(data_dir: Path | None = None) -> Path:
    if data_dir is None:
        from . import paths
        data_dir = paths.data_dir()
    return Path(data_dir) / _ACTIVITY_FILENAME


# --- persistence -------------------------------------------------------------

class RequestActivityStore:
    """Thread-safe active + recent ring buffer, persisted to a JSON file."""

    def __init__(self, path: Path, *, recent_limit: int = RECENT_LIMIT) -> None:
        self.path = Path(path)
        self.recent_limit = recent_limit
        self._lock = threading.Lock()
        self._active: dict[str, ActiveRequest] = {}
        self._recent: list[RecentRequest] = []
        self._dirty = False
        self._last_persist_mono = 0.0
        self._flush_timer: threading.Timer | None = None
        self._flush_generation = 0
        self._load_recent()

    def _load_recent(self) -> None:
        try:
            if not self.path.exists():
                return
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            recent = raw.get("recent") if isinstance(raw, dict) else None
            if not isinstance(recent, list):
                return
            loaded: list[RecentRequest] = []
            for item in recent:
                if not isinstance(item, dict):
                    continue
                try:
                    loaded.append(RecentRequest(
                        id=str(item.get("id") or ""),
                        started_at=float(item.get("started_at") or 0),
                        finished_at=float(item.get("finished_at") or 0),
                        model=str(item.get("model") or "unknown"),
                        path=str(item.get("path") or ""),
                        ok=bool(item.get("ok")),
                        duration_ms=int(item.get("duration_ms") or 0),
                        question_preview=str(item.get("question_preview") or ""),
                        answer_preview=str(item.get("answer_preview") or ""),
                        error_preview=str(item.get("error_preview") or ""),
                        prompt_tokens=max(0, int(item.get("prompt_tokens") or 0)),
                        completion_tokens=max(0, int(item.get("completion_tokens") or 0)),
                        completion_known=(
                            item.get("completion_known")
                            if isinstance(item.get("completion_known"), bool)
                            else None
                        ),
                    ))
                except Exception:
                    continue
            self._recent = loaded[: self.recent_limit]
        except Exception:
            logger.debug("request_activity: load recent failed", exc_info=True)

    def clear_active(self) -> None:
        with self._lock:
            self._cancel_flush_unlocked()
            self._active.clear()
            self._dirty = False
            self._persist_unlocked()

    def begin(
        self,
        *,
        model: str,
        path: str,
        question_preview: str,
        started_at: float | None = None,
        prompt_tokens: int = 0,
    ) -> str:
        rid = uuid.uuid4().hex[:12]
        entry = ActiveRequest(
            id=rid,
            started_at=time.time() if started_at is None else started_at,
            model=model or "unknown",
            path=path,
            phase=_PHASE_WAITING,
            question_preview=truncate_preview(question_preview),
            prompt_tokens=max(0, int(prompt_tokens)),
            completion_tokens=0,
            completion_known=False,
        )
        with self._lock:
            self._cancel_flush_unlocked()
            self._active[rid] = entry
            self._dirty = False
            self._persist_unlocked()
        return rid

    def set_phase(self, rid: str, phase: str) -> None:
        with self._lock:
            entry = self._active.get(rid)
            if entry is None or entry.phase == phase:
                return
            entry.phase = phase
            self._cancel_flush_unlocked()
            self._dirty = False
            self._persist_unlocked()

    def update_tokens(
        self,
        rid: str,
        *,
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
        completion_known: bool | None = None,
        force: bool = False,
    ) -> None:
        """Update live ⬆/⬇ token counters; disk writes are throttled."""
        with self._lock:
            entry = self._active.get(rid)
            if entry is None:
                return
            changed = False
            if prompt_tokens is not None:
                p = max(0, int(prompt_tokens))
                if p != entry.prompt_tokens:
                    entry.prompt_tokens = p
                    changed = True
            if completion_tokens is not None:
                c = max(0, int(completion_tokens))
                if c != entry.completion_tokens:
                    entry.completion_tokens = c
                    changed = True
            if completion_known is not None and completion_known != entry.completion_known:
                entry.completion_known = completion_known
                changed = True
            if changed:
                self._dirty = True
            self._flush_if_due_unlocked(force=force)

    def finish(
        self,
        rid: str,
        *,
        ok: bool,
        answer_preview: str = "",
        error_preview: str = "",
        finished_at: float | None = None,
    ) -> None:
        with self._lock:
            self._cancel_flush_unlocked()
            entry = self._active.pop(rid, None)
            if entry is None:
                self._dirty = False
                self._persist_unlocked()
                return
            end = time.time() if finished_at is None else finished_at
            recent = RecentRequest(
                id=entry.id,
                started_at=entry.started_at,
                finished_at=end,
                model=entry.model,
                path=entry.path,
                ok=ok,
                duration_ms=max(0, int((end - entry.started_at) * 1000)),
                question_preview=entry.question_preview,
                answer_preview=truncate_preview(answer_preview),
                error_preview=truncate_preview(error_preview),
                prompt_tokens=entry.prompt_tokens,
                completion_tokens=entry.completion_tokens,
                completion_known=entry.completion_known,
            )
            self._recent.insert(0, recent)
            del self._recent[self.recent_limit:]
            self._dirty = False
            self._persist_unlocked()

    def snapshot(self, *, now: float | None = None) -> ActivitySnapshot:
        now = time.time() if now is None else now
        with self._lock:
            active = [
                a for a in self._active.values()
                if (now - a.started_at) <= _STALE_ACTIVE_SECONDS
            ]
            active.sort(key=lambda a: a.started_at, reverse=True)
            return ActivitySnapshot(active=list(active), recent=list(self._recent))

    def _flush_if_due_unlocked(self, *, force: bool = False) -> None:
        if not self._dirty and not force:
            return
        now = time.monotonic()
        remaining = _TOKEN_PERSIST_INTERVAL - (now - self._last_persist_mono)
        if not force and remaining > 0:
            self._schedule_flush_unlocked(remaining)
            return
        self._cancel_flush_unlocked()
        if self._persist_unlocked():
            self._dirty = False

    def _schedule_flush_unlocked(self, delay: float) -> None:
        """Coalesce throttled updates into one cancellable delayed write."""
        if self._flush_timer is not None:
            return
        generation = self._flush_generation

        def flush() -> None:
            with self._lock:
                if generation != self._flush_generation:
                    return
                self._flush_timer = None
                if self._dirty and self._persist_unlocked():
                    self._dirty = False

        timer = threading.Timer(max(0.0, delay), flush)
        timer.daemon = True
        self._flush_timer = timer
        timer.start()

    def _cancel_flush_unlocked(self) -> None:
        """Invalidate any delayed writer before an immediate state transition."""
        self._flush_generation += 1
        timer = self._flush_timer
        self._flush_timer = None
        if timer is not None:
            timer.cancel()

    def _persist_unlocked(self) -> bool:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            snap = ActivitySnapshot(
                active=sorted(self._active.values(), key=lambda a: a.started_at, reverse=True),
                recent=list(self._recent),
            )
            payload = json.dumps(snap.to_dict(), ensure_ascii=False, indent=None)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(payload + "\n", encoding="utf-8")
            tmp.replace(self.path)
            self._last_persist_mono = time.monotonic()
            return True
        except Exception:
            logger.debug("request_activity: persist failed", exc_info=True)
            return False


def load_snapshot(path: Path | None = None, *, now: float | None = None) -> ActivitySnapshot:
    """Read the activity file for the tray (best-effort, never raises)."""
    path = activity_file() if path is None else Path(path)
    now = time.time() if now is None else now
    try:
        if not path.exists():
            return ActivitySnapshot()
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        logger.debug("request_activity: tray load failed", exc_info=True)
        return ActivitySnapshot()
    if not isinstance(raw, dict):
        return ActivitySnapshot()

    active: list[ActiveRequest] = []
    for item in raw.get("active") or []:
        if not isinstance(item, dict):
            continue
        try:
            started = float(item.get("started_at") or 0)
            if (now - started) > _STALE_ACTIVE_SECONDS:
                continue
            active.append(ActiveRequest(
                id=str(item.get("id") or ""),
                started_at=started,
                model=str(item.get("model") or "unknown"),
                path=str(item.get("path") or ""),
                phase=str(item.get("phase") or _PHASE_WAITING),
                question_preview=str(item.get("question_preview") or ""),
                prompt_tokens=max(0, int(item.get("prompt_tokens") or 0)),
                completion_tokens=max(0, int(item.get("completion_tokens") or 0)),
                completion_known=(
                    item.get("completion_known")
                    if isinstance(item.get("completion_known"), bool)
                    else None
                ),
            ))
        except Exception:
            continue
    active.sort(key=lambda a: a.started_at, reverse=True)

    recent: list[RecentRequest] = []
    for item in raw.get("recent") or []:
        if not isinstance(item, dict):
            continue
        try:
            recent.append(RecentRequest(
                id=str(item.get("id") or ""),
                started_at=float(item.get("started_at") or 0),
                finished_at=float(item.get("finished_at") or 0),
                model=str(item.get("model") or "unknown"),
                path=str(item.get("path") or ""),
                ok=bool(item.get("ok")),
                duration_ms=int(item.get("duration_ms") or 0),
                question_preview=str(item.get("question_preview") or ""),
                answer_preview=str(item.get("answer_preview") or ""),
                error_preview=str(item.get("error_preview") or ""),
                prompt_tokens=max(0, int(item.get("prompt_tokens") or 0)),
                completion_tokens=max(0, int(item.get("completion_tokens") or 0)),
                completion_known=(
                    item.get("completion_known")
                    if isinstance(item.get("completion_known"), bool)
                    else None
                ),
            ))
        except Exception:
            continue
    return ActivitySnapshot(active=active, recent=recent[:RECENT_LIMIT])


# --- ASGI middleware ---------------------------------------------------------

@dataclass
class _LiveState:
    rid: str
    status: int = 0
    is_sse: bool = False
    is_json: bool = False
    completed: bool = False
    answer_parts: list[str] = field(default_factory=list)
    output_parts: list[str] = field(default_factory=list)
    tool_output_state: dict[str, dict[str, Any]] = field(default_factory=dict)
    sse_tail: bytes = b""
    json_buf: bytearray = field(default_factory=bytearray)
    phase_set: bool = False
    response_bytes: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    usage_prompt: bool = False
    usage_completion: bool = False
    completion_known: bool = False


class RequestActivityMiddleware:
    """Pure-ASGI side-channel; never buffers the response for the client."""

    def __init__(self, app: Any, store: RequestActivityStore) -> None:
        self.app = app
        self.store = store

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        method = scope.get("method", "")
        path = scope.get("path", "")
        if method != "POST" or path not in COLLECT_PATHS:
            await self.app(scope, receive, send)
            return
        await self._handle(scope, receive, send, path)

    async def _handle(self, scope: dict, receive: Any, send: Any, path: str) -> None:
        captured: list[dict] = []
        body_buf = _RequestBodyBuffer()
        try:
            while True:
                message = await receive()
                captured.append(message)
                if message.get("type") == "http.request":
                    chunk = message.get("body", b"") or b""
                    body_buf.extend(chunk)
                    if not message.get("more_body", False):
                        break
                elif message.get("type") == "http.disconnect":
                    break
        except Exception:
            logger.debug("request_activity: request capture failed", exc_info=True)

        model = "unknown"
        question = ""
        prompt_est = 0
        try:
            raw = body_buf.preview_bytes()
            model = extract_model(raw)
            question = extract_question_preview(raw)
            prompt_est = estimate_prompt_tokens_from_body(
                raw,
                total_bytes=body_buf.total if body_buf.truncated else None,
            )
        except Exception:
            logger.debug("request_activity: preview extract failed", exc_info=True)

        rid = ""
        try:
            rid = self.store.begin(
                model=model,
                path=path,
                question_preview=question,
                prompt_tokens=prompt_est,
            )
        except Exception:
            logger.debug("request_activity: begin failed", exc_info=True)

        replay_index = 0

        async def replay_receive() -> dict:
            nonlocal replay_index
            if replay_index < len(captured):
                msg = captured[replay_index]
                replay_index += 1
                return msg
            return await receive()

        state = _LiveState(rid=rid, prompt_tokens=prompt_est)

        async def wrapped_send(message: dict) -> None:
            try:
                self._inspect(message, state)
            except Exception:
                logger.debug("request_activity: inspect failed", exc_info=True)
            await send(message)

        try:
            await self.app(scope, replay_receive, wrapped_send)
        except Exception:
            self._finalise(state, ok=False)
            raise
        else:
            status_ok = 200 <= int(state.status or 0) < 300
            self._finalise(state, ok=bool(status_ok and state.completed))

    def _sync_tokens(self, state: _LiveState, *, force: bool = False) -> None:
        if not state.rid:
            return
        self.store.update_tokens(
            state.rid,
            prompt_tokens=state.prompt_tokens,
            completion_tokens=state.completion_tokens,
            completion_known=state.completion_known,
            force=force,
        )

    def _inspect(self, message: dict, state: _LiveState) -> None:
        if not state.rid:
            return
        mtype = message.get("type")
        if mtype == "http.response.start":
            state.status = int(message.get("status", 0) or 0)
            ctype = ""
            for k, v in message.get("headers", []) or []:
                try:
                    if k.lower() == b"content-type":
                        ctype = v.decode("latin-1").lower()
                        break
                except Exception:
                    continue
            state.is_sse = "text/event-stream" in ctype
            state.is_json = "application/json" in ctype
            phase = _PHASE_STREAMING if state.is_sse else _PHASE_RESPONDING
            if not state.phase_set:
                state.phase_set = True
                self.store.set_phase(state.rid, phase)
        elif mtype == "http.response.body":
            chunk = message.get("body", b"") or b""
            if chunk:
                state.response_bytes += len(chunk)
            if state.is_sse and chunk:
                prior_parts = len(state.answer_parts)
                prompt_u, completion_u, state.sse_tail = feed_sse_buffered_chunk(
                    state.answer_parts,
                    chunk,
                    state.sse_tail,
                    output_acc=state.output_parts,
                    tool_state=state.tool_output_state,
                )
                if prompt_u is not None:
                    state.prompt_tokens = prompt_u
                    state.usage_prompt = True
                if completion_u is not None:
                    state.completion_tokens = completion_u
                    state.usage_completion = True
                    state.completion_known = True
                elif (
                    (len(state.answer_parts) > prior_parts or state.output_parts)
                    and not state.usage_completion
                ):
                    # Climb with visible/reasoning text while the provider is still
                    # streaming. Header-only and metadata-only SSE frames remain
                    # unknown instead of being misreported as zero output tokens.
                    state.completion_tokens = estimate_tokens_from_text(
                        "".join(state.output_parts)
                    )
                    state.completion_known = True
                self._sync_tokens(state)
            elif state.is_json and chunk:
                if len(state.json_buf) < ANSWER_ACCUM_CAP * 8:
                    state.json_buf.extend(chunk)
                if not state.usage_completion:
                    output_text = _generated_output_text_from_obj_bytes(bytes(state.json_buf))
                    if output_text:
                        state.completion_tokens = estimate_tokens_from_text(output_text)
                        state.completion_known = True
                    self._sync_tokens(state)
            if not message.get("more_body", False):
                state.completed = True
                if state.is_sse and state.sse_tail:
                    prompt_u, completion_u = feed_sse_chunk(
                        state.answer_parts,
                        state.sse_tail,
                        output_acc=state.output_parts,
                        tool_state=state.tool_output_state,
                    )
                    state.sse_tail = b""
                    if prompt_u is not None:
                        state.prompt_tokens = prompt_u
                        state.usage_prompt = True
                    if completion_u is not None:
                        state.completion_tokens = completion_u
                        state.usage_completion = True
                        state.completion_known = True
                if state.is_json and state.json_buf:
                    try:
                        data = json.loads(bytes(state.json_buf))
                    except Exception:
                        data = None
                    if isinstance(data, dict):
                        for u in _iter_usage_objects(data):
                            p, c = parse_usage_tokens(u)
                            if p is not None:
                                state.prompt_tokens = p
                                state.usage_prompt = True
                            if c is not None:
                                state.completion_tokens = c
                                state.usage_completion = True
                                state.completion_known = True
                    if not state.usage_completion:
                        output_text = _generated_output_text_from_obj_bytes(
                            bytes(state.json_buf)
                        )
                        if output_text:
                            state.completion_tokens = estimate_tokens_from_text(
                                output_text
                            )
                            state.completion_known = True
                self._sync_tokens(state, force=True)

    def _finalise(self, state: _LiveState, *, ok: bool) -> None:
        if not state.rid:
            return
        try:
            answer = ""
            error = ""
            if state.is_sse:
                answer = truncate_preview("".join(state.answer_parts))
            elif state.is_json:
                raw = bytes(state.json_buf)
                if ok:
                    answer = extract_answer_preview_from_json(raw)
                else:
                    error = extract_error_preview(raw)
            if not ok and not error and state.status:
                error = f"HTTP {state.status}"
            self._sync_tokens(state, force=True)
            self.store.finish(
                state.rid,
                ok=ok,
                answer_preview=answer,
                error_preview=error,
            )
        except Exception:
            logger.debug("request_activity: finalise failed", exc_info=True)
            try:
                self.store.finish(state.rid, ok=False, error_preview="记录失败")
            except Exception:
                pass


def wrap_app(app: Any, *, data_dir: Path | None = None) -> Any:
    """Always wrap ``app`` so the tray can show activity even without telemetry."""
    path = activity_file(data_dir)
    store = RequestActivityStore(path)
    store.clear_active()
    logger.info("request activity tracking enabled ({})", path)
    return RequestActivityMiddleware(app, store)
