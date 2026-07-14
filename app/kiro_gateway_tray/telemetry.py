# app/kiro_gateway_tray/telemetry.py
"""Usage telemetry: a side-channel ASGI middleware wrapped around the vendored
gateway's ``main.app`` (so we never touch ``vendor/``).

Design source of truth: ``docs/2026-06-25-telemetry-design.md``. The shape here
follows sections 五 (middleware), 六 (local aggregation), 八 (worker payload),
九 (client breakdown) and 十 (reliability/spooling).

Pipeline:
  request ─▶ TelemetryMiddleware ─▶ vendored main.app
                 │ per request: model / status / bytes / usage (SSE + JSON)
                 │ also note_model → CreditTracker (background /usage segments)
                 ▼ accumulate into the currently-open in-memory bucket
            Aggregator (keyed by username × model × app_version × bucket_start)
                 ▲ a background timer thread checkpoints credits, closes & uploads
                 ▼ on upload failure: append to <data_dir>/telemetry/pending.jsonl
            Uploader ─▶ POST {TELEMETRY_URL}  (Authorization: Bearer …)

Hard rules (see design 五):
  * Telemetry collection must NEVER break a request: every collection step is
    wrapped in try/except and only logged on failure.
  * Forward every response chunk verbatim with zero buffering (streaming-safe):
    we only keep a small rolling tail for SSE and a capped buffer for JSON to
    recover the final ``usage`` after the bytes have already been forwarded.
  * Only ``POST /v1/chat/completions`` and ``POST /v1/messages`` are collected;
    everything else passes straight through.
"""
from __future__ import annotations

import json
import os
import queue
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

import httpx

from .httpclient import local_client, resolve_proxy
from .log import logger

# --- constants ---------------------------------------------------------------

SCHEMA_VERSION = 1
DEFAULT_BUCKET_SECONDS = 600           # 10 minutes (design 三/六)
DEFAULT_FLUSH_INTERVAL = 600           # timer wake cadence, aligned to bucket
DEFAULT_MAX_RETENTION_DAYS = 30        # local spool retention (design 十)

# Only conversation endpoints are collected, to avoid model=null noise rows
# from /health, /usage, /v1/models, OPTIONS, etc. (design 五.1).
COLLECT_PATHS = frozenset({"/v1/chat/completions", "/v1/messages"})

_SSE_TAIL_CAP = 65536        # bytes of trailing SSE kept to recover final usage
_JSON_BODY_CAP = 2_000_000   # cap on buffered non-stream JSON body
_REQ_BODY_CAP = 4_000_000    # cap on buffered request body for model lookup
_UPLOAD_BATCH = 100          # rows per spool-retry chunk (LIFO, newest first)
_UPLOAD_TIMEOUT = 10.0       # seconds
_BACKOFF_BASE = 30.0         # seconds, exponential backoff floor for spool retry
_BACKOFF_MAX = 3600.0        # seconds, backoff ceiling

# Aggregation key field order; also the column order of an uploaded row's keys.
_DIMENSIONS = ("username", "model", "app_version", "bucket_start")


# --- time bucketing ----------------------------------------------------------

def bucket_start_for(ts: float, bucket_seconds: int) -> int:
    """Floor a Unix timestamp to the start of its ``bucket_seconds`` window."""
    if bucket_seconds <= 0:
        bucket_seconds = DEFAULT_BUCKET_SECONDS
    return int(ts) - (int(ts) % bucket_seconds)


# --- config ------------------------------------------------------------------

@dataclass
class TelemetryConfig:
    """Resolved telemetry settings for the gateway child process."""
    endpoint_url: str = ""
    secret: str = ""
    username: str = "unknown"
    app_version: str = "unknown"
    bucket_seconds: int = DEFAULT_BUCKET_SECONDS
    flush_interval: int = DEFAULT_FLUSH_INTERVAL
    max_retention_days: int = DEFAULT_MAX_RETENTION_DAYS
    # Inputs for on-401 secret refresh (design 八). The refresh endpoint is
    # same-origin as /provision and authenticated with the activation code.
    provision_url: str = ""
    shared_secret: str = ""
    # Local gateway probe for CreditTracker (already in child env via to_gateway_env).
    gateway_port: int = 64005
    proxy_api_key: str = ""

    @property
    def enabled(self) -> bool:
        """Telemetry has no on/off switch (design 三), but an empty endpoint
        means "not configured" so we stay dormant rather than crash."""
        return bool(self.endpoint_url)

    @property
    def can_refresh(self) -> bool:
        """A 401 can only be recovered when we have both an activation code and
        the provision origin to call /telemetry-secret against."""
        return bool(self.provision_url and self.shared_secret and self.username)

    @property
    def can_sample_credits(self) -> bool:
        """Credit sampling needs a local API key to call GET /usage."""
        return bool(self.proxy_api_key and self.gateway_port > 0)


