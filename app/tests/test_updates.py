# app/tests/test_updates.py
import json
import time

from kiro_gateway_tray import updates


def test_parse_version_strips_prefix():
    assert updates._parse_version("app-v0.2.0") == (0, 2, 0)
    assert updates._parse_version("v1.2.3") == (1, 2, 3)
    assert updates._parse_version("0.1.0") == (0, 1, 0)


def test_is_newer():
    assert updates._is_newer("0.1.0", "0.2.0") is True
    assert updates._is_newer("0.2.0", "0.2.0") is False
    assert updates._is_newer("0.2.0", "0.1.9") is False
    assert updates._is_newer("0.1.0", "1.0.0") is True


def test_cache_roundtrip_and_ttl(tmp_path, monkeypatch):
    monkeypatch.setenv("KIRO_GATEWAY_TRAY_HOME", str(tmp_path))
    # No cache yet -> should_check True
    assert updates._should_check() is True
    updates._write_cache(latest="0.2.0")
    # Just wrote -> within TTL -> should_check False
    assert updates._should_check() is False
    cached = updates._read_cache()
    assert cached["latest"] == "0.2.0"


def test_check_uses_cache_when_fresh(tmp_path, monkeypatch):
    monkeypatch.setenv("KIRO_GATEWAY_TRAY_HOME", str(tmp_path))
    updates._write_cache(latest="9.9.9")
    # fresh cache -> no HTTP call, returns cached latest
    def _boom(*a, **k):
        raise AssertionError("should not hit network when cache is fresh")
    monkeypatch.setattr(updates.httpx, "get", _boom)
    info = updates.check(current="0.1.0")
    assert info.latest == "9.9.9"
    assert info.update_available is True


def test_ttl_is_four_hours():
    # Update checks are throttled to once per 4 hours.
    assert updates._TTL_SECONDS == 4 * 60 * 60


def test_peek_cached_no_file(tmp_path, monkeypatch):
    monkeypatch.setenv("KIRO_GATEWAY_TRAY_HOME", str(tmp_path))
    assert updates.peek_cached(current="0.1.0") is None


def test_peek_cached_newer_version(tmp_path, monkeypatch):
    monkeypatch.setenv("KIRO_GATEWAY_TRAY_HOME", str(tmp_path))
    updates._write_cache(latest="v9.9.9")
    info = updates.peek_cached(current="0.1.0")
    assert info is not None
    assert info.latest == "v9.9.9"
    assert info.update_available is True


def test_peek_cached_same_version(tmp_path, monkeypatch):
    monkeypatch.setenv("KIRO_GATEWAY_TRAY_HOME", str(tmp_path))
    updates._write_cache(latest="v0.2.0")
    assert updates.peek_cached(current="0.2.0") is None


def test_failed_fetch_bumps_checked_at(tmp_path, monkeypatch):
    # A failed fetch (e.g. 403 rate limit) must still mark the cache as checked
    # so we don't retry on every menu open and exhaust the API budget.
    monkeypatch.setenv("KIRO_GATEWAY_TRAY_HOME", str(tmp_path))
    updates._write_cache(latest="0.1.0")
    # Force the next call to attempt a fetch, then make the fetch "fail".
    monkeypatch.setattr(updates, "_should_check", lambda: True)
    monkeypatch.setattr(updates, "_fetch_latest", lambda: None)

    before = time.time()
    updates.check(current="0.1.0")
    cached = updates._read_cache()
    assert cached["checked_at"] >= before
    # Restore real _should_check; cache should now look fresh.
    monkeypatch.undo()
    monkeypatch.setenv("KIRO_GATEWAY_TRAY_HOME", str(tmp_path))
    assert updates._should_check() is False


def test_upgrade_forces_recheck(tmp_path, monkeypatch):
    # Cache written by an older app version must trigger a fresh check even
    # within the TTL window.
    monkeypatch.setenv("KIRO_GATEWAY_TRAY_HOME", str(tmp_path))
    updates._write_cache(latest="0.1.0")
    cached = updates._read_cache()
    cached["app_version"] = "0.0.1"
    updates._cache_file().write_text(json.dumps(cached), encoding="utf-8")
    assert updates._should_check() is True


def test_version_status_no_cache_has_no_latest(tmp_path, monkeypatch):
    monkeypatch.setenv("KIRO_GATEWAY_TRAY_HOME", str(tmp_path))
    st = updates.version_status(current="0.1.0")
    assert st.latest is None
    assert st.upgradable is False


def test_version_status_upgradable(tmp_path, monkeypatch):
    monkeypatch.setenv("KIRO_GATEWAY_TRAY_HOME", str(tmp_path))
    updates._write_cache(latest="v9.9.9")
    st = updates.version_status(current="0.1.0")
    assert st.latest == "v9.9.9"
    assert st.upgradable is True


def test_version_status_not_upgradable_when_ahead_of_release(tmp_path, monkeypatch):
    # Cache left over from before an upgrade points at an older release than
    # the running app. Must not read as upgradable (no "高于发布版" confusion).
    monkeypatch.setenv("KIRO_GATEWAY_TRAY_HOME", str(tmp_path))
    updates._write_cache(latest="v0.1.17")
    st = updates.version_status(current="0.1.23")
    assert st.latest == "v0.1.17"
    assert st.upgradable is False
