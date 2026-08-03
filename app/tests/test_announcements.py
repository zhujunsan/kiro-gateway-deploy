# app/tests/test_announcements.py
"""Tests for the tray announcement client.

Emphasis is on the failure modes that would be visible to a user: a broken or
hostile payload must never crash the tray or produce an unreadable menu row, and
a Worker outage must degrade to "slightly stale" rather than "the bar vanishes".
"""
import json
import time

import httpx
import pytest

from kiro_gateway_tray import announcements
from kiro_gateway_tray.announcements import Announcement


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("KIRO_GATEWAY_TRAY_HOME", str(tmp_path))


@pytest.fixture(autouse=True)
def _no_accidental_network(monkeypatch):
    """Any test that wants HTTP must opt in by patching httpx.post itself."""
    def _boom(*a, **k):
        raise AssertionError("unexpected network call")
    monkeypatch.setattr(announcements.httpx, "post", _boom)


def _cfg(provision_url="https://worker.example.com", shared_secret="s3cret"):
    from kiro_gateway_tray.appconfig import AppCfg
    cfg = AppCfg()
    cfg.cloudflare.provision_url = provision_url
    cfg.cloudflare.shared_secret = shared_secret
    return cfg


def _payload(*items):
    return {"ok": True, "announcements": list(items)}


def _item(**overrides):
    base = {"id": 1, "body": "维护通知", "tag": None, "url": None,
            "level": "info", "priority": 0, "ends_at": None}
    base.update(overrides)
    return base


def _endpoint(cfg=None, username="abc123def456", monkeypatch=None):
    """Resolve an _Endpoint the way check() does, stubbing the username lookup."""
    if monkeypatch is not None:
        monkeypatch.setattr(
            "kiro_gateway_tray.provision._get_username", lambda c: username)
    return announcements._resolve_endpoint(cfg or _cfg())


def _stub_provisioned(monkeypatch):
    """Make check() see a provisioned install with a resolvable username."""
    monkeypatch.setattr(announcements.appconfig, "load", lambda **kw: _cfg())
    monkeypatch.setattr(
        "kiro_gateway_tray.provision._get_username", lambda cfg: "abc123def456")