def from_env(env: dict[str, str] | None = None) -> TelemetryConfig:
    """Build a config from the gateway env vars injected by appconfig."""
    e = env if env is not None else os.environ

    def _int(name: str, default: int) -> int:
        try:
            v = int(str(e.get(name, "")).strip())
            return v if v > 0 else default
        except (TypeError, ValueError):
            return default

    return TelemetryConfig(
        endpoint_url=(e.get("TELEMETRY_URL") or "").strip(),
        secret=(e.get("TELEMETRY_SECRET") or "").strip(),
        username=(e.get("TELEMETRY_USERNAME") or "unknown").strip() or "unknown",
        app_version=(e.get("APP_VERSION") or "unknown").strip() or "unknown",
        bucket_seconds=_int("TELEMETRY_BUCKET_SECONDS", DEFAULT_BUCKET_SECONDS),
        flush_interval=_int("TELEMETRY_FLUSH_INTERVAL", DEFAULT_FLUSH_INTERVAL),
        max_retention_days=_int("TELEMETRY_MAX_RETENTION_DAYS", DEFAULT_MAX_RETENTION_DAYS),
        provision_url=(e.get("TELEMETRY_PROVISION_URL") or "").strip(),
        shared_secret=(e.get("TELEMETRY_SHARED_SECRET") or "").strip(),
        gateway_port=_int("SERVER_PORT", 64005),
        proxy_api_key=(e.get("PROXY_API_KEY") or "").strip(),
    )


# --- in-memory aggregation ---------------------------------------------------

@dataclass
class _Bucket:
    """One open aggregation row: a (username, model, app_version, bucket_start)
    combo with running sums (design 六)."""
    bucket_start: int
    bucket_seconds: int
    username: str
    model: str
    app_version: str
    requests: int = 0
    successes: int = 0
    errors: int = 0
    prompt_tokens_sum: int = 0
    completion_tokens_sum: int = 0
    total_tokens_sum: int = 0
    request_bytes_sum: int = 0
    response_bytes_sum: int = 0
    # Estimated Credit usage from /usage segment diffs (Kiro billing may lag).
    estimated_credits: float = 0.0
    credit_estimate_segments: int = 0
    credit_estimate_missing_segments: int = 0

    def to_row(self) -> dict[str, Any]:
        """Serialise to the worker payload row shape (design 八).

        Credit fields are null when no segment was settled for this bucket
        (unknown). Explicit 0 means a settle measured zero Credit consumed.
        """
        has_credit_estimate = (
            self.credit_estimate_segments > 0
            or self.credit_estimate_missing_segments > 0
        )
        return {
            "bucket_start": self.bucket_start,
            "bucket_seconds": self.bucket_seconds,
            "username": self.username,
            "model": self.model,
            "app_version": self.app_version,
            "requests": self.requests,
            "successes": self.successes,
            "errors": self.errors,
            "prompt_tokens_sum": self.prompt_tokens_sum,
            "completion_tokens_sum": self.completion_tokens_sum,
            "total_tokens_sum": self.total_tokens_sum,
            "request_bytes_sum": self.request_bytes_sum,
            "response_bytes_sum": self.response_bytes_sum,
            "estimated_credits": (
                float(self.estimated_credits) if has_credit_estimate else None
            ),
            "credit_estimate_segments": (
                int(self.credit_estimate_segments) if has_credit_estimate else None
            ),
            "credit_estimate_missing_segments": (
                int(self.credit_estimate_missing_segments)
                if has_credit_estimate else None
            ),
        }


@dataclass
class RequestSample:
    """One finished request's measurements, fed to the aggregator."""
    model: str = "unknown"
    success: bool = False
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    request_bytes: int = 0
    response_bytes: int = 0


@dataclass
class _ResponseState:
    """Mutable accumulator for the response side of one collected request.

    Replaces a string-keyed dict so a typo'd field is a static error, not a
    silent miss. Holds only what we need to recover the final usage after the
    bytes are already forwarded: status, content-type flags, byte count and the
    small retained SSE tail / capped JSON buffer."""
    status: int = 0
    content_type: str = ""
    response_bytes: int = 0
    is_sse: bool = False
    is_json: bool = False
    completed: bool = False
    sse_tail: bytearray = field(default_factory=bytearray)
    json_buf: bytearray = field(default_factory=bytearray)


