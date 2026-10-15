"""Gateway-child side of the audit log: copy request and response bytes to disk.

The tray process does dedup, compression and upload. This module only decides
whether capture is on (``audit/control.json``, written by the tray) and appends
one raw JSON line per collected request under ``audit/spool/``. A segment is
renamed to ``*.ready`` every ``rotate_seconds`` and on shutdown; the tray never
reads a file this process still has open.

Capture must never change a response or raise into the request path.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Any, Callable

from . import paths
from .audit_payload import SCHEMA_VERSION, redact_headers
from .log import logger

COLLECT_PATHS = frozenset({
    "/v1/chat/completions",
    "/v1/messages",
    "/v1/responses",
})

ROTATE_SECONDS = 60
CONTROL_POLL_SECONDS = 5.0
QUEUE_MAX_BYTES = 256 * 1024 * 1024
RESP_MAX_BYTES = 20 * 1024 * 1024
DEFAULT_MAX_SPOOL_BYTES = 1024 * 1024 * 1024

_MODES = frozenset({"off", "best_effort"})


def _chmod(path: Path, mode: int) -> None:
    if os.name != "posix":
        return
    try:
        os.chmod(path, mode)
    except OSError:
        logger.debug("audit capture: chmod {} failed", path, exc_info=True)


class ControlReader:
    """Tray-written switch. Missing or unreadable files mean capture is off.

    The file is stat'd at most once per ``CONTROL_POLL_SECONDS``. A changed
    mtime is re-read; anything that is not ``off`` or ``best_effort`` is off.
    """

    def __init__(self, path: Path, clock: Callable[[], float]) -> None:
        self.path = path
        self.clock = clock
        self._checked_at = -1.0e18
        self._mtime: int | None = None
        self._mode = "off"
        self._max_spool_bytes = DEFAULT_MAX_SPOOL_BYTES

    def snapshot(self) -> tuple[str, int]:
        """Return ``(mode, max_spool_bytes)``."""
        now = self.clock()
        if now - self._checked_at < CONTROL_POLL_SECONDS:
            return self._mode, self._max_spool_bytes
        self._checked_at = now
        try:
            mtime = self.path.stat().st_mtime_ns
        except OSError:
            self._mode = "off"
            self._mtime = None
            return self._mode, self._max_spool_bytes
        if self._mtime is not None and mtime == self._mtime:
            return self._mode, self._max_spool_bytes
        self._mtime = mtime
        self._mode, self._max_spool_bytes = self._read()
        return self._mode, self._max_spool_bytes

    def _read(self) -> tuple[str, int]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return "off", self._max_spool_bytes
        if not isinstance(data, dict):
            return "off", self._max_spool_bytes
        mode = data.get("mode")
        if mode not in _MODES:
            mode = "off"
        limit = data.get("max_spool_bytes", self._max_spool_bytes)
        if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
            limit = self._max_spool_bytes
        return mode, limit


class SpoolWriter:
    """Append-only spool with a byte-capped queue and periodic rotation.

    The background thread is optional so tests can call :meth:`drain` themselves.
    """

    def __init__(
        self,
        spool_dir: Path,
        control: ControlReader,
        *,
        clock: Callable[[], float],
        rotate_seconds: float = ROTATE_SECONDS,
        queue_max_bytes: int = QUEUE_MAX_BYTES,
    ) -> None:
        self.spool_dir = spool_dir
        self.control = control
        self.clock = clock
        self.rotate_seconds = rotate_seconds
        self.queue_max_bytes = queue_max_bytes
        self.dropped_records = 0
        self.paused = False
        self._queue: deque[tuple[int, str]] = deque()
        self._queued_bytes = 0
        self._lock = threading.Lock()
        self._io = threading.RLock()
        self._file: Any = None
        self._segment_path: Path | None = None
        self._segment_started = 0.0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.spool_dir.mkdir(parents=True, exist_ok=True)
        _chmod(self.spool_dir, 0o700)
        self._adopt_leftovers()
        self._check_pause()

    def start(self) -> None:
        """Start the writer thread. A second call while it is alive does nothing."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="audit-capture", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        """Flush the queue, close the open segment as ``.ready``, and join."""
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)
        if thread is None or not thread.is_alive():
            self.drain()
            with self._io:
                self._close_segment()

    def accepting(self) -> bool:
        """True when this request should be copied.

        A paused spool (tray is not consuming) counts the skipped request and
        returns False. ``off`` does not count: nothing was supposed to be kept.
        """
        mode, _limit = self.control.snapshot()
        if mode != "best_effort":
            return False
        if self.paused:
            with self._lock:
                self.dropped_records += 1
            return False
        return True

    def enqueue(self, record: dict) -> bool:
        """Queue one record. Drops it (and counts) when paused or over the byte cap.

        Never raises and never blocks on disk.
        """
        try:
            line = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
            size = len(line.encode("utf-8"))
        except (TypeError, ValueError):
            with self._lock:
                self.dropped_records += 1
            return False
        with self._lock:
            if self.paused or self._queued_bytes + size > self.queue_max_bytes:
                self.dropped_records += 1
                return False
            self._queue.append((size, line))
            self._queued_bytes += size
        return True

    def drain(self) -> None:
        """Write queued lines and rotate when the segment is due. Swallows IO errors."""
        self._check_pause()
        while True:
            with self._lock:
                if not self._queue:
                    break
                size, line = self._queue.popleft()
                self._queued_bytes -= size
            try:
                with self._io:
                    self._ensure_segment()
                    assert self._file is not None
                    self._file.write(line)
                    self._file.flush()
            except Exception:
                with self._lock:
                    self.dropped_records += 1
                logger.debug("audit capture: write failed", exc_info=True)
        with self._io:
            self._maybe_rotate()

    def _loop(self) -> None:
        while not self._stop.wait(0.05):
            try:
                self.drain()
            except Exception:
                logger.debug("audit capture: drain failed", exc_info=True)
        try:
            self.drain()
            with self._io:
                self._close_segment()
        except Exception:
            logger.debug("audit capture: shutdown flush failed", exc_info=True)

    def _adopt_leftovers(self) -> None:
        """A previous child may have died with a segment still named ``.jsonl``."""
        for path in sorted(self.spool_dir.glob("*.jsonl")):
            ready = path.with_suffix(".ready")
            try:
                os.replace(path, ready)
                _chmod(ready, 0o600)
            except OSError:
                logger.debug("audit capture: could not adopt {}", path, exc_info=True)

    def _spool_bytes(self) -> int:
        total = 0
        try:
            for path in self.spool_dir.iterdir():
                if path.is_file():
                    total += path.stat().st_size
        except OSError:
            return total
        return total

    def _check_pause(self) -> None:
        _mode, limit = self.control.snapshot()
        was = self.paused
        paused = self._spool_bytes() > limit
        with self._lock:
            self.paused = paused
        if paused and not was:
            logger.warning(
                "audit capture paused: spool {} bytes exceeds {}",
                self._spool_bytes(), limit,
            )

    def _segment_name(self) -> str:
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(self.clock()))
        base = f"{stamp}-{os.getpid()}"
        for seq in range(1000):
            name = base if seq == 0 else f"{base}-{seq}"
            if not (self.spool_dir / f"{name}.jsonl").exists() and not (
                self.spool_dir / f"{name}.ready"
            ).exists():
                return name
        return f"{base}-{uuid.uuid4().hex[:8]}"

    def _ensure_segment(self) -> None:
        if self._file is not None:
            return
        self.spool_dir.mkdir(parents=True, exist_ok=True)
        _chmod(self.spool_dir, 0o700)
        path = self.spool_dir / f"{self._segment_name()}.jsonl"
        handle = open(path, "a", encoding="utf-8")
        _chmod(path, 0o600)
        self._file = handle
        self._segment_path = path
        self._segment_started = self.clock()

    def _maybe_rotate(self) -> None:
        if self._file is None:
            return
        if self.clock() - self._segment_started >= self.rotate_seconds:
            self._close_segment()
            self._check_pause()

    def _close_segment(self) -> None:
        if self._file is None or self._segment_path is None:
            return
        path = self._segment_path
        try:
            self._file.flush()
            self._file.close()
        finally:
            self._file = None
            self._segment_path = None
        ready = path.with_suffix(".ready")
        os.replace(path, ready)
        _chmod(ready, 0o600)


def _header_map(scope: dict) -> dict[str, str]:
    raw = scope.get("headers") or []
    try:
        return redact_headers(list(raw))
    except Exception:
        logger.debug("audit capture: header redact failed", exc_info=True)
        return {}


def _via_and_ip(headers: dict[str, str]) -> tuple[str, str | None]:
    if "cf-ray" in headers:
        return "tunnel", headers.get("cf-connecting-ip") or None
    return "local", None


class _RespState:
    def __init__(self, cap: int) -> None:
        self.cap = cap
        self.status = 0
        self.headers: list[tuple[bytes, bytes]] = []
        self.buf = bytearray()
        self.total = 0
        self.truncated = False
        self.completed = False


class AuditCaptureMiddleware:
    """Pure-ASGI capture. Forwards every byte before (and regardless of) recording."""

    def __init__(self, app: Any, writer: SpoolWriter, *, resp_max_bytes: int = RESP_MAX_BYTES) -> None:
        self.app = app
        self.writer = writer
        self.resp_max_bytes = resp_max_bytes

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        scope_type = scope.get("type")
        if scope_type == "lifespan":
            await self._handle_lifespan(scope, receive, send)
            return
        if scope_type != "http":
            await self.app(scope, receive, send)
            return
        method = scope.get("method", "")
        path = scope.get("path", "")
        if method != "POST" or path not in COLLECT_PATHS:
            await self.app(scope, receive, send)
            return
        try:
            enabled = self.writer.accepting()
        except Exception:
            logger.debug("audit capture: control read failed", exc_info=True)
            enabled = False
        if not enabled:
            await self.app(scope, receive, send)
            return
        await self._handle(scope, receive, send, path)

    async def _handle_lifespan(self, scope: dict, receive: Any, send: Any) -> None:
        async def wrapped_send(message: dict) -> None:
            if message.get("type") == "lifespan.shutdown.complete":
                try:
                    self.writer.stop()
                except Exception:
                    logger.debug("audit capture: shutdown flush failed", exc_info=True)
            await send(message)

        await self.app(scope, receive, wrapped_send)

    async def _handle(self, scope: dict, receive: Any, send: Any, path: str) -> None:
        started = time.time()
        captured: list[dict] = []
        body = bytearray()
        try:
            while True:
                message = await receive()
                captured.append(message)
                if message.get("type") == "http.request":
                    chunk = message.get("body", b"") or b""
                    body.extend(chunk)
                    if not message.get("more_body", False):
                        break
                elif message.get("type") == "http.disconnect":
                    break
        except Exception:
            logger.debug("audit capture: request read failed", exc_info=True)

        replay_index = 0

        async def replay_receive() -> dict:
            nonlocal replay_index
            if replay_index < len(captured):
                message = captured[replay_index]
                replay_index += 1
                return message
            return await receive()

        state = _RespState(self.resp_max_bytes)

        async def wrapped_send(message: dict) -> None:
            try:
                self._inspect(message, state)
            except Exception:
                logger.debug("audit capture: response inspect failed", exc_info=True)
            await send(message)

        error: BaseException | None = None
        try:
            await self.app(scope, replay_receive, wrapped_send)
        except Exception as exc:
            error = exc
            raise
        finally:
            try:
                record = self._record(scope, path, bytes(body), state, started, error)
                self.writer.enqueue(record)
            except Exception:
                logger.debug("audit capture: enqueue failed", exc_info=True)

    def _inspect(self, message: dict, state: _RespState) -> None:
        mtype = message.get("type")
        if mtype == "http.response.start":
            state.status = int(message.get("status", 0) or 0)
            state.headers = list(message.get("headers") or [])
        elif mtype == "http.response.body":
            chunk = message.get("body", b"") or b""
            state.total += len(chunk)
            room = state.cap - len(state.buf)
            if room > 0 and chunk:
                state.buf.extend(chunk[:room])
            if state.total > state.cap:
                state.truncated = True
            if not message.get("more_body", False):
                state.completed = True

    def _record(
        self,
        scope: dict,
        path: str,
        body: bytes,
        state: _RespState,
        started: float,
        error: BaseException | None,
    ) -> dict:
        headers = _header_map(scope)
        via, remote_ip = _via_and_ip(headers)
        try:
            resp_headers = redact_headers(state.headers)
        except Exception:
            resp_headers = {}
        return {
            "v": SCHEMA_VERSION,
            "id": uuid.uuid4().hex,
            "ts_start": started,
            "ts_end": time.time(),
            "username": (os.environ.get("TELEMETRY_USERNAME") or "unknown").strip() or "unknown",
            "app_version": os.environ.get("APP_VERSION") or "",
            "upstream_sha": os.environ.get("GATEWAY_UPSTREAM_SHA") or "",
            "os": sys.platform,
            "via": via,
            "remote_ip": remote_ip,
            "method": scope.get("method", ""),
            "path": path,
            "req_headers": headers,
            "req_bytes": len(body),
            "req_body": body.decode("utf-8", errors="replace"),
            "status": state.status,
            "resp_headers": resp_headers,
            "resp_bytes": state.total,
            "resp": {
                "raw": bytes(state.buf).decode("utf-8", errors="replace"),
                "truncated": state.truncated,
            },
            "completed": bool(state.completed and error is None),
            "error": type(error).__name__ if error is not None else None,
        }


def wrap_app(
    app: Any,
    *,
    audit_dir: Path | None = None,
    clock: Callable[[], float] | None = None,
    rotate_seconds: float = ROTATE_SECONDS,
    queue_max_bytes: int = QUEUE_MAX_BYTES,
    resp_max_bytes: int = RESP_MAX_BYTES,
    start_thread: bool = True,
) -> AuditCaptureMiddleware:
    """Wrap ``app``. Capture stays off until ``audit/control.json`` says otherwise."""
    root = audit_dir if audit_dir is not None else paths.data_dir() / "audit"
    root.mkdir(parents=True, exist_ok=True)
    _chmod(root, 0o700)
    now = clock or time.time
    control = ControlReader(root / "control.json", now)
    writer = SpoolWriter(
        root / "spool",
        control,
        clock=now,
        rotate_seconds=rotate_seconds,
        queue_max_bytes=queue_max_bytes,
    )
    if start_thread:
        writer.start()
    logger.info("audit capture armed ({})", root)
    return AuditCaptureMiddleware(app, writer, resp_max_bytes=resp_max_bytes)
