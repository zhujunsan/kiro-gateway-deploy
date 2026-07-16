import os
import threading
import tomllib

from kiro_gateway_tray import appconfig


def test_defaults_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("KIRO_GATEWAY_TRAY_HOME", str(tmp_path))
    cfg = appconfig.load()
    assert cfg.gateway.port == 64005
    assert cfg.cloudflare.hostname == ""
    assert cfg.cloudflare.run_token == ""
    assert appconfig.path().exists()


def test_edit_and_reload(tmp_path, monkeypatch):
    monkeypatch.setenv("KIRO_GATEWAY_TRAY_HOME", str(tmp_path))
    cfg = appconfig.load()
    cfg.gateway.proxy_api_key = "secret123"
    cfg.cloudflare.hostname = "kg-alice.example.com"
    cfg.cloudflare.run_token = "eyJ_test"
    appconfig.save(cfg)
    again = appconfig.load()
    assert again.gateway.proxy_api_key == "secret123"
    assert again.cloudflare.hostname == "kg-alice.example.com"
    assert again.cloudflare.run_token == "eyJ_test"


def test_to_env_maps_known_keys(tmp_path, monkeypatch):
    monkeypatch.setenv("KIRO_GATEWAY_TRAY_HOME", str(tmp_path))
    cfg = appconfig.load()
    cfg.gateway.profile_arn = "arn:x"
    cfg.gateway.proxy_api_key = "k"
    env = appconfig.to_gateway_env(cfg)
    assert env["PROFILE_ARN"] == "arn:x"
    assert env["PROXY_API_KEY"] == "k"
    assert env["SERVER_HOST"] == "127.0.0.1"
    assert env["SERVER_PORT"] == "64005"
    assert env["FAKE_REASONING"] == "false"
    # Debug capture defaults: verbose logs + on-error payload dump.
    assert env["LOG_LEVEL"] == "DEBUG"
    assert env["DEBUG_MODE"] == "errors"


def test_legacy_fake_reasoning_migrates_to_extra(tmp_path, monkeypatch):
    monkeypatch.setenv("KIRO_GATEWAY_TRAY_HOME", str(tmp_path))
    cfg = appconfig.load()
    p = appconfig.path()
    p.write_text(
        '[gateway]\nfake_reasoning = true\n\n[gateway_extra]\nAUTO_TRIM_PAYLOAD = "true"\n',
        encoding="utf-8",
    )
    again = appconfig.load()
    assert not hasattr(again.gateway, "fake_reasoning")
    assert again.gateway_extra["FAKE_REASONING"] == "true"
    env = appconfig.to_gateway_env(again)
    assert env["FAKE_REASONING"] == "true"


def test_debug_defaults_backfilled_for_old_configs(tmp_path, monkeypatch):
    monkeypatch.setenv("KIRO_GATEWAY_TRAY_HOME", str(tmp_path))
    cfg = appconfig.load()
    p = appconfig.path()
    # Old config written before LOG_LEVEL/DEBUG_MODE existed.
    p.write_text(
        '[gateway_extra]\nAUTO_TRIM_PAYLOAD = "false"\n',
        encoding="utf-8",
    )
    again = appconfig.load()
    assert again.gateway_extra["LOG_LEVEL"] == "DEBUG"
    assert again.gateway_extra["DEBUG_MODE"] == "errors"


def test_user_debug_values_are_not_overridden(tmp_path, monkeypatch):
    monkeypatch.setenv("KIRO_GATEWAY_TRAY_HOME", str(tmp_path))
    cfg = appconfig.load()
    p = appconfig.path()
    p.write_text(
        '[gateway_extra]\nLOG_LEVEL = "INFO"\nDEBUG_MODE = "off"\n',
        encoding="utf-8",
    )
    again = appconfig.load()
    # setdefault must not clobber explicit user choices.
    assert again.gateway_extra["LOG_LEVEL"] == "INFO"
    assert again.gateway_extra["DEBUG_MODE"] == "off"