class Aggregator:
    """Thread-safe in-memory rollup. The middleware (event loop) calls
    :meth:`record`; the timer thread calls :meth:`collect_closed` / :meth:`drain_all`.
    A single lightweight lock guards the bucket dict (design 十)."""

    def __init__(self, username: str, app_version: str, bucket_seconds: int) -> None:
        self.username = username or "unknown"
        self.app_version = app_version or "unknown"
        self.bucket_seconds = bucket_seconds if bucket_seconds > 0 else DEFAULT_BUCKET_SECONDS
        self._buckets: dict[tuple, _Bucket] = {}
        self._lock = threading.Lock()

    def record(self, sample: RequestSample, *, now: float | None = None) -> None:
        ts = time.time() if now is None else now
        bstart = bucket_start_for(ts, self.bucket_seconds)
        model = sample.model or "unknown"
        key = (self.username, model, self.app_version, bstart)
        with self._lock:
            b = self._buckets.get(key)
            if b is None:
                b = _Bucket(
                    bucket_start=bstart,
                    bucket_seconds=self.bucket_seconds,
                    username=self.username,
                    model=model,
                    app_version=self.app_version,
                )
                self._buckets[key] = b
            b.requests += 1
            if sample.success:
                b.successes += 1
            else:
                b.errors += 1
            # Token sums accumulate only non-null components (design 六).
            if sample.prompt_tokens:
                b.prompt_tokens_sum += int(sample.prompt_tokens)
            if sample.completion_tokens:
                b.completion_tokens_sum += int(sample.completion_tokens)
            if sample.total_tokens:
                b.total_tokens_sum += int(sample.total_tokens)
            b.request_bytes_sum += max(0, int(sample.request_bytes))
            b.response_bytes_sum += max(0, int(sample.response_bytes))

    def record_credit_estimate(
        self,
        model: str,
        *,
        credits: float | None = None,
        missing: bool = False,
        now: float | None = None,
    ) -> None:
        """Accumulate a settled Credit segment (or mark an unestimable gap).

        Credits are attributed to the bucket that contains ``now`` (segment end).
        Creates the bucket if needed so a late segment settle is not dropped."""
        ts = time.time() if now is None else now
        bstart = bucket_start_for(ts, self.bucket_seconds)
        model = model or "unknown"
        key = (self.username, model, self.app_version, bstart)
        with self._lock:
            b = self._buckets.get(key)
            if b is None:
                b = _Bucket(
                    bucket_start=bstart,
                    bucket_seconds=self.bucket_seconds,
                    username=self.username,
                    model=model,
                    app_version=self.app_version,
                )
                self._buckets[key] = b
            if missing:
                b.credit_estimate_missing_segments += 1
            elif credits is not None and credits >= 0:
                b.estimated_credits += float(credits)
                b.credit_estimate_segments += 1

    def collect_closed(self, now: float | None = None) -> list[dict[str, Any]]:
        """Remove & return rows for buckets whose window has fully elapsed.

        A bucket is closed once ``bucket_start + bucket_seconds <= now`` — no new
        request can land in it after that point, so its value is terminal."""
        ts = time.time() if now is None else now
        out: list[dict[str, Any]] = []
        with self._lock:
            closed_keys = [
                k for k, b in self._buckets.items()
                if b.bucket_start + b.bucket_seconds <= ts
            ]
            for k in closed_keys:
                out.append(self._buckets.pop(k).to_row())
        return out

    def drain_all(self) -> list[dict[str, Any]]:
        """Remove & return rows for ALL buckets (used on shutdown flush)."""
        with self._lock:
            rows = [b.to_row() for b in self._buckets.values()]
            self._buckets.clear()
        return rows


# --- Credit segment tracker --------------------------------------------------

@dataclass(frozen=True)
class CreditReading:
    """One snapshot of account Credit usage from GET /usage."""
    credits_used: float
    reset_key: str = ""


def parse_credits_used(data: dict[str, Any] | None) -> CreditReading | None:
    """Sum ``breakdowns[].used`` into a single account Credit reading.

    Returns None when the payload is unusable. ``reset_key`` is ``nextDateReset``
    (stringified) so a billing-cycle reset can invalidate an open segment."""
    if not isinstance(data, dict):
        return None
    breakdowns = data.get("breakdowns")
    if not isinstance(breakdowns, list) or not breakdowns:
        return None
    total = 0.0
    any_used = False
    for b in breakdowns:
        if not isinstance(b, dict):
            continue
        raw = b.get("used")
        if raw is None:
            continue
        try:
            total += float(raw)
            any_used = True
        except (TypeError, ValueError):
            continue
    if not any_used:
        return None
    reset = data.get("nextDateReset")
    reset_key = "" if reset is None else str(reset)
    return CreditReading(credits_used=total, reset_key=reset_key)


def fetch_credits_used(
    *,
    port: int,
    api_key: str,
    timeout: float = 10.0,
) -> CreditReading | None:
    """Probe localhost GET /usage and return the Credit reading, or None."""
    if not api_key or port <= 0:
        return None
    url = f"http://127.0.0.1:{int(port)}/usage"
    headers = {"Authorization": f"Bearer {api_key}"}
    try:
        with local_client(timeout=timeout) as client:
            resp = client.get(url, headers=headers)
        if resp.status_code != 200:
            logger.debug("telemetry: /usage returned {}", resp.status_code)
            return None
        return parse_credits_used(resp.json())
    except Exception:
        logger.debug("telemetry: /usage credit sample failed", exc_info=True)
        return None


