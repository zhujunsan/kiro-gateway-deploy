# app/tests/test_sentry_setup.py
"""Tests for Sentry init helpers: DSN resolution, scrubbing, verify middleware."""
from __future__ import annotations

import pytest

from kiro_gateway_tray import sentry_setup as ss


@pytest.fixture(autouse=True)
def _reset_sentry_ready(monkeypatch):
    monkeypatch.setattr(ss, "_READY", False)
    monkeypatch.setattr(ss, "_SNAPSHOT_BRIDGE_INSTALLED", False)
    monkeypatch.setattr(ss, "DEFAULT_DSN", "")
    # Never let a leaked DSN initialize the real transport during unit tests.
    monkeypatch.setenv("SENTRY_DSN", "")
    yield
    monkeypatch.setattr(ss, "_READY", False)
    monkeypatch.setattr(ss, "_SNAPSHOT_BRIDGE_INSTALLED", False)


def test_resolve_dsn_empty_by_default(monkeypatch):
    monkeypatch.delenv("SENTRY_DSN", raising=False)
    monkeypatch.setattr(ss, "DEFAULT_DSN", "")
    assert ss.resolve_dsn({}) == ""


def test_resolve_dsn_env_wins_over_default(monkeypatch):
    monkeypatch.setattr(ss, "DEFAULT_DSN", "https://default@o1.ingest.sentry.io/1")
    # Override the autouse empty SENTRY_DSN for this case.
    assert (
        ss.resolve_dsn({"SENTRY_DSN": "https://env@o1.ingest.sentry.io/2"})
        == "https://env@o1.ingest.sentry.io/2"
    )


def test_resolve_dsn_uses_default_when_env_absent(monkeypatch):
    monkeypatch.delenv("SENTRY_DSN", raising=False)
    monkeypatch.setattr(ss, "DEFAULT_DSN", "https://key@o1.ingest.sentry.io/1")
    assert ss.resolve_dsn({}) == "https://key@o1.ingest.sentry.io/1"


def test_resolve_dsn_env_empty_disables_default(monkeypatch):
    monkeypatch.setattr(ss, "DEFAULT_DSN", "https://key@o1.ingest.sentry.io/1")
    assert ss.resolve_dsn({"SENTRY_DSN": ""}) == ""
    assert ss.resolve_dsn({"SENTRY_DSN": "   "}) == ""


def test_release_name_uses_package_version():
    from kiro_gateway_tray import __version__
    assert ss.release_name() == f"kiro-gateway-tray@{__version__}"


def test_release_name_explicit():
    assert ss.release_name("9.9.9") == "kiro-gateway-tray@9.9.9"


def test_before_send_drops_keyboard_interrupt():
    event = {"message": "x"}
    assert ss.before_send(event, {"exc_info": (KeyboardInterrupt, KeyboardInterrupt(), None)}) is None


def test_before_send_drops_system_exit():
    event = {"message": "x"}
    assert ss.before_send(event, {"exc_info": (SystemExit, SystemExit(0), None)}) is None


def test_before_send_scrubs_auth_headers_and_token_vars():
    event = {
        "request": {
            "headers": {
                "Authorization": "Bearer secret-token",
                "X-Api-Key": "abc",
                "Content-Type": "application/json",
            }
        },
        "exception": {
            "values": [
                {
                    "stacktrace": {
                        "frames": [
                            {
                                "vars": {
                                    "refresh_token": "rt-secret",
                                    "ok_value": "keep-me",
                                    "PROXY_API_KEY": "k",
                                }
                            }
                        ]
                    }
                }
            ]
        },
    }
    out = ss.before_send(event, {})
    assert out is event
    assert out["request"]["headers"]["Authorization"] == "[Filtered]"
    assert out["request"]["headers"]["X-Api-Key"] == "[Filtered]"
    assert out["request"]["headers"]["Content-Type"] == "application/json"
    vars_ = out["exception"]["values"][0]["stacktrace"]["frames"][0]["vars"]
    assert vars_["refresh_token"] == "[Filtered]"
    assert vars_["PROXY_API_KEY"] == "[Filtered]"
    assert vars_["ok_value"] == "keep-me"


