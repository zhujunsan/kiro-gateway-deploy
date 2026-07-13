# -*- coding: utf-8 -*-
"""Background uploader for gateway error incident snapshots → Workers Logs.

Receives immutable snapshots from vendor ``debug_logger`` (via
``set_error_snapshot_callback``), splits them into a searchable manifest plus
size-capped artifact chunks, and POSTs each record to ``/telemetry/errors``.

Constraints (Cloudflare Workers Logs):
- Each Worker invocation may emit at most 256 KB of log data total.
- We send ONE record per HTTP request and keep serialized JSON ≤ 128 KiB
  client-side; the Worker rejects anything over 192 KiB with 413.

Never raises into the request path. Failures spool to pending-errors.jsonl.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from .httpclient import resolve_proxy
from .log import logger

SCHEMA_VERSION = 1
KIND = "kiro_gateway_incident"

# Final JSON body for one POST must stay under this (client hard cap).
CLIENT_RECORD_MAX_BYTES = 128 * 1024
# Leave room for envelope keys around the data payload when sizing chunks.
_CHUNK_PAYLOAD_BUDGET = 96 * 1024

UPLOAD_OK = "ok"
UPLOAD_UNAUTHORIZED = "unauthorized"
UPLOAD_ERROR = "error"
UPLOAD_TOO_LARGE = "too_large"

_UPLOAD_TIMEOUT = 30.0
_MAX_RATE_PER_SEC = 5.0
_SPOOL_MAX_BYTES = 64 * 1024 * 1024  # 64 MiB
_SPOOL_MAX_DAYS = 7
_BACKOFF_BASE = 30.0
_BACKOFF_MAX = 3600.0


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _stable_part_id(incident_id: str, artifact: str, part_index: int) -> str:
    raw = f"{incident_id}|{artifact}|{part_index}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:32]


def encode_artifact(data: bytes) -> tuple[str, str]:
    """Return (encoding, payload_str). Prefer utf-8 text; else base64."""
    try:
        text = data.decode("utf-8")
        # Round-trip check for lone surrogates / weirdness
        if text.encode("utf-8") == data:
            return "utf-8", text
    except UnicodeDecodeError:
        pass
    return "base64", base64.b64encode(data).decode("ascii")


def decode_artifact(encoding: str, payload: str) -> bytes:
    if encoding == "utf-8":
        return payload.encode("utf-8")
    if encoding == "base64":
        return base64.b64decode(payload.encode("ascii"))
    raise ValueError(f"unknown encoding: {encoding}")


def split_artifact_parts(
    incident_id: str,
    artifact_name: str,
    data: bytes,
    *,
    budget: int = _CHUNK_PAYLOAD_BUDGET,
) -> list[dict[str, Any]]:
    """Split one artifact into one or more uploadable chunk records.

    Each returned dict is a full ``artifact_chunk`` body (without outer envelope
    schema_version) whose JSON serialization fits under CLIENT_RECORD_MAX_BYTES
    when wrapped by the uploader.
    """
    if budget < 1024:
        budget = 1024
    digest = _sha256_hex(data)
    total_bytes = len(data)
    # Binary-search-ish: start with whole file, then bisect by raw byte slices.
    # Encoding expands size (esp. base64 ~4/3), so we measure serialized size.
    parts: list[bytes] = []
    offset = 0
    while offset < total_bytes:
        # Grow slice until serialized form would exceed budget.
        lo, hi = 1, total_bytes - offset
        best = 1
        while lo <= hi:
            mid = (lo + hi) // 2
            piece = data[offset : offset + mid]
            enc, payload = encode_artifact(piece)
            probe = {
                "kind": KIND,
                "record_type": "artifact_chunk",
                "incident_id": incident_id,
                "artifact": artifact_name,
                "part_index": 0,
                "part_total": 1,
                "part_id": "x" * 32,
                "sha256": digest,
                "artifact_bytes": total_bytes,
                "encoding": enc,
                "data": payload,
            }
            size = len(json.dumps(probe, ensure_ascii=False).encode("utf-8"))
            if size <= budget:
                best = mid
                lo = mid + 1
            else:
                hi = mid - 1
        # If even 1 byte won't fit (pathological), force base64 of 1 byte.
        piece = data[offset : offset + max(1, best)]
        parts.append(piece)
        offset += len(piece)

    part_total = len(parts)
    out: list[dict[str, Any]] = []
    for idx, piece in enumerate(parts):
        enc, payload = encode_artifact(piece)
        out.append({
            "kind": KIND,
            "record_type": "artifact_chunk",
            "incident_id": incident_id,
            "artifact": artifact_name,
            "part_index": idx,
            "part_total": part_total,
            "part_id": _stable_part_id(incident_id, artifact_name, idx),
            "sha256": digest,
            "artifact_bytes": total_bytes,
            "encoding": enc,
            "data": payload,
        })
    return out


def build_records(snapshot: dict[str, Any], *, username: str, app_version: str,
                  upstream_sha: str) -> list[dict[str, Any]]:
    """Turn a debug_logger snapshot into ordered upload records (manifest first)."""
    artifacts: dict[str, bytes] = dict(snapshot.get("artifacts") or {})
    artifact_hashes = {name: _sha256_hex(blob) for name, blob in artifacts.items()}
    artifact_sizes = {name: len(blob) for name, blob in artifacts.items()}

    chunk_records: list[dict[str, Any]] = []
    for name, blob in artifacts.items():
        chunk_records.extend(
            split_artifact_parts(snapshot["incident_id"], name, blob)
        )

    # Group part counts per artifact for the manifest.
    parts_by_artifact: dict[str, int] = {}
    for rec in chunk_records:
        parts_by_artifact[rec["artifact"]] = rec["part_total"]

    manifest = {
        "kind": KIND,
        "record_type": "manifest",
        "part_id": _stable_part_id(snapshot["incident_id"], "__manifest__", 0),
        "incident_id": snapshot["incident_id"],
        "ts": int(snapshot.get("ts") or time.time()),
        "username": username or "unknown",
        "app_version": app_version or "unknown",
        "upstream_sha": upstream_sha or "unknown",
        "path": snapshot.get("path") or "",
        "model": snapshot.get("model") or "unknown",
        "stream": snapshot.get("stream"),
        "status_code": snapshot.get("status_code"),
        "gateway_status": snapshot.get("gateway_status"),
        "upstream_status": snapshot.get("upstream_status"),
        "source": snapshot.get("source") or "unknown",
        "code": snapshot.get("code") or "unknown",
        "phase": snapshot.get("phase") or "unknown",
        "client_disconnected": bool(snapshot.get("client_disconnected")),
        "error_message": str(snapshot.get("error_message") or "")[:2000],
        "duration_ms": int(snapshot.get("duration_ms") or 0),
        "artifact_names": sorted(artifacts.keys()),
        "artifact_bytes": artifact_sizes,
        "artifact_sha256": artifact_hashes,
        "artifact_parts": parts_by_artifact,
        "total_parts": len(chunk_records),
    }
    return [manifest, *chunk_records]


class PendingErrorStore:
    """JSONL spool for failed incident upload records."""

    def __init__(self, path: Path, *, max_bytes: int = _SPOOL_MAX_BYTES,
                 max_days: int = _SPOOL_MAX_DAYS) -> None:
        self.path = Path(path)
        self.max_bytes = max_bytes
        self.max_days = max_days
        self._lock = threading.Lock()

    def append(self, records: list[dict[str, Any]]) -> None:
        if not records:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            with open(self.path, "a", encoding="utf-8") as f:
                for rec in records:
                    row = dict(rec)
                    row["_spooled_at"] = int(time.time())
                    f.write(json.dumps(row, ensure_ascii=False) + "\n")
            self._enforce_limits_unlocked()

    def load_all(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        out: list[dict[str, Any]] = []
        with self._lock:
            try:
                text = self.path.read_text(encoding="utf-8")
            except Exception:
                return []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                continue
        return out

    def replace_all(self, records: list[dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            tmp = self.path.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                for rec in records:
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            tmp.replace(self.path)
            self._enforce_limits_unlocked()

    def clear(self) -> None:
        with self._lock:
            try:
                if self.path.exists():
                    self.path.unlink()
            except Exception:
                pass

    def _enforce_limits_unlocked(self) -> None:
        if not self.path.exists():
            return
        try:
            # Drop stale by age, then trim oldest if over size (keep newest).
            rows = []
            cutoff = time.time() - self.max_days * 86400
            for line in self.path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                if int(row.get("_spooled_at") or 0) >= cutoff:
                    rows.append(row)
            # Newest last in file; keep from the end if oversized.
            encoded = [json.dumps(r, ensure_ascii=False) for r in rows]
            total = sum(len(e.encode("utf-8")) + 1 for e in encoded)
            if total > self.max_bytes and encoded:
                kept: list[str] = []
                size = 0
                for e in reversed(encoded):
                    n = len(e.encode("utf-8")) + 1
                    if size + n > self.max_bytes and kept:
                        break
                    kept.append(e)
                    size += n
                encoded = list(reversed(kept))
            with open(self.path, "w", encoding="utf-8") as f:
                for e in encoded:
                    f.write(e + "\n")
        except Exception:
            logger.debug("incident_report: spool trim failed", exc_info=True)


@dataclass
class IncidentConfig:
    endpoint_url: str = ""
    secret: str = ""
    username: str = "unknown"
    app_version: str = "unknown"
    upstream_sha: str = "unknown"
    provision_url: str = ""
    shared_secret: str = ""

    @property
    def enabled(self) -> bool:
        return bool(self.endpoint_url)

    @property
    def can_refresh(self) -> bool:
        return bool(self.provision_url and self.shared_secret and self.username)


def config_from_env(env: dict[str, str] | None = None) -> IncidentConfig:
    e = env if env is not None else os.environ
    return IncidentConfig(
        endpoint_url=(e.get("INCIDENT_URL") or "").strip(),
        secret=(e.get("TELEMETRY_SECRET") or "").strip(),
        username=(e.get("TELEMETRY_USERNAME") or "unknown").strip() or "unknown",
        app_version=(e.get("APP_VERSION") or "unknown").strip() or "unknown",
        upstream_sha=(e.get("GATEWAY_UPSTREAM_SHA") or "unknown").strip() or "unknown",
        provision_url=(e.get("TELEMETRY_PROVISION_URL") or "").strip(),
        shared_secret=(e.get("TELEMETRY_SHARED_SECRET") or "").strip(),
    )


class IncidentUploader:
    def __init__(self, endpoint_url: str, secret: str, *, timeout: float = _UPLOAD_TIMEOUT) -> None:
        self.endpoint_url = endpoint_url
        self.secret = secret
        self.timeout = timeout

    def upload_record(self, record: dict[str, Any]) -> str:
        if not self.endpoint_url:
            return UPLOAD_ERROR
        body = {"schema_version": SCHEMA_VERSION, "record": record}
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
        if len(raw) > CLIENT_RECORD_MAX_BYTES:
            return UPLOAD_TOO_LARGE
        headers = {"Content-Type": "application/json"}
        if self.secret:
            headers["Authorization"] = f"Bearer {self.secret}"
        try:
            resp = httpx.post(
                self.endpoint_url,
                content=raw,
                headers=headers,
                timeout=self.timeout,
                proxy=resolve_proxy(),
            )
        except Exception:
            logger.debug("incident_report: upload failed", exc_info=True)
            return UPLOAD_ERROR
        if resp.status_code == 200:
            return UPLOAD_OK
        if resp.status_code == 401:
            return UPLOAD_UNAUTHORIZED
        if resp.status_code == 413:
            return UPLOAD_TOO_LARGE
        logger.debug("incident_report: upload rejected HTTP {}", resp.status_code)
        return UPLOAD_ERROR


class IncidentReporter:
    """Queue + background worker that uploads incident records."""

    def __init__(
        self,
        config: IncidentConfig,
        uploader: IncidentUploader,
        pending: PendingErrorStore,
        refresher: Any = None,
        on_secret_refresh: Any = None,
    ) -> None:
        self.config = config
        self.uploader = uploader
        self.pending = pending
        self.refresher = refresher
        self.on_secret_refresh = on_secret_refresh
        self._queue: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_upload = 0.0
        self._backoff = _BACKOFF_BASE
        self._next_pending_retry = 0.0

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="incident-upload", daemon=True
        )
        self._thread.start()

    def stop(self, *, timeout: float = 2.0) -> None:
        self._stop.set()
        self._wake.set()
        t = self._thread
        if t and t.is_alive():
            t.join(timeout=timeout)

    def enqueue_snapshot(self, snapshot: dict[str, Any]) -> None:
        """Called from the debug_logger callback (request thread). Never raises."""
        try:
            if not self.config.enabled:
                return
            records = build_records(
                snapshot,
                username=self.config.username,
                app_version=self.config.app_version,
                upstream_sha=self.config.upstream_sha,
            )
            with self._lock:
                self._queue.extend(records)
            self._wake.set()
        except Exception:
            logger.debug("incident_report: enqueue failed", exc_info=True)

    def _loop(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(timeout=5.0)
            self._wake.clear()
            try:
                self.tick()
            except Exception:
                logger.debug("incident_report: tick failed", exc_info=True)

    def tick(self, *, now: float | None = None) -> None:
        ts = time.time() if now is None else now
        while True:
            with self._lock:
                if not self._queue:
                    break
                record = self._queue.pop(0)
            self._upload_one(record, now=ts)
        if ts >= self._next_pending_retry:
            self._retry_pending(now=ts)

    def _rate_limit(self, now: float) -> None:
        min_gap = 1.0 / _MAX_RATE_PER_SEC
        gap = now - self._last_upload
        if gap < min_gap:
            time.sleep(min_gap - gap)

    def _upload_one(self, record: dict[str, Any], *, now: float) -> None:
        self._rate_limit(now)
        result = self.uploader.upload_record(record)
        self._last_upload = time.time()
        if result == UPLOAD_OK:
            self._backoff = _BACKOFF_BASE
            return
        if result == UPLOAD_UNAUTHORIZED and self.refresher is not None:
            new_secret = self.refresher.maybe_refresh(now=now)
            if new_secret:
                self.uploader.secret = new_secret
                self.config.secret = new_secret
                if self.on_secret_refresh:
                    try:
                        self.on_secret_refresh(new_secret)
                    except Exception:
                        pass
                result = self.uploader.upload_record(record)
                self._last_upload = time.time()
                if result == UPLOAD_OK:
                    self._backoff = _BACKOFF_BASE
                    return
        if result == UPLOAD_TOO_LARGE:
            # Drop oversized records rather than blocking the spool forever.
            logger.warning(
                "incident_report: dropping oversized record incident={} artifact={}",
                record.get("incident_id"),
                record.get("artifact") or record.get("record_type"),
            )
            return
        self.pending.append([record])
        self._next_pending_retry = time.time() + self._backoff
        self._backoff = min(_BACKOFF_MAX, self._backoff * 2)

    def _retry_pending(self, *, now: float) -> None:
        rows = self.pending.load_all()
        if not rows:
            return
        # Newest first (LIFO): prefer fresh incidents.
        rows = list(reversed(rows))
        remaining: list[dict[str, Any]] = []
        for row in rows:
            clean = {k: v for k, v in row.items() if not k.startswith("_")}
            self._rate_limit(now)
            result = self.uploader.upload_record(clean)
            self._last_upload = time.time()
            if result == UPLOAD_OK:
                continue
            if result == UPLOAD_TOO_LARGE:
                continue
            remaining.append(row)
            # Stop on first failure so we don't hammer; restore untried + failed.
            idx = rows.index(row)
            remaining.extend(rows[idx + 1:])
            break
        # Restore chronological order in spool (oldest first).
        remaining = list(reversed(remaining))
        if remaining:
            self.pending.replace_all(remaining)
            self._next_pending_retry = now + self._backoff
            self._backoff = min(_BACKOFF_MAX, self._backoff * 2)
        else:
            self.pending.clear()
            self._backoff = _BACKOFF_BASE


_reporter: IncidentReporter | None = None


def get_reporter() -> IncidentReporter | None:
    return _reporter


def build_reporter(config: IncidentConfig, data_dir: Path) -> IncidentReporter:
    from . import telemetry as tel

    uploader = IncidentUploader(config.endpoint_url, config.secret)
    pending = PendingErrorStore(Path(data_dir) / "incidents" / "pending-errors.jsonl")
    refresher = None
    on_secret_refresh = None
    if config.can_refresh:
        def _do_refresh() -> str:
            from . import provision
            return provision.refresh_telemetry_secret(
                config.provision_url, config.shared_secret, config.username
            )
        refresher = tel.SecretRefresher(_do_refresh)
        on_secret_refresh = tel._persist_refreshed_secret
    return IncidentReporter(
        config, uploader, pending,
        refresher=refresher,
        on_secret_refresh=on_secret_refresh,
    )


def install(env: dict[str, str] | None = None, data_dir: Path | None = None) -> IncidentReporter | None:
    """Wire snapshot callback into vendor debug_logger. Returns reporter or None."""
    global _reporter
    config = config_from_env(env)
    if not config.enabled:
        return None
    if data_dir is None:
        from . import paths
        data_dir = paths.data_dir()
    reporter = build_reporter(config, data_dir)
    reporter.start()

    def _on_snapshot(snapshot: dict[str, Any]) -> None:
        reporter.enqueue_snapshot(snapshot)

    try:
        from kiro.debug_logger import set_error_snapshot_callback
        set_error_snapshot_callback(_on_snapshot)
    except Exception:
        logger.debug("incident_report: failed to register debug_logger callback", exc_info=True)
        return None

    _reporter = reporter
    logger.info(
        "incident_report: enabled → {} (user={})",
        config.endpoint_url,
        config.username,
    )
    return reporter
