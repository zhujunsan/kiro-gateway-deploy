"""A small thread-safe cache for values fetched off the UI thread.

Used by the tray menu, whose label callbacks are re-evaluated synchronously on
every redraw and must never block. The cached value renders instantly while a
background thread refreshes it, throttled by an optional cooldown.
"""
from __future__ import annotations

import threading
import time
from typing import Callable, Generic, TypeVar

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
        # Whether any attempt has completed yet. Kept separate from _value so the
        # cooldown also throttles fetches that legitimately yield None/empty, and
        # so the very first refresh is never mistaken for "within cooldown" (the
        # monotonic clock does not start at 0 on macOS/Linux).
        self._fetched = False
        self._last_fetch = 0.0

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
            if (
                not force
                and self._fetched
                and (now - self._last_fetch) < self._cooldown
            ):
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
                self._inflight = False
                self._fetched = True
                self._last_fetch = time.monotonic()
            if value is not None and self._on_update is not None:
                try:
                    self._on_update()
                except Exception:
                    pass

        threading.Thread(target=_work, daemon=True).start()