class CreditTracker:
    """Background model-segment Credit estimator.

    Middleware calls :meth:`note_model` (non-blocking). A dedicated worker thread
    serialises model switches and samples GET /usage so request latency is never
    blocked. :meth:`checkpoint` waits for a sample+settle of the current model
    before the reporter closes expired buckets.
    """

    def __init__(
        self,
        aggregator: Aggregator,
        *,
        sample_fn: Callable[[], CreditReading | None] | None = None,
        bucket_seconds: int = DEFAULT_BUCKET_SECONDS,
    ) -> None:
        self.aggregator = aggregator
        self._sample_fn = sample_fn
        self.bucket_seconds = bucket_seconds if bucket_seconds > 0 else DEFAULT_BUCKET_SECONDS
        self._queue: queue.Queue[tuple] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        # Open segment state (only touched on the worker thread).
        self._current_model: str | None = None
        self._baseline: float | None = None
        self._reset_key: str | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="telemetry-credit", daemon=True
        )
        self._thread.start()

    def stop(self, *, timeout: float = 5.0) -> None:
        """Drain pending events then stop the worker thread."""
        self._queue.put(("stop",))
        t = self._thread
        if t and t.is_alive() and t is not threading.current_thread():
            t.join(timeout=timeout)
        self._thread = None

    def note_model(self, model: str) -> None:
        """Enqueue a model sighting. Never blocks on /usage."""
        try:
            self._queue.put(("model", model or "unknown", time.time()))
        except Exception:
            logger.debug("telemetry: note_model enqueue failed", exc_info=True)

    def checkpoint(self, *, now: float | None = None, timeout: float = 15.0) -> None:
        """Sample+settle the current model segment; wait until done or timeout."""
        done = threading.Event()
        ts = time.time() if now is None else now
        try:
            self._queue.put(("checkpoint", done, ts))
        except Exception:
            logger.debug("telemetry: checkpoint enqueue failed", exc_info=True)
            return
        if not done.wait(timeout=timeout):
            logger.debug("telemetry: credit checkpoint timed out after {}s", timeout)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                item = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                kind = item[0]
                if kind == "stop":
                    self._stop.set()
                    break
                if kind == "model":
                    self._handle_model(item[1], item[2])
                elif kind == "checkpoint":
                    self._handle_checkpoint(item[1], item[2])
            except Exception:
                logger.debug("telemetry: credit tracker event failed", exc_info=True)
                if item[0] == "checkpoint":
                    try:
                        item[1].set()
                    except Exception:
                        pass

    def _sample(self) -> CreditReading | None:
        if self._sample_fn is None:
            return None
        try:
            return self._sample_fn()
        except Exception:
            logger.debug("telemetry: credit sample_fn failed", exc_info=True)
            return None

    def _handle_model(self, model: str, ts: float) -> None:
        model = model or "unknown"
        if self._current_model is None:
            reading = self._sample()
            if reading is None:
                # No baseline yet — keep waiting; next note_model retries.
                return
            self._current_model = model
            self._baseline = reading.credits_used
            self._reset_key = reading.reset_key
            return
        if model == self._current_model:
            if self._baseline is None:
                reading = self._sample()
                if reading is not None:
                    self._baseline = reading.credits_used
                    self._reset_key = reading.reset_key
            return
        # Switch A → B: one sample settles A and starts B.
        reading = self._sample()
        self._settle(self._current_model, reading, now=ts)
        if reading is None:
            # Lost baseline; next successful sample re-establishes.
            self._current_model = model
            self._baseline = None
            self._reset_key = None
            return
        self._current_model = model
        self._baseline = reading.credits_used
        self._reset_key = reading.reset_key

    def _handle_checkpoint(self, done: threading.Event, ts: float) -> None:
        try:
            if self._current_model is None:
                return
            reading = self._sample()
            self._settle(self._current_model, reading, now=ts)
            if reading is not None:
                # Continue the same model from the checkpoint reading.
                self._baseline = reading.credits_used
                self._reset_key = reading.reset_key
            else:
                self._baseline = None
                self._reset_key = None
        finally:
            done.set()

    def _settle(
        self,
        model: str,
        reading: CreditReading | None,
        *,
        now: float,
    ) -> None:
        if self._baseline is None or reading is None:
            self.aggregator.record_credit_estimate(model, missing=True, now=now)
            return
        if self._reset_key is not None and reading.reset_key != self._reset_key:
            # Billing cycle reset — discard the open segment.
            self.aggregator.record_credit_estimate(model, missing=True, now=now)
            return
        delta = float(reading.credits_used) - float(self._baseline)
        if delta < 0:
            self.aggregator.record_credit_estimate(model, missing=True, now=now)
            return
        self.aggregator.record_credit_estimate(model, credits=delta, now=now)


# --- local spool (pending.jsonl) ---------------------------------------------

def _row_key(row: dict[str, Any]) -> tuple:
    return tuple(row.get(d) for d in _DIMENSIONS)


class PendingStore:
    """Append-only JSON-lines spool for buckets that failed to upload.

    One JSON object per line (fields == design 八 row). Append on failure; after
    a successful retry the whole file is rewritten (read all → drop succeeded &
    expired → write back). The file is at most a few MB, so a full rewrite is
    cheap and avoids any partial-delete machinery (design 十)."""

    def __init__(self, path: Path, max_retention_days: int = DEFAULT_MAX_RETENTION_DAYS) -> None:
        self.path = Path(path)
        self.max_retention_days = max_retention_days

    def append(self, rows: Iterable[dict[str, Any]]) -> None:
        rows = list(rows)
        if not rows:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as f:
                for row in rows:
                    f.write(json.dumps(row, ensure_ascii=False) + "\n")
        except Exception:
            logger.debug("telemetry: failed to append pending rows", exc_info=True)

    def load_all(self) -> list[dict[str, Any]]:
        """Read every spooled row in file order (oldest first). Corrupt lines
        are skipped, never fatal."""
        if not self.path.exists():
            return []
        out: list[dict[str, Any]] = []
        try:
            with self.path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except Exception:
                        continue
                    if isinstance(obj, dict):
                        out.append(obj)
        except Exception:
            logger.debug("telemetry: failed to read pending spool", exc_info=True)
        return out

    def _is_expired(self, row: dict[str, Any], now: float) -> bool:
        try:
            bstart = int(row.get("bucket_start", 0))
        except (TypeError, ValueError):
            return False
        return bstart < now - self.max_retention_days * 86400

    def rewrite(self, rows: Iterable[dict[str, Any]], *, now: float | None = None) -> None:
        """Replace the spool with ``rows`` minus expired ones. Empty ⇒ remove
        the file."""
        ts = time.time() if now is None else now
        kept = [r for r in rows if not self._is_expired(r, ts)]
        try:
            if not kept:
                if self.path.exists():
                    self.path.unlink()
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            with tmp.open("w", encoding="utf-8") as f:
                for row in kept:
                    f.write(json.dumps(row, ensure_ascii=False) + "\n")
            tmp.replace(self.path)
        except Exception:
            logger.debug("telemetry: failed to rewrite pending spool", exc_info=True)


