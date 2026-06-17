# app/tests/test_updates.py
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
