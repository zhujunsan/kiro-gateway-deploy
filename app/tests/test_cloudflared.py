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


def test_start_pins_metrics_port(monkeypatch, tmp_path):
    # cloudflared must be launched with a fixed --metrics address so the
    # /ready probe has a stable target; the configured port is used when free.
    import kiro_gateway_tray.cloudflared as cf

    monkeypatch.setattr(cf, "binary_path", lambda: Path("/fake/cloudflared"))
    monkeypatch.setattr(cf, "_port_is_free", lambda _p: True)
    monkeypatch.setattr(cf.proc_guard, "kill_orphan", lambda: False)
    monkeypatch.setattr(cf.proc_guard, "after_spawn", lambda _p: None)
    monkeypatch.setattr(cf.proc_guard, "record_pid", lambda _pid: None)

    captured = {}

    class _FakeProc:
        pid = 4242
        stdout = None
        def poll(self): return None

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        return _FakeProc()

    monkeypatch.setattr(cf.subprocess, "Popen", fake_popen)

    cfg = appconfig.AppCfg()
    cfg.cloudflare.run_token = "eyJ_test"
    cfg.cloudflare.metrics_port = 20299
    cfg.cloudflare.protocol = "http2"

    proc = cf.CloudflaredProcess()
    proc.start(cfg)

    cmd = captured["cmd"]
    assert "--metrics" in cmd
    assert cmd[cmd.index("--metrics") + 1] == "127.0.0.1:20299"
    assert proc.metrics_port == 20299


def test_start_falls_back_when_metrics_port_busy(monkeypatch):
    # A busy metrics port must NOT be fatal: cloudflared treats a failed metrics
    # bind as fatal and exits, so we fall back to a free port and the probe
    # follows the port we actually bound.
    import kiro_gateway_tray.cloudflared as cf

    monkeypatch.setattr(cf, "binary_path", lambda: Path("/fake/cloudflared"))
    # Configured port is busy; the OS-assigned fallback is free.
    monkeypatch.setattr(cf, "_port_is_free", lambda p: p != 20299)
    monkeypatch.setattr(cf.proc_guard, "kill_orphan", lambda: False)
    monkeypatch.setattr(cf.proc_guard, "after_spawn", lambda _p: None)
    monkeypatch.setattr(cf.proc_guard, "record_pid", lambda _pid: None)

    captured = {}

    class _FakeProc:
        pid = 4242
        stdout = None
        def poll(self): return None

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        return _FakeProc()

    monkeypatch.setattr(cf.subprocess, "Popen", fake_popen)

    cfg = appconfig.AppCfg()
    cfg.cloudflare.run_token = "eyJ_test"
    cfg.cloudflare.metrics_port = 20299

    proc = cf.CloudflaredProcess()
    proc.start(cfg)

    cmd = captured["cmd"]
    assert "--metrics" in cmd
    bound = cmd[cmd.index("--metrics") + 1]
    assert bound != "127.0.0.1:20299"
    assert bound.startswith("127.0.0.1:")
    # The probe target must track the actually-bound port, not the config value.
    assert proc.metrics_port != 20299
    assert proc.metrics_port == int(bound.rsplit(":", 1)[1])


def test_start_requires_run_token():
    import kiro_gateway_tray.cloudflared as cf
    cfg = appconfig.AppCfg()
    cfg.cloudflare.run_token = ""
    try:
        cf.CloudflaredProcess().start(cfg)
        assert False, "expected RuntimeError"
    except RuntimeError as e:
        assert "run_token" in str(e)


def test_provision_username_from_client_id_hash(monkeypatch):
    from kiro_gateway_tray import provision
    cfg = appconfig.AppCfg()
    # No profileArn anywhere -> fall back to clientIdHash.
    monkeypatch.setattr(provision, "_read_kiro_token", lambda _cfg: None)
    monkeypatch.setattr(
        provision, "_read_client_id_hash", lambda _data: "ABCDEF0123456789abcdef"
    )
    # first 12 hex chars, lowercased
    assert provision._get_username(cfg) == "abcdef012345"


def test_provision_username_missing_hash_raises(monkeypatch):
    from kiro_gateway_tray import provision
    cfg = appconfig.AppCfg()
    monkeypatch.setattr(provision, "_read_kiro_token", lambda _cfg: None)
    monkeypatch.setattr(provision, "_read_client_id_hash", lambda _data: None)
    try:
        provision._get_username(cfg)
        assert False, "expected RuntimeError"
    except RuntimeError as e:
        assert "clientIdHash" in str(e)


def test_provision_username_prefers_per_user_client_id(monkeypatch):
    """Per-user clientId (unique per user) takes precedence over org-shared clientIdHash."""
    import hashlib
    from kiro_gateway_tray import provision
    cfg = appconfig.AppCfg()
    monkeypatch.setattr(provision, "_read_per_user_client_id", lambda _cfg, _data: "my-unique-client-id")
    monkeypatch.setattr(provision, "_read_client_id_hash", lambda _data: "ABCDEF0123456789abcdef")
    expected = hashlib.sha1("my-unique-client-id".encode()).hexdigest()[:12]
    assert provision._get_username(cfg) == expected


def test_provision_config_profile_arn_overrides_token(monkeypatch):
    from kiro_gateway_tray import provision
    cfg = appconfig.AppCfg()
    cfg.gateway.profile_arn = "arn:aws:codewhisperer:eu-west-1:999:profile/CFG"
    monkeypatch.setattr(
        provision, "_read_kiro_token",
        lambda _cfg: {"profileArn": "arn:aws:codewhisperer:us-east-1:111:profile/TOK"},
    )
    assert provision.read_profile_arn(cfg) == cfg.gateway.profile_arn
    assert provision.read_api_region(cfg) == "eu-west-1"


def test_region_from_arn():
    from kiro_gateway_tray import provision
    arn = "arn:aws:codewhisperer:ap-northeast-1:123456789012:profile/ABC"
    assert provision.region_from_arn(arn) == "ap-northeast-1"
    assert provision.region_from_arn("") == ""
    assert provision.region_from_arn("not-an-arn") == ""


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

    def fake_post(url, json, timeout, **kwargs):
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

    def fake_post(url, json, timeout, **kwargs):
        calls["n"] += 1
        return _Resp()

    monkeypatch.setattr(provision.httpx, "post", fake_post)
    monkeypatch.setattr(provision.time, "sleep", lambda _s: None)
    resp = provision._post_with_retry("http://x/provision", {})
    assert resp.status_code == 401
    assert calls["n"] == 1  # client error returned immediately, not retried
