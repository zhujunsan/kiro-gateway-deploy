"""Pull the cloud audit switch and upload sealed batches to OSS.

The gateway child never talks to the network for this. Each file gets its own
presigned PUT from ``POST /audit/upload-url``; this process does not hold an
AccessKey. A failed upload stays on disk and is retried with exponential backoff.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any, Callable

import httpx

from . import appconfig, paths
from .announcements import user_agent
from .audit_control import load_state, publish_control, save_state
from .audit_pipeline import default_username, safe_username
from .httpclient import resolve_proxy
from .log import logger

_BACKOFF_BASE = 30.0
_BACKOFF_MAX = 3600.0
_CONFIG_INTERVAL = 600.0
_BATCHES_PER_TICK = 5
_PUT_TIMEOUT = 120.0


class AuditUploader:
    """Background uploader. Tests call :meth:`tick` instead of :meth:`start`."""

    def __init__(
        self,
        root: Path | None = None,
        *,
        clock: Callable[[], float] | None = None,
        config_provider: Callable[[], appconfig.AppCfg] | None = None,
        username_provider: Callable[[], str] | None = None,
        transport: httpx.BaseTransport | None = None,
        poll_seconds: float = 30.0,
    ) -> None:
        self._root_override = root
        self.clock = clock or time.time
        self.config_provider = config_provider or appconfig.load
        self.username_provider = username_provider or default_username
        self._transport = transport
        self.poll_seconds = poll_seconds
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._config_at = 0.0
        self._next_try = 0.0
        self._backoff = _BACKOFF_BASE
        self.upload_attempts = 0

    def root(self) -> Path:
        if self._root_override is not None:
            return self._root_override
        return paths.data_dir() / "audit"

    def thread_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        """Start the loop. A second call while it is alive does nothing."""
        if self.thread_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="audit-upload", daemon=True)
        self._thread.start()

    def tick(self) -> None:
        """Refresh config if due, then upload a few of the oldest batches."""
        with self._lock:
            if self._config_at == 0.0 or self.clock() - self._config_at >= _CONFIG_INTERVAL:
                self._refresh_config()
                self._config_at = self.clock()
            if self.clock() < self._next_try:
                return
            self._upload()

    def _loop(self) -> None:
        while not self._stop.wait(self.poll_seconds):
            try:
                self.tick()
            except Exception:
                logger.debug("audit uploader: tick failed", exc_info=True)

    def _client(self) -> httpx.Client:
        kwargs: dict[str, Any] = {"timeout": _PUT_TIMEOUT}
        if self._transport is not None:
            kwargs["transport"] = self._transport
        else:
            proxy = resolve_proxy()
            if proxy:
                kwargs["proxy"] = proxy
        return httpx.Client(**kwargs)

    def _refresh_config(self) -> None:
        cfg = self.config_provider()
        root = self.root()
        state = load_state(root)
        cloud_mode: str | None = None
        url = (cfg.cloudflare.provision_url or "").strip()
        secret = (cfg.cloudflare.shared_secret or "").strip()
        username = safe_username(self.username_provider())
        if url and secret and username != "unknown":
            endpoint = url.rstrip("/") + "/client-config"
            try:
                with self._client() as client:
                    response = client.post(
                        endpoint,
                        json={"shared_secret": secret, "username": username},
                        headers={"User-Agent": user_agent()},
                    )
                if response.status_code == 200:
                    audit = response.json().get("audit") or {}
                    mode = audit.get("mode")
                    if mode in ("off", "best_effort"):
                        rotate = audit.get("rotate_seconds") or 600
                        state["cloud"] = {
                            "mode": mode,
                            "rotate_seconds": rotate,
                            "fetched_at": self.clock(),
                        }
                        save_state(root, state)
                        cloud_mode = mode
                else:
                    logger.debug("audit config fetch returned {}", response.status_code)
            except Exception:
                logger.debug("audit config fetch failed", exc_info=True)
        try:
            publish_control(root, cfg, state, cloud_mode=cloud_mode)
        except Exception:
            logger.debug("audit control publish failed", exc_info=True)

    def _batches(self) -> list[dict[str, Any]]:
        outbox = self.root() / "outbox"
        if not outbox.exists():
            return []
        groups: dict[str, dict[str, Any]] = {}
        for path in outbox.rglob("*.jsonl.zst"):
            name = path.name
            kind = "req" if name.endswith(".req.jsonl.zst") else "blob" if name.endswith(".blob.jsonl.zst") else ""
            if not kind:
                continue
            batch = name[: -len(f".{kind}.jsonl.zst")]
            slot = groups.setdefault(batch, {"name": batch, "req": None, "blob": None})
            slot[kind] = path
        return [groups[name] for name in sorted(groups)]

    def _object_key(self, path: Path) -> str:
        relative = path.relative_to(self.root() / "outbox").as_posix()
        return f"audit/v1/{relative}"

    def _upload(self) -> None:
        batches = self._batches()[:_BATCHES_PER_TICK]
        pending: list[Path] = []
        for batch in batches:
            for kind in ("blob", "req"):
                path = batch.get(kind)
                if path is not None and path.exists():
                    pending.append(path)
        if not pending:
            return
        cfg = self.config_provider()
        url = (cfg.cloudflare.provision_url or "").rstrip("/")
        secret = (cfg.cloudflare.shared_secret or "").strip()
        username = safe_username(self.username_provider())
        if not url or not secret or username == "unknown":
            return
        keys = [self._object_key(path) for path in pending]
        self.upload_attempts += 1
        try:
            with self._client() as client:
                response = client.post(
                    url + "/audit/upload-url",
                    json={"shared_secret": secret, "username": username, "keys": keys},
                    headers={"User-Agent": user_agent()},
                )
        except Exception:
            logger.debug("audit upload-url failed", exc_info=True)
            self._schedule_retry()
            return
        if response.status_code == 503:
            logger.debug("audit storage is not configured; will retry later")
            self._schedule_retry()
            return
        if response.status_code != 200:
            logger.debug("audit upload-url returned {}", response.status_code)
            self._schedule_retry()
            return
        signed = {item["key"]: item for item in (response.json().get("uploads") or []) if isinstance(item, dict)}
        failed = False
        try:
            with self._client() as client:
                for path in pending:
                    if not path.exists():
                        continue
                    item = signed.get(self._object_key(path))
                    if not item:
                        failed = True
                        break
                    outcome = self._put(client, path, item)
                    if outcome != "deleted":
                        failed = True
                        break
        except Exception:
            logger.debug("audit put failed", exc_info=True)
            failed = True
        if failed:
            self._schedule_retry()
        else:
            self._backoff = _BACKOFF_BASE
            self._next_try = 0.0

    def _put(self, client: httpx.Client, path: Path, item: dict) -> str:
        headers = {
            "content-type": (item.get("headers") or {}).get("content-type", "application/octet-stream"),
            "x-oss-forbid-overwrite": "true",
        }
        response = client.put(item["url"], content=path.read_bytes(), headers=headers)
        if response.status_code in (200, 409):
            path.unlink(missing_ok=True)
            return "deleted"
        logger.debug("audit put {} returned {}", path.name, response.status_code)
        return "retry"

    def _schedule_retry(self) -> None:
        self._next_try = self.clock() + self._backoff
        self._backoff = min(self._backoff * 2, _BACKOFF_MAX)
