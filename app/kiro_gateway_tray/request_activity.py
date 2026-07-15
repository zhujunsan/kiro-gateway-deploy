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
ANSWER_ACCUM_CAP = 200  # stop accumulating assistant text once we have enough
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
_REQ_BODY_CAP = 4_000_000
_STALE_ACTIVE_SECONDS = 600  # drop orphaned in-flight rows after 10 minutes
_ACTIVITY_FILENAME = "request_activity.json"
_TOKEN_PERSIST_INTERVAL = 0.45  # throttle disk writes while tokens climb

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


@dataclass
class ActivitySnapshot:
    active: list[ActiveRequest] = field(default_factory=list)
    recent: list[RecentRequest] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "active": [asdict(a) for a in self.active],
            "recent": [asdict(r) for r in self.recent],
        }


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


def extract_model(body: bytes) -> str:
    if not body:
        return "unknown"
    try:
        data = json.loads(body)
    except Exception:
        return "unknown"
    if isinstance(data, dict):
        m = data.get("model")
        if isinstance(m, str) and m:
            return m
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


def extract_question_preview(body: bytes, limit: int = PREVIEW_CHARS) -> str:
    """Last user text from ``messages`` or Responses ``input``."""
    if not body:
        return ""
    try:
        data = json.loads(body)
    except Exception:
        return ""
    if not isinstance(data, dict):
        return ""
    messages = data.get("messages")
    if isinstance(messages, list):
        text = _last_user_text_from_messages(messages)
        if text:
            return truncate_preview(text, limit)
    # OpenAI Responses API uses ``input`` instead of ``messages``.
    text = _question_from_responses_input(data.get("input"))
    if text:
        return truncate_preview(text, limit)
    return ""


def extract_answer_preview_from_json(buf: bytes, limit: int = PREVIEW_CHARS) -> str:
    if not buf:
        return ""
    try:
        data = json.loads(buf)
    except Exception:
        return ""
    if not isinstance(data, dict):
        return ""
    # OpenAI chat completion
    choices = data.get("choices")
    if isinstance(choices, list) and choices:
        first = choices[0] if isinstance(choices[0], dict) else {}
        msg = first.get("message") if isinstance(first, dict) else None
        if isinstance(msg, dict):
            text = _flatten_content(msg.get("content"))
            if text:
                return truncate_preview(text, limit)
        text = _flatten_content(first.get("text")) if isinstance(first, dict) else ""
        if text:
            return truncate_preview(text, limit)
    # Anthropic messages API
    content = data.get("content")
    text = _flatten_content(content)
    if text:
        return truncate_preview(text, limit)
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
            return truncate_preview("".join(parts), limit)
    return ""


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