def test_init_sentry_noop_without_dsn(monkeypatch):
    monkeypatch.setattr(ss, "DEFAULT_DSN", "")
    monkeypatch.setenv("SENTRY_DSN", "")
    assert ss.init_sentry(process="tray") is False
    assert ss._READY is False


def test_init_sentry_idempotent(monkeypatch):
    calls: list[dict] = []

    class _FakeSdk:
        @staticmethod
        def init(**kwargs):
            calls.append(kwargs)

        @staticmethod
        def set_tag(key, value):
            pass

        @staticmethod
        def set_user(user):
            pass

    class _FakeLoguru:
        def __init__(self, **kwargs):
            pass

    class _FakeScrubber:
        def __init__(self, **kwargs):
            pass

    monkeypatch.setenv("SENTRY_DSN", "https://key@o1.ingest.sentry.io/99")
    monkeypatch.setitem(__import__("sys").modules, "sentry_sdk", _FakeSdk)
    monkeypatch.setitem(
        __import__("sys").modules,
        "sentry_sdk.integrations.loguru",
        type("m", (), {"LoguruIntegration": _FakeLoguru})(),
    )
    monkeypatch.setitem(
        __import__("sys").modules,
        "sentry_sdk.scrubber",
        type("m", (), {"DEFAULT_DENYLIST": [], "EventScrubber": _FakeScrubber})(),
    )

    assert ss.init_sentry(process="gateway") is True
    assert ss.init_sentry(process="gateway") is True
    assert len(calls) == 1
    assert calls[0]["dsn"] == "https://key@o1.ingest.sentry.io/99"
    assert calls[0]["send_default_pii"] is False
    assert calls[0]["max_request_body_size"] == "always"
    assert calls[0]["before_send"] is ss.before_send

def test_traces_sampler_drops_health():
    assert ss._traces_sampler({
        "transaction_context": {"op": "http.server", "name": "GET /health"},
    }) == 0.0


def test_traces_sampler_samples_api():
    rate = ss._traces_sampler({
        "transaction_context": {"op": "http.server", "name": "POST /v1/messages"},
    })
    assert rate == 0.15


def test_install_verify_route_noop_by_default():
    app = object()
    assert ss.install_gateway_verify_route(app) is app


@pytest.mark.asyncio
async def test_install_verify_route_raises_when_enabled(monkeypatch):
    monkeypatch.setenv("SENTRY_VERIFY", "1")
    monkeypatch.setenv("SENTRY_VERIFY_MARKER", "marker-abc")
    monkeypatch.setattr(ss, "_READY", True)

    inner_calls: list[str] = []

    async def inner(scope, receive, send):
        inner_calls.append(scope["path"])

    wrapped = ss.install_gateway_verify_route(inner)
    assert wrapped is not inner

    with pytest.raises(RuntimeError, match="marker-abc"):
        await wrapped(
            {"type": "http", "method": "GET", "path": "/_sentry_verify"},
            None,
            None,
        )

    await wrapped(
        {"type": "http", "method": "GET", "path": "/health"},
        None,
        None,
    )
    assert inner_calls == ["/health"]


def test_capture_exception_safe_when_disabled():
    ss.capture_exception(RuntimeError("nope"))  # must not raise


def test_flush_safe_when_disabled():
    ss.flush(timeout=0.1)  # must not raise


def test_report_incident_snapshot_noop_when_disabled():
    ss.report_incident_snapshot({
        "incident_id": "x",
        "source": "gateway",
        "code": "test",
        "artifacts": {"request_body.json": b'{"a":1}'},
    })