def test_is_provisioned(tmp_path, monkeypatch):
    monkeypatch.setenv("KIRO_GATEWAY_TRAY_HOME", str(tmp_path))
    cfg = appconfig.load()
    assert appconfig.is_provisioned(cfg) is False
    cfg.cloudflare.hostname = "kg-alice.example.com"
    cfg.cloudflare.run_token = "eyJ_test"
    assert appconfig.is_provisioned(cfg) is True


def test_cache_returns_same_instance_until_save(tmp_path, monkeypatch):
    monkeypatch.setenv("KIRO_GATEWAY_TRAY_HOME", str(tmp_path))
    appconfig.invalidate_cache()
    c1 = appconfig.load(use_cache=True)
    c2 = appconfig.load(use_cache=True)
    assert c1 is c2  # cache hit returns the same object


def test_save_refreshes_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("KIRO_GATEWAY_TRAY_HOME", str(tmp_path))
    appconfig.invalidate_cache()
    c1 = appconfig.load(use_cache=True)
    c1.gateway.port = 55555
    appconfig.save(c1)
    c2 = appconfig.load(use_cache=True)
    assert c2.gateway.port == 55555


def test_url_helpers(tmp_path, monkeypatch):
    monkeypatch.setenv("KIRO_GATEWAY_TRAY_HOME", str(tmp_path))
    cfg = appconfig.load()
    cfg.gateway.port = 64005
    assert appconfig.gateway_origin(cfg) == "http://127.0.0.1:64005"
    assert appconfig.local_url(cfg) == "http://127.0.0.1:64005/v1"
    assert appconfig.tunnel_url(cfg) == ""
    # base_url falls back to local when no tunnel hostname
    assert appconfig.base_url(cfg) == "http://127.0.0.1:64005/v1"
    cfg.cloudflare.hostname = "kg-alice.example.com"
    assert appconfig.tunnel_url(cfg) == "https://kg-alice.example.com/v1"
    assert appconfig.base_url(cfg) == "https://kg-alice.example.com/v1"


def test_shared_secret_persists(tmp_path, monkeypatch):
    monkeypatch.setenv("KIRO_GATEWAY_TRAY_HOME", str(tmp_path))
    cfg = appconfig.load()
    cfg.cloudflare.shared_secret = "act-code-123"
    appconfig.save(cfg)
    again = appconfig.load()
    assert again.cloudflare.shared_secret == "act-code-123"


def test_telemetry_url_derived_from_provision_url(tmp_path, monkeypatch):
    # Scheme A: telemetry shares the provision Worker/domain. With no explicit
    # endpoint_url but a provisioned URL, the report URL is derived as
    # provision_url + /telemetry, so telemetry auto-enables after activation.
    monkeypatch.setenv("KIRO_GATEWAY_TRAY_HOME", str(tmp_path))
    cfg = appconfig.load()
    cfg.cloudflare.provision_url = "https://kiro-gateway-provision.botsonny.top"
    cfg.cloudflare.shared_secret = "act-code"
    env = appconfig.to_gateway_env(cfg)
    assert env["TELEMETRY_URL"] == "https://kiro-gateway-provision.botsonny.top/telemetry"
    # Refresh chain still wired from the same provision_url.
    assert env["TELEMETRY_PROVISION_URL"] == "https://kiro-gateway-provision.botsonny.top"
    assert env["TELEMETRY_SHARED_SECRET"] == "act-code"


def test_telemetry_url_strips_trailing_slash(tmp_path, monkeypatch):
    monkeypatch.setenv("KIRO_GATEWAY_TRAY_HOME", str(tmp_path))
    cfg = appconfig.load()
    cfg.cloudflare.provision_url = "https://w.example.com/"
    env = appconfig.to_gateway_env(cfg)
    assert env["TELEMETRY_URL"] == "https://w.example.com/telemetry"


