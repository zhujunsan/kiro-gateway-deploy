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


def test_ensure_dns_ok_true(monkeypatch):
    import kiro_gateway_tray.appconfig as appconfig

    captured = {}

    def fake_post(url, **kwargs):
        captured["url"] = url
        captured["json"] = kwargs.get("json")
        return httpx.Response(
            200,
            json={"ok": True, "hostname": "kg-alice.example.com", "repaired": True},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(provision.httpx, "post", fake_post)
    cfg = appconfig.AppCfg()
    cfg.cloudflare.provision_url = "https://w.example.com"
    cfg.cloudflare.hostname = "kg-alice.example.com"
    monkeypatch.setattr(provision, "_get_username", lambda _c: "other-slug")
    assert provision.ensure_dns(cfg, "secret") is True
    assert captured["url"].endswith("/ensure-dns")
    assert captured["json"]["username"] == "alice"


def test_ensure_dns_tunnel_missing_is_false(monkeypatch):
    import kiro_gateway_tray.appconfig as appconfig

    def fake_post(url, **kwargs):
        return httpx.Response(
            404,
            json={"error": "tunnel not found"},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(provision.httpx, "post", fake_post)
    cfg = appconfig.AppCfg()
    cfg.cloudflare.provision_url = "https://w.example.com"
    monkeypatch.setattr(provision, "_get_username", lambda _c: "alice")
    assert provision.ensure_dns(cfg, "secret") is False


def test_ensure_dns_old_worker_is_none(monkeypatch):
    import kiro_gateway_tray.appconfig as appconfig

    def fake_post(url, **kwargs):
        return httpx.Response(
            404,
            text="not found",
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(provision.httpx, "post", fake_post)
    cfg = appconfig.AppCfg()
    cfg.cloudflare.provision_url = "https://w.example.com"
    monkeypatch.setattr(provision, "_get_username", lambda _c: "alice")
    assert provision.ensure_dns(cfg, "secret") is None


def test_ensure_dns_network_error_is_none(monkeypatch):
    import kiro_gateway_tray.appconfig as appconfig

    def fake_post(url, **kwargs):
        raise httpx.ConnectError("nope")

    monkeypatch.setattr(provision.httpx, "post", fake_post)
    cfg = appconfig.AppCfg()
    cfg.cloudflare.provision_url = "https://w.example.com"
    monkeypatch.setattr(provision, "_get_username", lambda _c: "alice")
    assert provision.ensure_dns(cfg, "secret") is None