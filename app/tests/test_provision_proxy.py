"""Regression: first-run registration must survive a socks:// proxy env.

httpx only accepts http/https/socks5/socks5h proxy schemes and raises
``ValueError: Unknown scheme for proxy URL`` at client construction for the
generic ``socks://`` form that many proxy clients export. That crashed the tray
on Linux (v0.3.6). We now normalize socks:// -> socks5h:// and pass the proxy
explicitly, and httpx[socks] provides the SOCKS backend.
"""
import httpx
import pytest

from kiro_gateway_tray import provision


def test_post_with_retry_normalizes_socks_proxy_env(monkeypatch):
    # Simulate a box with a socks proxy exported globally.
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
    # socks:// is normalized so httpx accepts it, and the call goes through it.
    assert captured.get("proxy") == "socks5h://127.0.0.1:7891"


def test_post_with_retry_real_client_survives_socks_env(monkeypatch):
    """End-to-end guard: build a real httpx client under socks env.

    We can't hit the network in tests, but constructing the client is exactly
    the step that used to raise. A connect/timeout error (RuntimeError from the
    retry wrapper) is fine; a ValueError is not.
    """
    monkeypatch.setenv("ALL_PROXY", "socks://127.0.0.1:7891")
    monkeypatch.setattr(provision.time, "sleep", lambda *_: None)
    with pytest.raises(RuntimeError):
        provision._post_with_retry("http://127.0.0.1:1/provision", {"x": 1})