# --- uploader ----------------------------------------------------------------

# Upload outcomes. We distinguish 401 (stale secret → triggers a refresh) from
# any other failure (network / 4xx / 5xx → just spool & back off).
UPLOAD_OK = "ok"
UPLOAD_UNAUTHORIZED = "unauthorized"
UPLOAD_ERROR = "error"


class Uploader:
    """POST rows to the telemetry worker with a pre-shared bearer secret.

    The secret only ever rides in the ``Authorization`` header, never the body
    and never the logs (design 八). ``secret`` is mutable so a successful key
    refresh can swap it in for the rest of the session."""

    def __init__(self, endpoint_url: str, secret: str, *, timeout: float = _UPLOAD_TIMEOUT) -> None:
        self.endpoint_url = endpoint_url
        self.secret = secret
        self.timeout = timeout

    def upload(self, rows: list[dict[str, Any]]) -> str:
        """Return one of UPLOAD_OK / UPLOAD_UNAUTHORIZED / UPLOAD_ERROR.

        Never raises. An empty batch is a no-op success."""
        if not rows:
            return UPLOAD_OK
        if not self.endpoint_url:
            return UPLOAD_ERROR
        headers = {"Content-Type": "application/json"}
        if self.secret:
            headers["Authorization"] = f"Bearer {self.secret}"
        body = {"schema_version": SCHEMA_VERSION, "rows": rows}
        try:
            # Honour the environment's proxy so users behind a SOCKS/HTTP proxy
            # can still upload telemetry through it. resolve_proxy() normalizes
            # socks:// -> socks5h:// so it doesn't crash client construction.
            resp = httpx.post(
                self.endpoint_url, json=body, headers=headers,
                timeout=self.timeout, proxy=resolve_proxy(),
            )
        except Exception:
            logger.debug("telemetry: upload request failed", exc_info=True)
            return UPLOAD_ERROR
        if resp.status_code == 200:
            return UPLOAD_OK
        if resp.status_code == 401:
            # Stale local secret: caller may refresh via /telemetry-secret.
            logger.debug("telemetry: upload unauthorized (401), secret may be stale")
            return UPLOAD_UNAUTHORIZED
        logger.debug("telemetry: upload rejected with HTTP {}", resp.status_code)
        return UPLOAD_ERROR


# --- secret refresh ----------------------------------------------------------

class SecretRefresher:
    """Fetches a fresh telemetry secret on 401, throttled to at most once per
    ``throttle_seconds`` (design 八: 60s).

    ``refresh_fn`` is a zero-arg callable returning the new secret (or "" on
    failure). It's injected so the network + config-write side effects stay out
    of this class and are trivial to fake in tests."""

    def __init__(self, refresh_fn: Any, *, throttle_seconds: float = 60.0) -> None:
        self._refresh_fn = refresh_fn
        self.throttle_seconds = throttle_seconds
        self.last_refresh = 0.0

    def maybe_refresh(self, *, now: float | None = None) -> str | None:
        """Return a new secret, "" if the refresh failed, or None if throttled.

        Throttled callers must NOT treat None as a failure to be retried
        immediately — they just spool and move on (design 八.3)."""
        ts = time.time() if now is None else now
        if ts - self.last_refresh < self.throttle_seconds:
            return None
        self.last_refresh = ts
        try:
            return self._refresh_fn() or ""
        except Exception:
            logger.debug("telemetry: secret refresh failed", exc_info=True)
            return ""


# --- reporter (owns aggregator + uploader + spool + timer) -------------------

