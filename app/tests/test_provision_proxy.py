"""Regression: first-run registration must ignore system proxy env vars.

httpx raises ``ValueError: Unknown scheme for proxy URL`` at client construction
when ALL_PROXY / HTTP_PROXY points at a socks:// proxy and httpx[socks] isn't
installed. That crashed the whole tray on Linux (v0.3.6) because
provision._post_with_retry built its httpx.post with the default trust_env=True.
"""
import httpx
import pytest

from kiro_gateway_tray import provision


def test_post_with_retry_ignores_socks_proxy_env(monkeypatch):
    # Simulate a Linux box with a socks proxy exported globally.
    for var in ("ALL_PROXY", "all_proxy", "HTTP_PROXY", "http_proxy",
                "HTTPS_PROXY", "https_proxy"):
        monkeypatch.setenv(var, "socks://127.0.0.1:7891")

    captured = {}

    def fake_post(url, **kwargs):
        captured.update(kwargs)
        return httpx.Response(200, request=httpx.Request("POST", url))

    monkeypatch.setattr(provision.httpx, "post", fake_post)

    # Must not raise ValueError("Unknown scheme for proxy URL ...").
    resp = provision._post_with_retry("https://w.example.com/provision", {"x": 1})

    assert resp.status_code == 200
    assert captured.get("trust_env") is False


def test_post_with_retry_real_client_survives_socks_env(monkeypatch):
    """End-to-end guard: build a real httpx client under socks env.

    We can't hit the network in tests, but constructing the client is exactly
    the step that used to raise. A connect error is fine; a ValueError is not.
    """
    monkeypatch.setenv("ALL_PROXY", "socks://127.0.0.1:7891")
    monkeypatch.setattr(provision.time, "sleep", lambda *_: None)
    with pytest.raises(RuntimeError):
        provision._post_with_retry("http://127.0.0.1:1/provision", {"x": 1})
