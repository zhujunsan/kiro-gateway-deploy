"""Proxy normalization for tray outbound calls.

Guards the socks:// -> socks5h:// rewrite that keeps a user proxy from crashing
httpx at client construction, and that local_client stays unproxied.
"""
import httpx
import pytest

from kiro_gateway_tray import httpclient

_PROXY_VARS = ["HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy",
               "HTTP_PROXY", "http_proxy"]


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("socks://127.0.0.1:7891", "socks5h://127.0.0.1:7891"),
        ("socks4://host:1080", "socks5h://host:1080"),
        ("socks5://127.0.0.1:7891", "socks5://127.0.0.1:7891"),
        ("socks5h://127.0.0.1:7891", "socks5h://127.0.0.1:7891"),
        ("http://127.0.0.1:7890", "http://127.0.0.1:7890"),
        ("127.0.0.1:7890", "http://127.0.0.1:7890"),
        ("", None),
        (None, None),
    ],
)
def test_normalize_proxy_url(raw, expected):
    assert httpclient.normalize_proxy_url(raw) == expected


def test_resolve_proxy_none_when_unset(monkeypatch):
    for var in _PROXY_VARS:
        monkeypatch.delenv(var, raising=False)
    assert httpclient.resolve_proxy() is None


def test_resolve_proxy_normalizes_socks(monkeypatch):
    for var in _PROXY_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("ALL_PROXY", "socks://127.0.0.1:7891")
    assert httpclient.resolve_proxy() == "socks5h://127.0.0.1:7891"


def test_resolved_proxy_accepted_by_httpx(monkeypatch):
    for var in _PROXY_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("ALL_PROXY", "socks://127.0.0.1:7891")
    # The whole point: constructing a client with the resolved proxy must not raise.
    client = httpx.Client(proxy=httpclient.resolve_proxy())
    client.close()


def test_local_client_ignores_proxy_env(monkeypatch):
    # Local probes must never be routed through a user proxy.
    monkeypatch.setenv("ALL_PROXY", "socks://127.0.0.1:7891")
    client = httpclient.local_client(timeout=3)
    try:
        assert client.trust_env is False
    finally:
        client.close()
