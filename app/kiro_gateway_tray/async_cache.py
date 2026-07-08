"""A small thread-safe cache for values fetched off the UI thread.

Used by the tray menu, whose label callbacks are re-evaluated synchronously on
every redraw and must never block. The cached value renders instantly while a
background thread refreshes it, throttled by an optional cooldown.
"""
from __future__ import annotations

import threading
import time
from typing import Callable, Generic, TypeVar

from .log import logger

T = TypeVar("T")


class AsyncRefreshCache(Generic[T]):
    def __init__(
        self,
        fetch: Callable[[], T],
        *,
        cooldown: float = 0.0,
        on_update: Callable[[], None] | None = None,
    ) -> None:
        """
        fetch:     called on a background thread to produce a fresh value.
        cooldown:  minimum seconds between successful refreshes (0 = always).
        on_update: optional callback fired (on the worker thread) after the
                   value changes, e.g. to ask the tray to redraw.
        """
        self._fetch = fetch
        self._cooldown = cooldown
        self._on_update = on_update
        self._lock = threading.Lock()
        self._value: T | None = None
        self._inflight = False
        # Whether a fetch has ever succeeded. Cooldown only applies after the
        # first success so that startup retries are not throttled.
        self._succeeded = False
        self._last_fetch = 0.0
        self._backoff = 0.0  # current backoff delay (reset on success)

    _BACKOFF_BASE = 1.0
    _BACKOFF_MAX = 64.0

    def get(self) -> T | None:
        """Return the last cached value (None until the first refresh lands)."""
        with self._lock:
            return self._value

    @property
    def inflight(self) -> bool:
        with self._lock:
            return self._inflight

    def refresh(self, *, force: bool = False) -> None:
        """Kick a background refresh unless one is in flight or within cooldown."""
        now = time.monotonic()
        with self._lock:
            if self._inflight:
                return
            if not force:
                if self._succeeded and (now - self._last_fetch) < self._cooldown:
                    return
                # A retry is already scheduled via backoff; don't pile on.
                if self._backoff > 0.0:
                    return
            self._inflight = True

        def _work():
            try:
                value = self._fetch()
            except Exception:
                value = None
            with self._lock:
                if value is not None:
                    self._value = value
                    self._succeeded = True
                    self._last_fetch = time.monotonic()
                    self._backoff = 0.0
                self._inflight = False

            if value is not None:
                if self._on_update is not None:
                    try:
                        self._on_update()
                    except Exception:
                        logger.debug("async_cache on_update callback failed", exc_info=True)
            else:
                # Failed — schedule retry with exponential backoff
                with self._lock:
                    delay = self._BACKOFF_BASE if self._backoff == 0.0 else min(self._backoff * 2, self._BACKOFF_MAX)
                    self._backoff = delay
                self._schedule_retry(delay)

        threading.Thread(target=_work, daemon=True).start()

    def _schedule_retry(self, delay: float) -> None:
        def _retry():
            time.sleep(delay)
            self.refresh(force=True)

        threading.Thread(target=_retry, daemon=True).start()
