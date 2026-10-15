"""Turn raw audit spool files into deduplicated, compressed batches.

The gateway child writes ``spool/*.ready``. This module (tray process) strips
media, splits messages into content-addressed blobs, and every
``rotate_seconds`` — or at a UTC midnight — seals the open batch with zstd.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

import zstandard

from . import appconfig, paths
from .audit_control import atomic_write, audit_dir, load_state, publish_control, save_state
from .audit_payload import split_request_bytes
from .httpclient import local_client
from .log import logger

_USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
_BATCH_RE = re.compile(r"^[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}$")
# IAM Identity Center user id: d-<10 hex>.<uuid>
_KIRO_USER_ID_RE = re.compile(
    r"^d-[0-9a-f]{10}\.[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
_IDENTITY_INTERVAL = 3600.0

Clock = Callable[[], float]
Fetcher = Callable[[], tuple[str | None, str | None]]


def safe_username(name: str | None) -> str:
    """Return ``name`` when it is a tunnel slug, otherwise ``unknown``."""
    text = (name or "").strip().lower()
    if _USERNAME_RE.fullmatch(text):
        return text
    return "unknown"


def partition_owner(user_id: str | None, username: str | None) -> str:
    """Directory name for one batch: the IdC user id, or the machine slug.

    A missing or unrecognized ``userId`` falls back to ``username`` so a
    failed ``/usage`` call still produces a path the worker will accept.
    """
    text = (user_id or "").strip().lower()
    if _KIRO_USER_ID_RE.fullmatch(text):
        return text
    return safe_username(username)


def default_username() -> str:
    """The same per-machine slug the tunnel uses. Never raises."""
    try:
        from . import provision
        return safe_username(provision._get_username(appconfig.load()))
    except Exception:
        logger.debug("audit pipeline: username lookup failed", exc_info=True)
        return "unknown"


def parse_user_info(payload: Any) -> tuple[str | None, str | None]:
    """Pull ``userId`` / ``email`` out of a raw ``getUsageLimits`` body.

    Missing or empty fields become None. ``email`` is null for enterprise
    IdC accounts; ``userId`` (``d-xxxxxxxxxx.<uuid>``) is the stable key.
    """
    if not isinstance(payload, dict):
        return None, None
    info = payload.get("userInfo")
    if not isinstance(info, dict):
        return None, None
    user_id = info.get("userId")
    email = info.get("email")
    if not isinstance(user_id, str) or not user_id:
        user_id = None
    if not isinstance(email, str) or not email:
        email = None
    return user_id, email


def fetch_kiro_identity(timeout: float = 5.0) -> tuple[str | None, str | None]:
    """Read identity from the local gateway's ``GET /usage?raw=true``.

    Raises:
        RuntimeError: The gateway did not return 200. Callers back off; a
            missing ``userInfo`` is not an error and comes back as nulls.
    """
    cfg = appconfig.load()
    url = f"{appconfig.gateway_origin(cfg)}/usage?raw=true"
    headers = {"Authorization": f"Bearer {cfg.gateway.proxy_api_key}"}
    with local_client(timeout=timeout) as client:
        response = client.get(url, headers=headers)
    if response.status_code != 200:
        raise RuntimeError(f"/usage returned {response.status_code}")
    return parse_user_info(response.json())


def _compressor() -> zstandard.ZstdCompressor:
    params = zstandard.ZstdCompressionParameters.from_level(10, window_log=27, enable_ldm=1)
    return zstandard.ZstdCompressor(compression_params=params)


def _utc_day(ts: float) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(ts))


def _partition_day(day: str) -> str:
    """``YYYY-MM-DD`` to the outbox directory name ``YY-MM-DD``."""
    return day[2:]


def _stamp(ts: float) -> str:
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(ts))


def _chmod(path: Path, mode: int) -> None:
    if os.name != "posix":
        return
    try:
        os.chmod(path, mode)
    except OSError:
        pass


def directory_size(path: Path) -> int:
    if not path.exists():
        return 0
    total = 0
    for item in path.rglob("*"):
        if item.is_file():
            try:
                total += item.stat().st_size
            except OSError:
                continue
    return total


class AuditPipeline:
    """Consume ``.ready`` files into the open batch and seal it on a timer.

    ``background=False`` leaves the caller in charge of :meth:`tick`, which is
    how the tests drive a fake clock.
    """

    def __init__(
        self,
        root: Path | None = None,
        *,
        clock: Clock | None = None,
        username_provider: Callable[[], str] | None = None,
        config_provider: Callable[[], appconfig.AppCfg] | None = None,
        identity_fetcher: Fetcher | None = None,
        poll_seconds: float = 2.0,
    ) -> None:
        self._root_override = root
        self.clock = clock or time.time
        self.username_provider = username_provider or default_username
        self.config_provider = config_provider or appconfig.load
        self.identity_fetcher = identity_fetcher or fetch_kiro_identity
        self.poll_seconds = poll_seconds
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._batch_name: str | None = None
        self._batch_started = 0.0
        self._req: Any = None
        self._blob: Any = None
        self._seen: set[str] = set()
        self._seen_day = ""
        self._identity: tuple[str | None, str | None] = (None, None)
        self._identity_at = 0.0
        self._identity_attempts = 0
        self.dropped_batches = 0

    def root(self) -> Path:
        return audit_dir(self._root_override if self._root_override is not None else paths.data_dir() / "audit")

    def start(self, *, background: bool = True) -> None:
        """Seal anything a previous run left open, then optionally start the loop.

        A second call does not start another thread.
        """
        with self._lock:
            self._load_identity_cache()
            self._publish()
            self._seal_leftovers()
            self._rebuild_seen(self._today())
        if not background:
            return
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="audit-pipeline", daemon=True)
        self._thread.start()

    def flush_ready(self) -> None:
        """Process queued ``.ready`` files and fsync the open batch. Does not seal."""
        with self._lock:
            self._consume_ready()
            self._fsync_open()

    def tick(self) -> None:
        """One pass: refresh identity, rotate, consume, enforce the disk cap."""
        with self._lock:
            self._publish()
            self._refresh_identity()
            self._seal_if_due()
            self._consume_ready()
            self._seal_if_due()
            self._enforce_limit()

    def open_batch(self) -> str:
        """Start an empty batch. Used by tests; production opens on the first record."""
        with self._lock:
            self._open_batch()
            assert self._batch_name is not None
            return self._batch_name

    def seal(self) -> None:
        """Compress the open batch even when it has no records."""
        with self._lock:
            self._seal_open()

    def thread_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def seen(self) -> set[str]:
        return set(self._seen)

    @property
    def identity_attempts(self) -> int:
        return self._identity_attempts

    def _loop(self) -> None:
        while not self._stop.wait(self.poll_seconds):
            try:
                self.tick()
            except Exception:
                logger.debug("audit pipeline: tick failed", exc_info=True)

    def _today(self) -> str:
        return _utc_day(self.clock())

    def _publish(self) -> None:
        try:
            publish_control(self.root(), self.config_provider(), load_state(self.root()))
        except Exception:
            logger.debug("audit pipeline: control publish failed", exc_info=True)

    def _load_identity_cache(self) -> None:
        state = load_state(self.root())
        self.dropped_batches = int(state.get("dropped_batches") or 0)
        identity = state.get("identity")
        if isinstance(identity, dict):
            self._identity = (
                identity.get("kiro_user_id") if isinstance(identity.get("kiro_user_id"), str) else None,
                identity.get("email") if isinstance(identity.get("email"), str) else None,
            )
            fetched = identity.get("fetched_at") or 0
            self._identity_at = float(fetched) if isinstance(fetched, (int, float)) else 0.0

    def _refresh_identity(self) -> None:
        if self._identity_at and self.clock() - self._identity_at < _IDENTITY_INTERVAL:
            return
        self._identity_attempts += 1
        self._identity_at = self.clock()
        try:
            self._identity = self.identity_fetcher()
        except Exception:
            logger.debug("audit pipeline: identity refresh failed", exc_info=True)
            self._store_identity()
            return
        self._store_identity()

    def _store_identity(self) -> None:
        state = load_state(self.root())
        state["identity"] = {
            "kiro_user_id": self._identity[0],
            "email": self._identity[1],
            "fetched_at": self._identity_at,
        }
        state["dropped_batches"] = self.dropped_batches
        try:
            save_state(self.root(), state)
        except Exception:
            logger.debug("audit pipeline: state save failed", exc_info=True)

    def _dirs(self) -> tuple[Path, Path, Path]:
        root = self.root()
        spool = root / "spool"
        current = root / "current"
        outbox = root / "outbox"
        for directory in (spool, current, outbox):
            directory.mkdir(parents=True, exist_ok=True)
            _chmod(directory, 0o700)
        return spool, current, outbox

    def _seal_leftovers(self) -> None:
        _spool, current, _outbox = self._dirs()
        names = []
        for path in current.glob("*.req.jsonl"):
            name = path.name[: -len(".req.jsonl")]
            if _BATCH_RE.fullmatch(name):
                names.append(name)
        for name in sorted(names):
            self._compress_batch(name)

    def _rebuild_seen(self, day: str) -> None:
        """Load today's blob hashes. A corrupt file is skipped, not fatal."""
        self._seen = set()
        self._seen_day = day
        _spool, current, outbox = self._dirs()
        paths_to_read: list[tuple[Path, bool]] = []
        day_dir = outbox / _partition_day(day)
        if day_dir.exists():
            for path in day_dir.rglob("*.blob.jsonl.zst"):
                paths_to_read.append((path, True))
        for path in current.glob("*.blob.jsonl"):
            paths_to_read.append((path, False))
        for path, compressed in paths_to_read:
            try:
                self._seen.update(self._hashes_in(path, compressed))
            except Exception:
                logger.warning("audit pipeline: skipping unreadable blob file {}", path)

    def _hashes_in(self, path: Path, compressed: bool) -> set[str]:
        if compressed:
            dctx = zstandard.ZstdDecompressor()
            with path.open("rb") as handle:
                with dctx.stream_reader(handle) as reader:
                    raw = reader.read()
        else:
            raw = path.read_bytes()
        found = set()
        for line in raw.decode("utf-8", errors="replace").splitlines():
            if not line:
                continue
            record = json.loads(line)
            digest = record.get("h") if isinstance(record, dict) else None
            if isinstance(digest, str):
                found.add(digest)
        return found

    def _seal_if_due(self) -> None:
        if self._batch_name is None:
            return
        elapsed = self.clock() - self._batch_started
        crossed = _utc_day(self._batch_started) != self._today()
        rotate = self._rotate_seconds()
        if crossed or elapsed >= rotate:
            self._seal_open()
            if crossed:
                self._rebuild_seen(self._today())

    def _rotate_seconds(self) -> float:
        try:
            value = int(self.config_provider().audit.rotate_seconds)
        except Exception:
            return 600
        return value if value > 0 else 600

    def _max_bytes(self) -> int:
        try:
            mb = int(self.config_provider().audit.max_spool_mb)
        except Exception:
            mb = 1024
        if mb <= 0:
            mb = 1024
        return mb * 1024 * 1024

    def _open_batch(self) -> None:
        if self._batch_name is not None:
            return
        _spool, current, _outbox = self._dirs()
        started = self.clock()
        name = f"{_stamp(started)}-{uuid.uuid4().hex[:8]}"
        self._batch_name = name
        self._batch_started = started
        if self._seen_day != _utc_day(started):
            self._rebuild_seen(_utc_day(started))
        self._req = open(current / f"{name}.req.jsonl", "a", encoding="utf-8")
        self._blob = open(current / f"{name}.blob.jsonl", "a", encoding="utf-8")
        _chmod(current / f"{name}.req.jsonl", 0o600)
        _chmod(current / f"{name}.blob.jsonl", 0o600)

    def _fsync_open(self) -> None:
        for handle in (self._blob, self._req):
            if handle is None:
                continue
            handle.flush()
            os.fsync(handle.fileno())

    def _seal_open(self) -> None:
        name = self._batch_name
        if name is None:
            return
        self._fsync_open()
        for handle in (self._blob, self._req):
            if handle is not None:
                handle.close()
        self._blob = None
        self._req = None
        self._batch_name = None
        self._compress_batch(name)

    def _compress_batch(self, name: str) -> None:
        _spool, current, outbox = self._dirs()
        if not _BATCH_RE.fullmatch(name):
            return
        day = _partition_day(f"{name[:4]}-{name[4:6]}-{name[6:8]}")
        user = partition_owner(self._identity[0], self.username_provider())
        day_dir = outbox / day
        dest_dir = day_dir / user
        dest_dir.mkdir(parents=True, exist_ok=True)
        _chmod(day_dir, 0o700)
        _chmod(dest_dir, 0o700)
        for kind in ("blob", "req"):
            src = current / f"{name}.{kind}.jsonl"
            if not src.exists():
                src.write_text("", encoding="utf-8")
            dest = dest_dir / f"{name}.{kind}.jsonl.zst"
            self._compress_file(src, dest)
            try:
                src.unlink()
            except OSError:
                logger.debug("audit pipeline: could not remove {}", src, exc_info=True)

    def _compress_file(self, src: Path, dest: Path) -> None:
        tmp = dest.with_suffix(dest.suffix + ".tmp")
        compressor = _compressor()
        with src.open("rb") as source, tmp.open("wb") as target:
            compressor.copy_stream(source, target)
            target.flush()
            os.fsync(target.fileno())
        os.replace(tmp, dest)
        _chmod(dest, 0o600)

    def _consume_ready(self) -> None:
        spool, _current, _outbox = self._dirs()
        ready = sorted(spool.glob("*.ready"))
        for path in ready:
            try:
                self._consume_one(path)
            except Exception:
                logger.warning("audit pipeline: failed to consume {}", path, exc_info=True)

    def _consume_one(self, path: Path) -> None:
        raw = path.read_bytes()
        if raw and not raw.endswith(b"\n"):
            raw = raw.rsplit(b"\n", 1)[0]
        text = raw.decode("utf-8", errors="replace")
        for line in text.splitlines():
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                logger.warning("audit pipeline: dropping malformed spool line in {}", path.name)
                continue
            if not isinstance(record, dict):
                continue
            self._write_record(record)
        self._fsync_open()
        path.unlink()

    def _write_record(self, record: dict) -> None:
        body = record.pop("req_body", "")
        if not isinstance(body, str):
            body = ""
        req, dropped, new_blobs = split_request_bytes(body.encode("utf-8"))
        record["kiro_user_id"] = self._identity[0]
        record["email"] = self._identity[1]
        record["req"] = req
        record["dropped"] = dropped
        self._open_batch()
        assert self._blob is not None and self._req is not None
        for digest, value in new_blobs.items():
            if digest in self._seen:
                continue
            blob_line = json.dumps({"h": digest, "body": value}, ensure_ascii=False, separators=(",", ":"))
            self._blob.write(blob_line + "\n")
            self._seen.add(digest)
        req_line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        self._req.write(req_line + "\n")

    def _usage_bytes(self) -> int:
        root = self.root()
        return directory_size(root / "spool") + directory_size(root / "current") + directory_size(root / "outbox")

    def _enforce_limit(self) -> None:
        limit = self._max_bytes()
        while self._usage_bytes() > limit:
            oldest = self._oldest_batch()
            if oldest is None:
                logger.warning(
                    "audit spool is {} bytes over the {} byte cap and no sealed batch can be dropped",
                    self._usage_bytes(), limit,
                )
                break
            for path in oldest:
                try:
                    path.unlink()
                except OSError:
                    logger.debug("audit pipeline: could not delete {}", path, exc_info=True)
            self.dropped_batches += 1
            state = load_state(self.root())
            state["dropped_batches"] = self.dropped_batches
            save_state(self.root(), state)
            logger.warning("audit dropped oldest batch {} (total dropped {})", oldest[0].parent.name, self.dropped_batches)

    def _oldest_batch(self) -> list[Path] | None:
        """Files of the oldest sealed batch, or None when the outbox is empty."""
        _spool, _current, outbox = self._dirs()
        groups: dict[str, list[Path]] = {}
        for path in outbox.rglob("*.jsonl.zst"):
            name = path.name
            for suffix in (".req.jsonl.zst", ".blob.jsonl.zst"):
                if name.endswith(suffix):
                    batch = name[: -len(suffix)]
                    groups.setdefault(batch, []).append(path)
                    break
        if not groups:
            return None
        oldest = sorted(groups)[0]
        return groups[oldest]


def read_zst_lines(path: Path) -> list[str]:
    """Decompress a batch file to text lines. Shared with the stats and rebuild tools."""
    dctx = zstandard.ZstdDecompressor()
    with path.open("rb") as handle:
        with dctx.stream_reader(handle) as reader:
            raw = reader.read()
    return [line for line in raw.decode("utf-8", errors="replace").splitlines() if line]
