# app/tests/test_cloudflared.py
from pathlib import Path
from kiro_gateway_tray import cloudflared, appconfig


def test_binary_name_per_platform():
    import sys
    name = cloudflared._binary_name()
    if sys.platform.startswith("win"):
        assert name == "cloudflared.exe"
    else:
        assert name == "cloudflared"


def test_binary_path_missing_raises(monkeypatch):
    monkeypatch.setattr(cloudflared, "_candidate_dirs", lambda: [Path("/no/such")])
    try:
        cloudflared.binary_path()
        assert False, "expected RuntimeError"
    except RuntimeError as e:
        assert "cloudflared" in str(e).lower()


def test_connection_detection_constants():
    # Guards the fragile stdout-string contract; if cloudflared rewords these,
    # this is the single place to update (see CloudflaredProcess docstring).
    assert cloudflared.CloudflaredProcess._LOG_CONNECTED == "Registered tunnel connection"
    assert cloudflared.CloudflaredProcess._LOG_DISCONNECTED == "Unregistered tunnel connection"


def test_provision_username_from_client_id_hash(monkeypatch):
    from kiro_gateway_tray import provision
    cfg = appconfig.AppCfg()
    monkeypatch.setattr(
        provision, "_read_client_id_hash", lambda _cfg: "ABCDEF0123456789abcdef"
    )
    # first 12 hex chars, lowercased
    assert provision._get_username(cfg) == "abcdef012345"


def test_provision_username_missing_hash_raises(monkeypatch):
    from kiro_gateway_tray import provision
    cfg = appconfig.AppCfg()
    monkeypatch.setattr(provision, "_read_client_id_hash", lambda _cfg: None)
    try:
        provision._get_username(cfg)
        assert False, "expected RuntimeError"
    except RuntimeError as e:
        assert "clientIdHash" in str(e)


def test_provision_read_profile_arn_and_region(monkeypatch):
    from kiro_gateway_tray import provision
    cfg = appconfig.AppCfg()
    arn = "arn:aws:codewhisperer:us-east-1:123456789012:profile/ABC"
    monkeypatch.setattr(provision, "_read_kiro_token", lambda _cfg: {"profileArn": arn})
    assert provision.read_profile_arn(cfg) == arn
    assert provision.read_api_region(cfg) == "us-east-1"


def test_post_with_retry_retries_on_5xx(monkeypatch):
    from kiro_gateway_tray import provision

    class _Resp:
        def __init__(self, code):
            self.status_code = code
            self.text = "x"

    calls = {"n": 0}

    def fake_post(url, json, timeout):
        calls["n"] += 1
        return _Resp(500 if calls["n"] < 3 else 200)

    monkeypatch.setattr(provision.httpx, "post", fake_post)
    monkeypatch.setattr(provision.time, "sleep", lambda _s: None)
    resp = provision._post_with_retry("http://x/provision", {})
    assert resp.status_code == 200
    assert calls["n"] == 3


def test_post_with_retry_no_retry_on_401(monkeypatch):
    from kiro_gateway_tray import provision

    class _Resp:
        status_code = 401
        text = "nope"

    calls = {"n": 0}

    def fake_post(url, json, timeout):
        calls["n"] += 1
        return _Resp()

    monkeypatch.setattr(provision.httpx, "post", fake_post)
    monkeypatch.setattr(provision.time, "sleep", lambda _s: None)
    resp = provision._post_with_retry("http://x/provision", {})
    assert resp.status_code == 401
    assert calls["n"] == 1  # client error returned immediately, not retried