def estimate_tokens_from_bytes(n: int) -> int:
    """Rough token estimate from UTF-8 byte length (≈4 bytes/token)."""
    return max(0, int(n) // 4)


def estimate_tokens_from_text(text: str) -> int:
    if not text:
        return 0
    return estimate_tokens_from_bytes(len(text.encode("utf-8")))


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


def feed_sse_text(acc: list[str], chunk: bytes, cap: int = ANSWER_ACCUM_CAP) -> None:
    """Append assistant text deltas from an SSE chunk into ``acc`` (joined later).

    Stops once joined length reaches ``cap``. Mutates ``acc`` in place.
    """
    feed_sse_chunk(acc, chunk, cap=cap)


def feed_sse_chunk(
    acc: list[str],
    chunk: bytes,
    cap: int = ANSWER_ACCUM_CAP,
) -> tuple[int | None, int | None]:
    """Append text deltas and return the latest ``(prompt, completion)`` usage.

    Either side may be ``None`` when that field was not present in this chunk.
    """
    prompt: int | None = None
    completion: int | None = None
    try:
        text = chunk.decode("utf-8", errors="replace")
    except Exception:
        return None, None
    capped = sum(len(p) for p in acc) >= cap
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
        for u in _iter_usage_objects(obj):
            p, c = parse_usage_tokens(u)
            if p is not None:
                prompt = p
            if c is not None:
                completion = c
        if capped:
            continue
        piece = _sse_delta_text(obj)
        if not piece:
            continue
        acc.append(piece)
        if sum(len(p) for p in acc) >= cap:
            capped = True
    return prompt, completion


def _sse_delta_text(obj: dict[str, Any]) -> str:
    # OpenAI: choices[0].delta.content
    choices = obj.get("choices")
    if isinstance(choices, list) and choices:
        first = choices[0]
        if isinstance(first, dict):
            delta = first.get("delta")
            if isinstance(delta, dict):
                c = delta.get("content")
                if isinstance(c, str):
                    return c
            # some non-stream finals put message on the chunk
            msg = first.get("message")
            if isinstance(msg, dict):
                return _flatten_content(msg.get("content"))
    # Anthropic streaming
    otype = obj.get("type")
    if otype == "content_block_delta":
        delta = obj.get("delta")
        if isinstance(delta, dict):
            t = delta.get("text")
            if isinstance(t, str):
                return t
    if otype == "content_block_start":
        block = obj.get("content_block")
        if isinstance(block, dict) and block.get("type") == "text":
            t = block.get("text")
            if isinstance(t, str):
                return t
    # OpenAI Responses API streaming: top-level string delta
    # e.g. {"type":"response.output_text.delta","delta":"你好"}
    delta = obj.get("delta")
    if isinstance(delta, str):
        return delta
    if isinstance(delta, dict):
        t = delta.get("text") or delta.get("content")
        if isinstance(t, str):
            return t
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


def _format_tokens_line(*, prompt_tokens: int, completion_tokens: int) -> str:
    return (
        f"⬆ {format_token_n(prompt_tokens)} · "
        f"⬇ {format_token_n(completion_tokens)}"
    )


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
                    ))
                except Exception:
                    continue
            self._recent = loaded[: self.recent_limit]
        except Exception:
            logger.debug("request_activity: load recent failed", exc_info=True)

    def clear_active(self) -> None:
        with self._lock:
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
        )
        with self._lock:
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
            self._dirty = False
            self._persist_unlocked()

    def update_tokens(
        self,
        rid: str,
        *,
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
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
        if not force and (now - self._last_persist_mono) < _TOKEN_PERSIST_INTERVAL:
            return
        self._persist_unlocked()
        self._dirty = False
        self._last_persist_mono = now

    def _persist_unlocked(self) -> None:
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
        except Exception:
            logger.debug("request_activity: persist failed", exc_info=True)


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
    json_buf: bytearray = field(default_factory=bytearray)
    phase_set: bool = False
    response_bytes: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    usage_prompt: bool = False
    usage_completion: bool = False


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
        body = bytearray()
        try:
            while True:
                message = await receive()
                captured.append(message)
                if message.get("type") == "http.request":
                    chunk = message.get("body", b"") or b""
                    if len(body) < _REQ_BODY_CAP:
                        body.extend(chunk)
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
            raw = bytes(body)
            model = extract_model(raw)
            question = extract_question_preview(raw)
            prompt_est = estimate_tokens_from_bytes(len(raw))
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
                prompt_u, completion_u = feed_sse_chunk(state.answer_parts, chunk)
                if prompt_u is not None:
                    state.prompt_tokens = prompt_u
                    state.usage_prompt = True
                if completion_u is not None:
                    state.completion_tokens = completion_u
                    state.usage_completion = True
                if not state.usage_completion:
                    # Climb with streamed text so the menu shows live ⬇ progress
                    # before the provider emits a final usage event.
                    est = estimate_tokens_from_text("".join(state.answer_parts))
                    if est == 0 and state.response_bytes:
                        est = estimate_tokens_from_bytes(state.response_bytes)
                    state.completion_tokens = est
                self._sync_tokens(state)
            elif state.is_json and chunk:
                if len(state.json_buf) < ANSWER_ACCUM_CAP * 8:
                    state.json_buf.extend(chunk)
                if not state.usage_completion:
                    state.completion_tokens = estimate_tokens_from_bytes(
                        state.response_bytes
                    )
                    self._sync_tokens(state)
            if not message.get("more_body", False):
                state.completed = True
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
