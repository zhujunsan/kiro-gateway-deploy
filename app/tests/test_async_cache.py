# app/tests/test_async_cache.py
import threading
import time

from kiro_gateway_tray.async_cache import AsyncRefreshCache


def _wait_until(pred, timeout=2.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(0.01)
    return False


def test_refresh_populates_value_and_fires_callback():
    fired = threading.Event()
    cache = AsyncRefreshCache(lambda: [1, 2, 3], on_update=fired.set)
    assert cache.get() is None
    cache.refresh()
    assert _wait_until(lambda: cache.get() == [1, 2, 3])
    assert fired.is_set()


def test_failed_fetch_keeps_old_value_and_skips_callback():
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            return "good"
        raise RuntimeError("boom")

    updates = {"n": 0}
    cache = AsyncRefreshCache(flaky, on_update=lambda: updates.__setitem__("n", updates["n"] + 1))
    cache.refresh(force=True)
    assert _wait_until(lambda: cache.get() == "good")
    assert updates["n"] == 1
    # second refresh raises -> value retained, callback not fired again
    cache.refresh(force=True)
    assert _wait_until(lambda: not cache.inflight)
    assert cache.get() == "good"
    assert updates["n"] == 1


def test_cooldown_blocks_immediate_refetch():
    calls = {"n": 0}

    def counter():
        calls["n"] += 1
        return calls["n"]

    cache = AsyncRefreshCache(counter, cooldown=60)
    cache.refresh()
    assert _wait_until(lambda: cache.get() == 1)
    # within cooldown, no new fetch
    cache.refresh()
    assert _wait_until(lambda: not cache.inflight)
    assert calls["n"] == 1
    # force bypasses cooldown
    cache.refresh(force=True)
    assert _wait_until(lambda: cache.get() == 2)


def test_cooldown_throttles_even_when_fetch_returns_none():
    # A fetch that legitimately yields None must still arm the cooldown, so the
    # refresh->redraw->refresh menu loop cannot spin a fetch on every redraw.
    calls = {"n": 0}

    def none_fetch():
        calls["n"] += 1
        return None

    cache = AsyncRefreshCache(none_fetch, cooldown=60)
    cache.refresh()
    assert _wait_until(lambda: not cache.inflight)
    assert calls["n"] == 1
    # within cooldown, no new fetch even though value is still None
    cache.refresh()
    assert _wait_until(lambda: not cache.inflight)
    assert calls["n"] == 1


def test_first_refresh_not_swallowed_by_cooldown():
    # monotonic() does not start at 0, so the first refresh must run regardless
    # of cooldown (regression guard for using 0.0 as a "never fetched" sentinel).
    calls = {"n": 0}
    cache = AsyncRefreshCache(lambda: calls.__setitem__("n", calls["n"] + 1) or "v",
                              cooldown=9999)
    cache.refresh()
    assert _wait_until(lambda: cache.get() == "v")
    assert calls["n"] == 1