def _stub_post(monkeypatch, *, json_body=None, status_code=200, raises=None):
    """Install a fake httpx.post and record the request it received."""
    seen = {}

    def _post(url, json=None, headers=None, timeout=None, proxy=None):
        seen["url"] = url
        seen["json"] = json
        seen["headers"] = headers or {}
        seen["timeout"] = timeout
        if raises is not None:
            raise raises
        return httpx.Response(
            status_code,
            json=json_body if json_body is not None else {},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(announcements.httpx, "post", _post)
    return seen


# --- payload parsing ---

def test_parse_items_maps_every_field():
    items = announcements._parse_items([
        _item(id=1, body="有偿升级", tag="限时", url="https://example.com/a",
              level="warning", dimmed=True, ends_at=1234),
    ])
    assert items == [Announcement(
        id=1, body="有偿升级", tag="限时",
        url="https://example.com/a", level="warning", dimmed=True, ends_at=1234,
    )]


@pytest.mark.parametrize("raw,expected", [
    (True, True), (False, False),
    (1, True), (0, False),
    ("1", True), ("true", True), ("yes", True),
    ("0", False), ("false", False), (None, False), ("", False),
])
def test_dimmed_coercion(raw, expected):
    assert announcements._parse_items([_item(dimmed=raw)])[0].dimmed is expected


def test_missing_dimmed_defaults_to_false():
    raw = _item()
    raw.pop("dimmed", None)
    assert announcements._parse_items([raw])[0].dimmed is False


def test_parse_items_drops_entries_without_id_or_body():
    items = announcements._parse_items([
        _item(id=0),
        _item(id=-1),
        _item(id="nope"),
        _item(id=2, body="   "),
        _item(id=3, body=None),
        _item(id=True),
        _item(id=9, body="真正的公告"),
    ])
    assert [i.id for i in items] == [9]


def test_parse_items_tolerates_garbage():
    assert announcements._parse_items(None) == []
    assert announcements._parse_items("nope") == []
    assert announcements._parse_items({}) == []
    assert announcements._parse_items([None, 42, "x", []]) == []


def test_parse_items_caps_at_max_items():
    raw = [_item(id=i + 1, body=f"公告{i}") for i in range(20)]
    items = announcements._parse_items(raw)
    assert len(items) == announcements.MAX_ITEMS
    assert [i.id for i in items] == list(range(1, announcements.MAX_ITEMS + 1))


def test_unknown_level_falls_back_to_info():
    assert announcements._parse_items([_item(level="URGENT")])[0].level == "info"
    assert announcements._parse_items([_item(level=None)])[0].level == "info"
    assert announcements._parse_items([_item(level="critical")])[0].level == "critical"


@pytest.mark.parametrize("bad", [
    "javascript:alert(1)", "file:///etc/passwd", "ftp://example.com/x",
    "example.com", "  ", "", None, 42,
])
def test_non_http_urls_are_rejected(bad):
    """These end up in webbrowser.open, so the client re-checks what the Worker sent."""
    assert announcements._parse_items([_item(url=bad)])[0].url == ""


@pytest.mark.parametrize("good", [
    "https://example.com/notice", "http://example.com", "HTTPS://Example.com/x",
])
def test_http_urls_survive(good):
    assert announcements._parse_items([_item(url=good)])[0].url == good


def test_body_is_flattened_to_one_line():
    """Newlines break the row off macOS; tabs are the gray-suffix delimiter."""
    body = "第一行\n第二行\t带制表符\r\n  多余空格"
    parsed = announcements._parse_items([_item(body=body)])[0]
    assert parsed.body == "第一行 第二行 带制表符 多余空格"
    assert "\n" not in parsed.body
    assert "\t" not in parsed.body


def test_overlong_body_is_truncated_with_ellipsis():
    parsed = announcements._parse_items([_item(body="长" * 500)])[0]
    assert len(parsed.body) == announcements._MAX_BODY_CHARS
    assert parsed.body.endswith("…")


def test_overlong_tag_is_truncated():
    parsed = announcements._parse_items([_item(tag="标" * 100)])[0]
    assert len(parsed.tag) == announcements._MAX_TAG_CHARS


def test_ends_at_accepts_numeric_strings_and_rejects_junk():
    assert announcements._parse_items([_item(ends_at="1700")])[0].ends_at == 1700
    assert announcements._parse_items([_item(ends_at="soon")])[0].ends_at is None
    assert announcements._parse_items([_item(ends_at=None)])[0].ends_at is None


# --- menu rendering ---

def test_menu_title_uses_level_emoji():
    assert announcements.menu_title(
        Announcement(id=1, body="普通")).startswith("📢 ")
    assert announcements.menu_title(
        Announcement(id=1, body="警告", level="warning")).startswith("⚠️ ")
    assert announcements.menu_title(
        Announcement(id=1, body="严重", level="critical")).startswith("🚨 ")


def test_menu_title_appends_tag_as_gray_suffix():
    title = announcements.menu_title(Announcement(id=1, body="维护", tag="限时"))
    assert title == "📢 维护\t限时"


def test_menu_title_without_tag_has_no_tab():
    assert "\t" not in announcements.menu_title(Announcement(id=1, body="维护"))


# --- expiry ---

def test_peek_cached_drops_expired_entries():
    now = time.time()
    announcements._write_cache([
        Announcement(id=1, body="进行中", ends_at=int(now) + 60),
        Announcement(id=2, body="已结束", ends_at=int(now) - 60),
        Announcement(id=3, body="长期"),
    ])
    assert [i.id for i in announcements.peek_cached()] == [1, 3]


def test_peek_cached_expiry_boundary_is_exclusive():
    announcements._write_cache([Announcement(id=1, body="b", ends_at=1000)])
    assert [i.id for i in announcements.peek_cached(now=999)] == [1]
    assert announcements.peek_cached(now=1000) == []
    assert announcements.peek_cached(now=1001) == []


def test_peek_cached_with_no_file_is_empty():
    assert announcements.peek_cached() == []


def test_peek_cached_survives_a_corrupt_cache():
    announcements._cache_file().parent.mkdir(parents=True, exist_ok=True)
    announcements._cache_file().write_text("{not json", encoding="utf-8")
    assert announcements.peek_cached() == []


# --- TTL ---

def test_ttl_is_one_hour():
    assert announcements._TTL_SECONDS == 60 * 60


def test_should_check_without_cache():
    assert announcements._should_check() is True


def test_fresh_cache_suppresses_check():
    announcements._write_cache([Announcement(id=1, body="b")])
    assert announcements._should_check() is False


def test_stale_cache_triggers_check():
    announcements._write_cache([Announcement(id=1, body="b")])
    cached = announcements._read_cache()
    cached["fetched_at"] = time.time() - announcements._TTL_SECONDS - 1
    announcements._cache_file().write_text(json.dumps(cached), encoding="utf-8")
    assert announcements._should_check() is True


def test_upgrade_forces_recheck():
    """Version-targeted announcements may start/stop applying after an upgrade."""
    announcements._write_cache([Announcement(id=1, body="b")])
    cached = announcements._read_cache()
    cached["app_version"] = "0.0.1"
    announcements._cache_file().write_text(json.dumps(cached), encoding="utf-8")
    assert announcements._should_check() is True


# --- fetching ---

def test_fetch_sends_identity_to_the_right_endpoint(monkeypatch):
    endpoint = _endpoint(
        _cfg(provision_url="https://worker.example.com/"), monkeypatch=monkeypatch)
    seen = _stub_post(monkeypatch, json_body=_payload(_item()))

    items = announcements._fetch(endpoint)

    assert seen["url"] == "https://worker.example.com/announcements"
    assert seen["json"] == {
        "shared_secret": "s3cret",
        "username": "abc123def456",
    }
    assert seen["headers"]["User-Agent"] == announcements.user_agent()
    assert [i.id for i in items] == [1]


@pytest.mark.parametrize("sys_platform,expected", [
    ("darwin", "macos"),
    ("win32", "windows"),
    ("linux", "linux"),
    ("linux2", "linux"),
    ("freebsd", ""),
])
def test_user_agent_includes_version_and_platform(monkeypatch, sys_platform, expected):
    from kiro_gateway_tray import __version__
    monkeypatch.setattr(announcements.sys, "platform", sys_platform)
    ua = announcements.user_agent()
    assert ua.startswith(f"KiroGatewayTray/{__version__}")
    if expected:
        assert ua.endswith(f"({expected})")
    else:
        assert "(" not in ua


def test_fetch_returns_empty_list_when_everything_is_taken_down(monkeypatch):
    """Empty is a real answer and must be distinguishable from a failure."""
    endpoint = _endpoint(monkeypatch=monkeypatch)
    _stub_post(monkeypatch, json_body=_payload())
    assert announcements._fetch(endpoint) == []


@pytest.mark.parametrize("status", [400, 401, 404, 500, 503])
def test_fetch_returns_none_on_error_status(monkeypatch, status):
    endpoint = _endpoint(monkeypatch=monkeypatch)
    _stub_post(monkeypatch, status_code=status, json_body={"error": "nope"})
    assert announcements._fetch(endpoint) is None


def test_fetch_returns_none_on_network_error(monkeypatch):
    endpoint = _endpoint(monkeypatch=monkeypatch)
    _stub_post(monkeypatch, raises=httpx.ConnectTimeout("timeout"))
    assert announcements._fetch(endpoint) is None


def test_fetch_returns_none_on_malformed_json(monkeypatch):
    endpoint = _endpoint(monkeypatch=monkeypatch)

    def _post(url, json=None, headers=None, timeout=None, proxy=None):
        return httpx.Response(200, content=b"<html>nope", request=httpx.Request("POST", url))

    monkeypatch.setattr(announcements.httpx, "post", _post)
    assert announcements._fetch(endpoint) is None


# --- readiness ---

def test_endpoint_is_unresolved_before_setup_completes(monkeypatch):
    monkeypatch.setattr(
        "kiro_gateway_tray.provision._get_username", lambda cfg: "abc123def456")
    assert announcements._resolve_endpoint(_cfg(provision_url="")) is None
    assert announcements._resolve_endpoint(_cfg(shared_secret="")) is None


def test_endpoint_is_unresolved_when_username_is_unavailable(monkeypatch):
    def _raise(cfg):
        raise RuntimeError("no kiro token")
    monkeypatch.setattr("kiro_gateway_tray.provision._get_username", _raise)
    assert announcements._resolve_endpoint(_cfg()) is None


def test_endpoint_url_joins_cleanly_regardless_of_trailing_slash(monkeypatch):
    for base in ("https://w.example.com", "https://w.example.com/"):
        endpoint = _endpoint(_cfg(provision_url=base), monkeypatch=monkeypatch)
        assert endpoint.url == "https://w.example.com/announcements"


# --- check() orchestration ---

def test_check_uses_cache_when_fresh():
    """Network fixture would fail the test if a request were attempted."""
    announcements._write_cache([Announcement(id=1, body="缓存的")])
    assert [i.id for i in announcements.check()] == [1]


def test_check_force_refetches_even_when_fresh(monkeypatch):
    announcements._write_cache([Announcement(id=1, body="旧的")])
    _stub_provisioned(monkeypatch)
    _stub_post(monkeypatch, json_body=_payload(_item(id=2, body="新的")))

    assert [i.id for i in announcements.check(force=True)] == [2]


def test_check_replaces_cache_with_empty_result(monkeypatch):
    """Taking every announcement down must actually clear the bar."""
    announcements._write_cache([Announcement(id=1, body="旧的")])
    _stub_provisioned(monkeypatch)
    _stub_post(monkeypatch, json_body=_payload())

    assert announcements.check(force=True) == []
    assert announcements.peek_cached() == []


def test_check_skips_the_network_on_an_unprovisioned_install(monkeypatch):
    """Fresh install, setup not finished yet: no request, no crash, no rows.

    The autouse network fixture fails the test if a request is attempted.
    """
    monkeypatch.setattr(
        announcements.appconfig, "load", lambda **kw: _cfg(provision_url=""))
    assert announcements.check(force=True) == []


def test_check_still_shows_cached_items_before_setup_completes(monkeypatch):
    """An upgrade wipes neither config nor cache, but a transient read of an
    empty config must not blank the bar."""
    announcements._write_cache([Announcement(id=1, body="旧的")])
    monkeypatch.setattr(
        announcements.appconfig, "load", lambda **kw: _cfg(provision_url=""))
    assert [i.id for i in announcements.check(force=True)] == [1]


def test_check_keeps_previous_items_when_the_fetch_fails(monkeypatch):
    announcements._write_cache([Announcement(id=1, body="旧的")])
    _stub_provisioned(monkeypatch)
    monkeypatch.setattr(announcements, "_fetch", lambda endpoint: None)

    assert [i.id for i in announcements.check(force=True)] == [1]


def test_failed_fetch_still_bumps_the_ttl(monkeypatch):
    """A Worker outage must not turn every menu open into a request."""
    announcements._write_cache([Announcement(id=1, body="旧的")])
    cached = announcements._read_cache()
    cached["fetched_at"] = time.time() - announcements._TTL_SECONDS - 1
    announcements._cache_file().write_text(json.dumps(cached), encoding="utf-8")
    _stub_provisioned(monkeypatch)
    monkeypatch.setattr(announcements, "_fetch", lambda endpoint: None)

    before = time.time()
    announcements.check()

    assert announcements._read_cache()["fetched_at"] >= before
    assert announcements._should_check() is False


def test_incomplete_setup_does_not_bump_the_ttl(monkeypatch):
    """First run: the activation code lands a moment after the tray starts.

    Stamping the TTL here would leave a brand-new user without announcements for
    a full hour, so an unresolved endpoint must leave the cache untouched.
    """
    monkeypatch.setattr(
        announcements.appconfig, "load", lambda **kw: _cfg(shared_secret=""))

    assert announcements.check() == []
    assert announcements._read_cache() is None
    assert announcements._should_check() is True

    # Setup finishes; the very next check goes straight to the network.
    _stub_provisioned(monkeypatch)
    _stub_post(monkeypatch, json_body=_payload(_item(id=7, body="现在可见")))
    assert [i.id for i in announcements.check()] == [7]


def test_check_never_raises(monkeypatch):
    def _explode(*a, **k):
        raise RuntimeError("boom")
    monkeypatch.setattr(announcements, "_fetch", _explode)
    assert announcements.check(force=True) == []


# --- platform slug ---

@pytest.mark.parametrize("sys_platform,expected", [
    ("darwin", "macos"),
    ("win32", "windows"),
    ("linux", "linux"),
    ("linux2", "linux"),
    ("freebsd13", ""),
])
def test_platform_slug(monkeypatch, sys_platform, expected):
    monkeypatch.setattr(announcements.sys, "platform", sys_platform)
    assert announcements.platform_slug() == expected