def test_report_incident_snapshot_attaches_artifacts(monkeypatch):
    captured: dict = {}

    class _Scope:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def set_tag(self, *args, **kwargs):
            pass

        def set_context(self, key, value):
            captured.setdefault("contexts", {})[key] = value

        def add_attachment(self, **kwargs):
            captured.setdefault("attachments", []).append(kwargs)

        @property
        def fingerprint(self):
            return captured.get("fingerprint")

        @fingerprint.setter
        def fingerprint(self, value):
            captured["fingerprint"] = value

    class _FakeSdk:
        @staticmethod
        def new_scope():
            return _Scope()

        @staticmethod
        def capture_message(message, level="info"):
            captured["message"] = message
            captured["level"] = level

    monkeypatch.setattr(ss, "_READY", True)
    monkeypatch.setitem(__import__("sys").modules, "sentry_sdk", _FakeSdk)

    ss.report_incident_snapshot({
        "incident_id": "inc-42",
        "path": "/v1/messages",
        "model": "claude-sonnet-4",
        "status_code": 502,
        "gateway_status": 502,
        "upstream_status": 500,
        "source": "kiro_upstream",
        "code": "INVALID_TOOL_USE",
        "phase": "streaming",
        "client_disconnected": False,
        "error_message": "bad tool format",
        "duration_ms": 1234,
        "artifacts": {
            "request_body.json": b'{"messages":[{"role":"user","content":"hi"}]}',
            "response_stream_raw.txt": b"chunk-1",
            "app_logs.txt": b"log line",
        },
    })

    assert "INVALID_TOOL_USE" in captured["message"]
    assert captured["level"] == "error"
    names = {a["filename"] for a in captured["attachments"]}
    assert names == {
        "request_body.json",
        "response_stream_raw.txt",
        "app_logs.txt",
    }
    assert captured["contexts"]["incident"]["incident_id"] == "inc-42"
    assert "request_body.json" in captured["contexts"]["incident_artifacts"]
    assert captured["fingerprint"][0] == "kiro-gateway-incident"
    assert "INVALID_TOOL_USE" in captured["fingerprint"]


def test_report_incident_truncates_huge_attachment(monkeypatch):
    captured: dict = {}

    class _Scope:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def set_tag(self, *a, **k):
            pass

        def set_context(self, *a, **k):
            pass

        def add_attachment(self, **kwargs):
            captured.setdefault("attachments", []).append(kwargs)

        @property
        def fingerprint(self):
            return None

        @fingerprint.setter
        def fingerprint(self, value):
            pass

    class _FakeSdk:
        @staticmethod
        def new_scope():
            return _Scope()

        @staticmethod
        def capture_message(*a, **k):
            pass

    monkeypatch.setattr(ss, "_READY", True)
    monkeypatch.setattr(ss, "_MAX_ATTACHMENT_BYTES", 16)
    monkeypatch.setitem(__import__("sys").modules, "sentry_sdk", _FakeSdk)

    ss.report_incident_snapshot({
        "incident_id": "big",
        "source": "network",
        "code": "timeout",
        "path": "/v1/chat/completions",
        "artifacts": {"response_stream_raw.txt": b"x" * 64},
    })
    att = captured["attachments"][0]
    assert att["filename"] == "response_stream_raw.txt.truncated"
    assert len(att["bytes"]) == 16


def test_gateway_upstream_sha_always_injected(tmp_path, monkeypatch):
    monkeypatch.setenv("KIRO_GATEWAY_TRAY_HOME", str(tmp_path))
    from kiro_gateway_tray import appconfig, __version__, UPSTREAM_SHA
    cfg = appconfig.load()
    env = appconfig.to_gateway_env(cfg)
    assert "INCIDENT_URL" not in env
    assert env["GATEWAY_UPSTREAM_SHA"] == UPSTREAM_SHA
    assert env["APP_VERSION"] == __version__


def test_telemetry_url_does_not_inject_incident_url(tmp_path, monkeypatch):
    monkeypatch.setenv("KIRO_GATEWAY_TRAY_HOME", str(tmp_path))
    from kiro_gateway_tray import appconfig
    cfg = appconfig.load()
    cfg.cloudflare.provision_url = "https://prov.example"
    env = appconfig.to_gateway_env(cfg)
    assert env["TELEMETRY_URL"] == "https://prov.example/telemetry"
    assert "INCIDENT_URL" not in env