class Reporter:
    """Glues the aggregator, uploader and spool together and drives the
    bucket-closing timer thread (design 十).

    Upload ordering on each tick: freshly-closed buckets first (newest data is
    most useful), then spooled backlog newest-first (LIFO) under exponential
    backoff, so a stuck backlog never blocks new data."""

    def __init__(
        self,
        config: TelemetryConfig,
        aggregator: Aggregator,
        uploader: Uploader,
        pending: PendingStore,
        refresher: "SecretRefresher | None" = None,
        on_secret_refresh: Any = None,
        credit_tracker: "CreditTracker | None" = None,
    ) -> None:
        self.config = config
        self.aggregator = aggregator
        self.uploader = uploader
        self.pending = pending
        self.refresher = refresher
        # Optional callback(new_secret) to persist a refreshed secret back to
        # config so it survives the next restart. Failures are swallowed.
        self.on_secret_refresh = on_secret_refresh
        self.credit_tracker = credit_tracker
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._next_pending_retry = 0.0
        self._backoff = _BACKOFF_BASE

    # -- timer lifecycle --
    def start(self) -> None:
        """Start the background timer thread (idempotent)."""
        if self.credit_tracker is not None:
            try:
                self.credit_tracker.start()
            except Exception:
                logger.debug("telemetry: credit tracker start failed", exc_info=True)
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="telemetry-flush", daemon=True
        )
        self._thread.start()

    def _loop(self) -> None:
        interval = self.config.flush_interval if self.config.flush_interval > 0 else DEFAULT_FLUSH_INTERVAL
        # Wake every flush_interval; stop.wait returns True when set.
        while not self._stop.wait(interval):
            try:
                self.tick()
            except Exception:
                logger.debug("telemetry: tick failed", exc_info=True)

    def flush_and_stop(self) -> None:
        """Stop the timer and drain all in-memory buckets (shutdown path).

        Called from the ASGI lifespan shutdown so a graceful (SIGTERM) exit
        never loses an open bucket (design 十)."""
        self._stop.set()
        t = self._thread
        if t and t.is_alive() and t is not threading.current_thread():
            t.join(timeout=5)
        self._thread = None
        try:
            self.flush_all()
        except Exception:
            logger.debug("telemetry: final flush failed", exc_info=True)
        if self.credit_tracker is not None:
            try:
                self.credit_tracker.stop()
            except Exception:
                logger.debug("telemetry: credit tracker stop failed", exc_info=True)

    # -- upload paths --
    def tick(self, *, now: float | None = None) -> None:
        """One timer iteration: credit checkpoint, close+upload, then retry spool."""
        ts = time.time() if now is None else now
        if self.credit_tracker is not None:
            try:
                # Attribute the settle to the bucket that is about to close.
                # At the exact boundary ``ts == bucket_start + bucket_seconds``,
                # ``bucket_start_for(ts)`` would land in the *next* window.
                self.credit_tracker.checkpoint(now=ts - 1e-6)
            except Exception:
                logger.debug("telemetry: credit checkpoint failed", exc_info=True)
        closed = self.aggregator.collect_closed(now=ts)
        self._upload_or_spool(closed)
        self._retry_pending(now=ts)

    def flush_all(self, *, now: float | None = None) -> None:
        """Upload every open bucket regardless of age (shutdown). Failures spool."""
        if self.credit_tracker is not None:
            try:
                self.credit_tracker.checkpoint(now=now)
            except Exception:
                logger.debug("telemetry: credit checkpoint on flush failed", exc_info=True)
        rows = self.aggregator.drain_all()
        self._upload_or_spool(rows)
        # One best-effort backlog drain on the way out.
        self._retry_pending(now=now, force=True)

    def _upload_or_spool(self, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        result = self.uploader.upload(rows)
        if result == UPLOAD_OK:
            return
        # On 401, try a single throttled secret refresh and one retry with the
        # new key before giving up and spooling (design 八.1/八.2).
        if result == UPLOAD_UNAUTHORIZED and self._try_refresh_secret():
            if self.uploader.upload(rows) == UPLOAD_OK:
                return
        self.pending.append(rows)

    def _try_refresh_secret(self) -> bool:
        """Attempt a throttled secret refresh. Returns True iff a new, different
        secret was obtained and swapped into the uploader. None (throttled) and
        "" (failed) both return False — the caller then just spools."""
        if self.refresher is None:
            return False
        new_secret = self.refresher.maybe_refresh()
        if not new_secret:
            return False
        if new_secret == self.uploader.secret:
            return False
        self.uploader.secret = new_secret
        if self.on_secret_refresh is not None:
            try:
                self.on_secret_refresh(new_secret)
            except Exception:
                logger.debug("telemetry: persist refreshed secret failed", exc_info=True)
        logger.info("telemetry: refreshed telemetry secret after 401")
        return True

    def _retry_pending(self, *, now: float | None = None, force: bool = False) -> None:
        ts = time.time() if now is None else now
        if not force and ts < self._next_pending_retry:
            return
        rows = self.pending.load_all()
        if not rows:
            self._backoff = _BACKOFF_BASE
            return
        # Dedupe by aggregation key (idempotent terminal values) and order
        # newest-first for LIFO retry (design 十.3).
        by_key: dict[tuple, dict[str, Any]] = {}
        for r in rows:
            by_key[_row_key(r)] = r
        ordered = sorted(
            by_key.values(),
            key=lambda r: int(r.get("bucket_start", 0)),
            reverse=True,
        )
        succeeded: list[tuple] = []
        had_failure = False
        refreshed = False
        for i in range(0, len(ordered), _UPLOAD_BATCH):
            chunk = ordered[i:i + _UPLOAD_BATCH]
            result = self.uploader.upload(chunk)
            if result == UPLOAD_UNAUTHORIZED and not refreshed and self._try_refresh_secret():
                # Refresh once mid-backlog, then retry this same chunk.
                refreshed = True
                result = self.uploader.upload(chunk)
            if result == UPLOAD_OK:
                succeeded.extend(_row_key(r) for r in chunk)
            else:
                had_failure = True
                break  # stop at first failed chunk; keep the rest for next time
        if succeeded:
            done = set(succeeded)
            remaining = [r for r in ordered if _row_key(r) not in done]
            self.pending.rewrite(remaining, now=ts)
        else:
            # Nothing sent: still rewrite to drop expired rows (design 十.5).
            self.pending.rewrite(ordered, now=ts)
        if had_failure:
            self._next_pending_retry = ts + self._backoff
            self._backoff = min(self._backoff * 2, _BACKOFF_MAX)
        else:
            self._next_pending_retry = 0.0
            self._backoff = _BACKOFF_BASE


# --- usage / model extraction helpers ----------------------------------------

def extract_model(body: bytes) -> str:
    """Pull ``model`` from a request body, falling back to ``"unknown"`` so the
    aggregation key is never null (design 五.4)."""
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


def _iter_usage_objects(obj: Any) -> Iterable[dict[str, Any]]:
    """Yield usage dicts from a parsed SSE/JSON object. Anthropic nests the
    initial usage under ``message.usage`` (message_start) and the final under a
    top-level ``usage`` (message_delta); OpenAI uses a top-level ``usage``."""
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


def _merge_usage_from_sse(buf: bytes) -> dict[str, Any] | None:
    """Recover usage from an SSE byte tail.

    Merges usage fields across all ``data:`` events (last-wins per field) so
    Anthropic's split input/output tokens are both captured; for OpenAI this is
    just the single final-chunk usage."""
    if not buf:
        return None
    try:
        text = buf.decode("utf-8", errors="replace")
    except Exception:
        return None
    merged: dict[str, Any] = {}
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
        for u in _iter_usage_objects(obj):
            merged.update(u)
    return merged or None


def _usage_from_json(buf: bytes) -> dict[str, Any] | None:
    if not buf:
        return None
    try:
        data = json.loads(buf)
    except Exception:
        return None
    merged: dict[str, Any] = {}
    for u in _iter_usage_objects(data):
        merged.update(u)
    return merged or None


def parse_usage(usage: dict[str, Any] | None) -> tuple[int | None, int | None, int | None]:
    """Normalise an OpenAI/Anthropic usage dict.

    Returns ``(prompt, completion, total)``. Maps Anthropic
    ``input_tokens``/``output_tokens`` onto the prompt/completion columns and
    synthesises ``total`` when only the parts are present."""
    if not usage:
        return None, None, None
    prompt = usage.get("prompt_tokens")
    if prompt is None:
        prompt = usage.get("input_tokens")
    completion = usage.get("completion_tokens")
    if completion is None:
        completion = usage.get("output_tokens")
    total = usage.get("total_tokens")
    if total is None and (prompt is not None or completion is not None):
        total = int(prompt or 0) + int(completion or 0)
    return (
        int(prompt) if prompt is not None else None,
        int(completion) if completion is not None else None,
        int(total) if total is not None else None,
    )


# --- ASGI middleware ---------------------------------------------------------

class TelemetryMiddleware:
    """Pure-ASGI side-channel collector wrapped around the inner app.

    It never buffers a whole response: every chunk is forwarded verbatim, and we
    only retain a small SSE tail / capped JSON buffer to recover the final usage
    after the bytes are already on the wire."""

    def __init__(self, app: Any, reporter: Reporter) -> None:
        self.app = app
        self.reporter = reporter

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
        await self._handle_collected(scope, receive, send)

    async def _handle_lifespan(self, scope: dict, receive: Any, send: Any) -> None:
        """Pass the inner app's startup/shutdown through untouched; start the
        timer on startup, flush all buckets on shutdown (design 十)."""
        async def wrapped_send(message: dict) -> None:
            mtype = message.get("type")
            if mtype == "lifespan.startup.complete":
                try:
                    self.reporter.start()
                except Exception:
                    logger.debug("telemetry: reporter start failed", exc_info=True)
            elif mtype == "lifespan.shutdown.complete":
                try:
                    self.reporter.flush_and_stop()
                except Exception:
                    logger.debug("telemetry: reporter flush failed", exc_info=True)
            await send(message)

        await self.app(scope, receive, wrapped_send)

    async def _handle_collected(self, scope: dict, receive: Any, send: Any) -> None:
        # --- request side: buffer the body so we can read `model`, then replay
        # it to the inner app (the ASGI receive stream is single-shot). ---
        captured: list[dict] = []
        body = bytearray()
        request_bytes = 0
        try:
            while True:
                message = await receive()
                captured.append(message)
                if message.get("type") == "http.request":
                    chunk = message.get("body", b"") or b""
                    request_bytes += len(chunk)
                    if len(body) < _REQ_BODY_CAP:
                        body.extend(chunk)
                    if not message.get("more_body", False):
                        break
                elif message.get("type") == "http.disconnect":
                    break
        except Exception:
            logger.debug("telemetry: request capture failed", exc_info=True)

        model = "unknown"
        try:
            model = extract_model(bytes(body))
        except Exception:
            logger.debug("telemetry: model extraction failed", exc_info=True)

        # Non-blocking: enqueue model sighting for Credit segment tracking.
        try:
            tracker = getattr(self.reporter, "credit_tracker", None)
            if tracker is not None:
                tracker.note_model(model)
        except Exception:
            logger.debug("telemetry: note_model failed", exc_info=True)

        replay_index = 0

        async def replay_receive() -> dict:
            nonlocal replay_index
            if replay_index < len(captured):
                msg = captured[replay_index]
                replay_index += 1
                return msg
            return await receive()

        # --- response side: forward every chunk verbatim; retain only what we
        # need to recover the final usage afterwards. ---
        state = _ResponseState()

        async def wrapped_send(message: dict) -> None:
            try:
                self._inspect_response(message, state)
            except Exception:
                logger.debug("telemetry: response inspect failed", exc_info=True)
            await send(message)

        sample = RequestSample(
            model=model,
            request_bytes=request_bytes,
        )
        try:
            await self.app(scope, replay_receive, wrapped_send)
        except Exception:
            # The request is already failing; record an error sample and let the
            # exception propagate so we never change request behaviour.
            self._finalise(sample, state, ok=False)
            raise
        else:
            self._finalise(sample, state, ok=True)

    @staticmethod
    def _inspect_response(message: dict, state: _ResponseState) -> None:
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
            state.content_type = ctype
            state.is_sse = "text/event-stream" in ctype
            state.is_json = "application/json" in ctype
        elif mtype == "http.response.body":
            chunk = message.get("body", b"") or b""
            state.response_bytes += len(chunk)
            if state.is_sse:
                tail = state.sse_tail
                tail.extend(chunk)
                if len(tail) > _SSE_TAIL_CAP:
                    del tail[:-_SSE_TAIL_CAP]
            elif state.is_json:
                buf = state.json_buf
                if len(buf) < _JSON_BODY_CAP:
                    buf.extend(chunk)
            if not message.get("more_body", False):
                state.completed = True

    def _finalise(self, sample: RequestSample, state: _ResponseState, *, ok: bool) -> None:
        """Extract usage from the retained buffers and record the sample. Never
        raises (telemetry must not affect the request)."""
        try:
            sample.response_bytes = int(state.response_bytes)
            usage: dict[str, Any] | None = None
            if state.is_sse:
                usage = _merge_usage_from_sse(bytes(state.sse_tail))
            elif state.is_json:
                usage = _usage_from_json(bytes(state.json_buf))
            prompt, completion, total = parse_usage(usage)
            sample.prompt_tokens = prompt
            sample.completion_tokens = completion
            sample.total_tokens = total
            status_ok = 200 <= int(state.status or 0) < 300
            has_usage = prompt is not None or completion is not None or total is not None
            # Success = the request completed AND we recovered a final usage
            # (design 六: successes == "got final usage").
            sample.success = bool(ok and status_ok and state.completed and has_usage)
            self.reporter.aggregator.record(sample)
        except Exception:
            logger.debug("telemetry: finalise failed", exc_info=True)


# --- assembly ----------------------------------------------------------------

def build_reporter(config: TelemetryConfig, data_dir: Path) -> Reporter:
    """Wire an aggregator, uploader and spool into a Reporter.

    When refresh inputs are present (provision_url + activation code), also wire
    a SecretRefresher that calls /telemetry-secret on 401 and a callback that
    persists the new secret back to config so it survives the next restart."""
    aggregator = Aggregator(config.username, config.app_version, config.bucket_seconds)
    uploader = Uploader(config.endpoint_url, config.secret)
    pending = PendingStore(
        Path(data_dir) / "telemetry" / "pending.jsonl",
        max_retention_days=config.max_retention_days,
    )
    refresher: SecretRefresher | None = None
    on_secret_refresh = None
    if config.can_refresh:
        def _do_refresh() -> str:
            from . import provision
            return provision.refresh_telemetry_secret(
                config.provision_url, config.shared_secret, config.username
            )
        refresher = SecretRefresher(_do_refresh)
        on_secret_refresh = _persist_refreshed_secret
    credit_tracker: CreditTracker | None = None
    if config.can_sample_credits:
        port = config.gateway_port
        api_key = config.proxy_api_key

        def _sample() -> CreditReading | None:
            return fetch_credits_used(port=port, api_key=api_key)

        credit_tracker = CreditTracker(
            aggregator,
            sample_fn=_sample,
            bucket_seconds=config.bucket_seconds,
        )
    return Reporter(
        config, aggregator, uploader, pending,
        refresher=refresher, on_secret_refresh=on_secret_refresh,
        credit_tracker=credit_tracker,
    )


def _persist_refreshed_secret(new_secret: str) -> None:
    """Write a refreshed telemetry secret back to config.toml.

    Runs in the gateway child process. The parent re-reads config on the next
    start, so persisting here means the rotated key survives restarts instead of
    re-fetching on every launch. Best-effort: never raises."""
    try:
        from . import appconfig
        cfg = appconfig.load()
        if cfg.telemetry.secret != new_secret:
            cfg.telemetry.secret = new_secret
            appconfig.save(cfg)
    except Exception:
        logger.debug("telemetry: failed to persist refreshed secret to config", exc_info=True)


def wrap_app(app: Any, *, env: dict[str, str] | None = None, data_dir: Path | None = None) -> Any:
    """Wrap ``app`` in :class:`TelemetryMiddleware` when telemetry is configured.

    Returns the app unchanged when ``TELEMETRY_URL`` is absent, so the gateway
    runs identically when telemetry isn't set up."""
    config = from_env(env)
    if not config.enabled:
        return app
    if data_dir is None:
        from . import paths
        data_dir = paths.data_dir()
    reporter = build_reporter(config, data_dir)
    logger.info(
        "telemetry enabled (bucket={}s, retention={}d)",
        config.bucket_seconds, config.max_retention_days,
    )
    return TelemetryMiddleware(app, reporter)