def test_telemetry_explicit_endpoint_wins_over_derivation(tmp_path, monkeypatch):
    # An explicit endpoint_url is the override escape hatch and must not be
    # replaced by the provision_url derivation.
    monkeypatch.setenv("KIRO_GATEWAY_TRAY_HOME", str(tmp_path))
    cfg = appconfig.load()
    cfg.telemetry.endpoint_url = "https://custom.example.com/telemetry"
    cfg.cloudflare.provision_url = "https://kiro-gateway-provision.botsonny.top"
    env = appconfig.to_gateway_env(cfg)
    assert env["TELEMETRY_URL"] == "https://custom.example.com/telemetry"


def test_telemetry_flush_interval_injected_end_to_end(tmp_path, monkeypatch):
    # flush_interval must actually reach the child: it is injected as its own
    # env var (not aliased to TELEMETRY_BUCKET_SECONDS) and round-trips through
    # telemetry.from_env back onto the config field.
    monkeypatch.setenv("KIRO_GATEWAY_TRAY_HOME", str(tmp_path))
    cfg = appconfig.load()
    cfg.cloudflare.provision_url = "https://w.example.com"
    cfg.telemetry.bucket_seconds = 300
    cfg.telemetry.flush_interval = 120
    env = appconfig.to_gateway_env(cfg)
    assert env["TELEMETRY_BUCKET_SECONDS"] == "300"
    assert env["TELEMETRY_FLUSH_INTERVAL"] == "120"

    from kiro_gateway_tray import telemetry
    resolved = telemetry.from_env(env)
    assert resolved.bucket_seconds == 300
    assert resolved.flush_interval == 120


def test_telemetry_not_injected_when_both_empty(tmp_path, monkeypatch):
    # No endpoint_url and no provision_url ⇒ telemetry stays dormant.
    monkeypatch.setenv("KIRO_GATEWAY_TRAY_HOME", str(tmp_path))
    cfg = appconfig.load()
    assert cfg.telemetry.endpoint_url == ""
    assert cfg.cloudflare.provision_url == ""
    env = appconfig.to_gateway_env(cfg)
    assert "TELEMETRY_URL" not in env


def test_save_failure_preserves_previous_toml_and_cleans_temp(tmp_path, monkeypatch):
    monkeypatch.setenv("KIRO_GATEWAY_TRAY_HOME", str(tmp_path))
    cfg = appconfig.load()
    cfg.gateway.port = 64006
    appconfig.save(cfg)
    original = appconfig.path().read_bytes()
    cfg.gateway.port = 64007

    monkeypatch.setattr(
        appconfig.os, "replace",
        lambda *_args: (_ for _ in ()).throw(OSError("replace failed")),
    )
    try:
        appconfig.save(cfg)
        assert False, "expected atomic replacement failure"
    except OSError:
        pass

    assert appconfig.path().read_bytes() == original
    assert tomllib.loads(original.decode("utf-8"))["gateway"]["port"] == 64006
    assert appconfig.load(use_cache=True).gateway.port == 64006
    assert list(appconfig.path().parent.glob(f".{appconfig.path().name}.*.tmp")) == []


def test_update_serializes_latest_config_and_keeps_valid_toml(tmp_path, monkeypatch):
    monkeypatch.setenv("KIRO_GATEWAY_TRAY_HOME", str(tmp_path))
    appconfig.invalidate_cache()
    appconfig.load()

    def increment_port() -> None:
        appconfig.update(lambda cfg: setattr(cfg.gateway, "port", cfg.gateway.port + 1))

    workers = [threading.Thread(target=increment_port) for _ in range(16)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()

    expected = 64005 + len(workers)
    assert appconfig.load().gateway.port == expected
    assert appconfig.load(use_cache=True).gateway.port == expected
    assert tomllib.loads(appconfig.path().read_text(encoding="utf-8"))["gateway"]["port"] == expected
    if os.name == "posix":
        assert appconfig.path().stat().st_mode & 0o777 == 0o600
