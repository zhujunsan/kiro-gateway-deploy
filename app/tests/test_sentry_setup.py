# app/tests/test_sentry_setup.py
"""Tests for Sentry init helpers: DSN resolution, scrubbing, verify middleware."""
from __future__ import annotations

import logging

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


def test_before_send_drops_addr_in_use_oserror():
    import errno

    event = {"message": "bind failed"}
    exc = OSError(errno.EADDRINUSE, "Address already in use")
    assert ss.before_send(event, {"exc_info": (OSError, exc, None)}) is None


def test_before_send_drops_addr_in_use_message_event():
    event = {
        "message": "error while attempting to bind on address ('127.0.0.1', 64005): "
        "[errno 48] address already in use",
    }
    assert ss.before_send(event, {}) is None


def test_before_send_drops_addr_in_use_uvicorn_logentry():
    """LoggingIntegration path: uvicorn.error logentry, no exc_info."""
    event = {
        "logger": "uvicorn.error",
        "logentry": {
            "formatted": (
                "[Errno 48] error while attempting to bind on address "
                "('127.0.0.1', 64005): address already in use"
            ),
        },
    }
    assert ss.before_send(event, {}) is None


def test_before_send_drops_addr_in_use_exception_values():
    event = {
        "exception": {
            "values": [
                {
                    "type": "OSError",
                    "value": "[Errno 48] Address already in use",
                }
            ]
        }
    }
    assert ss.before_send(event, {}) is None


def test_before_send_keeps_unrelated_oserror():
    event = {"message": "disk full"}
    exc = OSError(28, "No space left on device")
    assert ss.before_send(event, {"exc_info": (OSError, exc, None)}) is event


def test_before_send_drops_client_disconnect_by_tags():
    event = {
        "message": "Gateway incident: client_disconnect (cancelled) /v1/chat/completions",
        "tags": {
            "incident.code": "client_disconnect",
            "incident.source": "cancelled",
            "incident.client_disconnected": "true",
        },
    }
    assert ss.before_send(event, {}) is None


def test_before_send_drops_client_disconnect_by_message():
    event = {
        "message": (
            "Gateway incident: client_disconnect (cancelled) "
            "/v1/responses status=200: client disconnected"
        ),
    }
    assert ss.before_send(event, {}) is None


def test_before_send_drops_expected_upstream_invalid_model():
    event = {
        "message": (
            "Gateway incident: INVALID_MODEL_ID (expected_upstream) "
            "/v1/chat/completions status=400"
        ),
        "tags": [
            ["incident.code", "INVALID_MODEL_ID"],
            ["incident.source", "expected_upstream"],
        ],
    }
    assert ss.before_send(event, {}) is None


def test_before_send_drops_application_startup_failed_logentry():
    event = {
        "logger": "uvicorn.error",
        "message": "Application startup failed. Exiting.",
        "logentry": {"message": "Application startup failed. Exiting."},
    }
    assert ss.before_send(event, {}) is None


def test_before_send_drops_failed_to_initialize_account_logentry_duplicate():
    """uvicorn.error traceback-as-message must not open a second Issue (TRAY-1W)."""
    event = {
        "logger": "uvicorn.error",
        "message": (
            "Traceback (most recent call last):\n"
            "  File \"main.py\", line 513, in lifespan\n"
            "    raise RuntimeError(\"Failed to initialize any account\")\n"
            "RuntimeError: Failed to initialize any account"
        ),
    }
    assert ss.before_send(event, {}) is None


def test_before_send_keeps_failed_to_initialize_account_exception():
    """Real startup root cause must remain observable (TRAY-W class)."""
    event = {
        "exception": {
            "values": [
                {
                    "type": "RuntimeError",
                    "value": "Failed to initialize any account",
                }
            ]
        }
    }
    assert ss.before_send(event, {
        "exc_info": (RuntimeError, RuntimeError("Failed to initialize any account"), None),
    }) is event


class TestSignedOutFiltering:
    """Being signed out of Kiro is user state, not an application fault.

    Sentry KIRO-GATEWAY-TRAY-D was 1195 events / 36 users of exactly this, with
    one account reaching ``consecutive=6883``. The tray now shows an actionable
    menu prompt and stops polling, so these events add nothing — but genuine
    outages (DNS / TLS / timeout / proxy) must still report.
    """

    def test_drops_usage_auth_required_message(self):
        event = {"message": "GET /usage requires Kiro re-login (usage_auth_required)"}
        assert ss.before_send(event, {}) is None

    def test_drops_account_auth_required_tag(self):
        event = {
            "message": "gateway degraded",
            "tags": {"incident.code": "account_auth_required"},
        }
        assert ss.before_send(event, {}) is None

    def test_drops_account_not_configured_context(self):
        event = {
            "message": "gateway degraded",
            "contexts": {"account": {"code": "account_not_configured"}},
        }
        assert ss.before_send(event, {}) is None

    def test_drops_login_required_tag(self):
        event = {"message": "x", "tags": {"login_required": "true"}}
        assert ss.before_send(event, {}) is None

    def test_drops_login_required_context_flag(self):
        event = {"message": "x", "contexts": {"account": {"login_required": True}}}
        assert ss.before_send(event, {}) is None

    def test_drops_invalid_grant_text(self):
        event = {"message": "token refresh failed: invalid_grant"}
        assert ss.before_send(event, {}) is None

    def test_drops_legacy_oidc_400_outage_from_old_clients(self):
        """The exact shape released 0.4.2x/0.4.3x clients still emit.

        Those builds cannot be fixed by a gateway change, so the pattern is
        filtered rather than waiting for everyone to upgrade.
        """
        event = {
            "message": (
                "GET /usage upstream unreachable (consecutive=6883): "
                "HTTPStatusError: Client error '400 Bad Request' for url "
                "'https://oidc.ap-northeast-1.amazonaws.com/token'"
            ),
            "tags": {"usage_outage": "true", "subsystem": "usage_upstream"},
        }
        assert ss.before_send(event, {}) is None

    @pytest.mark.parametrize(
        "detail",
        [
            "ConnectError: All connection attempts failed",
            "ConnectError: [Errno 8] nodename nor servname provided, or not known",
            "ConnectError: [SSL: SSLV3_ALERT_HANDSHAKE_FAILURE] sslv3 alert",
            "ReadTimeout:",
            "ConnectTimeout:",
        ],
    )
    def test_keeps_genuine_usage_outages(self, detail):
        """Network failures are actionable and must survive the filter."""
        event = {
            "message": f"GET /usage upstream unreachable (consecutive=5): {detail}",
            "tags": {"usage_outage": "true", "subsystem": "usage_upstream"},
        }
        assert ss.before_send(event, {}) is event

    def test_keeps_oidc_5xx_outage(self):
        """A 500 from the token endpoint is an AWS problem, not a bad login."""
        event = {
            "message": (
                "GET /usage upstream unreachable (consecutive=5): HTTPStatusError: "
                "Server error '503 Service Unavailable' for url "
                "'https://oidc.ap-northeast-1.amazonaws.com/token'"
            ),
            "tags": {"usage_outage": "true"},
        }
        assert ss.before_send(event, {}) is event

    def test_keeps_connect_failure_to_oidc_host(self):
        """Same host, but a transport failure — still a real outage."""
        event = {
            "message": (
                "GET /usage upstream unreachable (consecutive=5): ConnectError: "
                "connection to https://oidc.ap-northeast-1.amazonaws.com/token failed"
            ),
        }
        assert ss.before_send(event, {}) is event

    def test_keeps_unrelated_400_without_oidc_host(self):
        event = {"message": "Kiro returned 400 Bad Request for /v1/chat/completions"}
        assert ss.before_send(event, {}) is event

    def test_tolerates_list_shaped_tags(self):
        event = {
            "message": "x",
            "tags": [["incident.code", "usage_auth_required"]],
        }
        assert ss.before_send(event, {}) is None

    def test_tolerates_non_dict_contexts(self):
        event = {"message": "harmless", "contexts": {"trace": "not-a-dict"}}
        assert ss.before_send(event, {}) is event


def test_before_send_drops_client_validation_error_incident():
    """Empty-body 422 from a misconfigured client is not a gateway bug (TRAY-1X)."""
    event = {
        "message": (
            "Gateway incident: validation_error (client_request) /v1/messages "
            "status=422: Validation error: Field required"
        ),
        "tags": {
            "incident.code": "validation_error",
            "incident.source": "client_request",
            "incident.status_code": "422",
        },
        "contexts": {
            "incident": {
                "code": "validation_error",
                "source": "client_request",
                "status_code": 422,
            }
        },
    }
    assert ss.before_send(event, {}) is None


def test_before_send_drops_client_request_400_incident():
    """Any client_request 4xx (except rate-limit-class noise we keep elsewhere)."""
    event = {
        "message": "Gateway incident: http_400 (client_request) /v1/messages status=400",
        "tags": [
            ["incident.code", "http_400"],
            ["incident.source", "client_request"],
            ["incident.status_code", "400"],
        ],
    }
    assert ss.before_send(event, {}) is None


def test_before_send_keeps_network_502_incident():
    """Upstream transport failures must remain visible."""
    event = {
        "message": (
            "Gateway incident: incomplete_upstream_response (network) "
            "/v1/messages status=502"
        ),
        "tags": {
            "incident.code": "incomplete_upstream_response",
            "incident.source": "network",
            "incident.status_code": "502",
        },
    }
    assert ss.before_send(event, {}) is event


def test_before_send_drops_xlib_display_connection_closed():
    """Desktop session teardown is not an application bug (TRAY-1S)."""
    event = {
        "exception": {
            "values": [
                {
                    "type": "ConnectionClosedError",
                    "value": "Display connection closed by server: [Errno 104]",
                }
            ]
        }
    }
    assert ss.before_send(event, {}) is None


def test_before_send_drops_vendored_gateway_not_found():
    """Incomplete source checkout is a local setup issue (TRAY-1V)."""
    event = {
        "exception": {
            "values": [
                {
                    "type": "RuntimeError",
                    "value": "vendored gateway not found; run scripts/vendor_sync.py",
                }
            ]
        }
    }
    assert ss.before_send(event, {
        "exc_info": (
            RuntimeError,
            RuntimeError("vendored gateway not found; run scripts/vendor_sync.py"),
            None,
        ),
    }) is None


def test_before_send_drops_starlette_http_exception_504():
    """Starlette-captured network HTTPException duplicates the incident Issue (TRAY-H)."""
    class HTTPException(Exception):
        def __init__(self, status_code: int, detail: str) -> None:
            super().__init__(detail)
            self.status_code = status_code
            self.detail = detail

    exc = HTTPException(504, "Read timeout - server stopped responding during data transfer.")
    event = {
        "exception": {
            "values": [
                {
                    "type": "HTTPException",
                    "value": str(exc),
                }
            ]
        }
    }
    assert ss.before_send(event, {"exc_info": (HTTPException, exc, None)}) is None


def test_before_send_keeps_first_token_streaming_error_incident():
    """TRAY-K class first-token timeout incidents must not be filtered."""
    event = {
        "message": (
            "Gateway incident: streaming_error (gateway) /v1/chat/completions "
            "status=500: 504: Model did not respond within 30.0s after 3 attempts."
        ),
        "tags": {
            "incident.code": "streaming_error",
            "incident.source": "gateway",
            "incident.status_code": "500",
        },
        "contexts": {
            "incident": {
                "code": "streaming_error",
                "source": "gateway",
                "client_disconnected": False,
            }
        },
    }
    assert ss.before_send(event, {}) is event


def test_report_incident_skips_client_validation_error(monkeypatch):
    """Primary drop: snapshot path must not create Issues for 422 client faults."""
    captured: dict = {}

    class _FakeSdk:
        @staticmethod
        def new_scope():
            raise AssertionError("validation_error must not open a Sentry scope")

        @staticmethod
        def capture_message(*a, **k):
            captured["called"] = True

    monkeypatch.setattr(ss, "_READY", True)
    monkeypatch.setitem(__import__("sys").modules, "sentry_sdk", _FakeSdk)

    ss.report_incident_snapshot({
        "incident_id": "val-1",
        "path": "/v1/messages",
        "source": "client_request",
        "code": "validation_error",
        "client_disconnected": False,
        "status_code": 422,
        "error_message": "Validation error: Field required",
        "artifacts": {},
    })
    assert "called" not in captured


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
    logging_kwargs: list[dict] = []
    loguru_kwargs: list[dict] = []

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

    class _FakeLogging:
        def __init__(self, **kwargs):
            logging_kwargs.append(kwargs)

    class _FakeLoguru:
        def __init__(self, **kwargs):
            loguru_kwargs.append(kwargs)

    class _FakeScrubber:
        def __init__(self, **kwargs):
            pass

    monkeypatch.setenv("SENTRY_DSN", "https://key@o1.ingest.sentry.io/99")
    monkeypatch.setitem(__import__("sys").modules, "sentry_sdk", _FakeSdk)
    monkeypatch.setitem(
        __import__("sys").modules,
        "sentry_sdk.integrations.logging",
        type("m", (), {"LoggingIntegration": _FakeLogging})(),
    )
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
    assert calls[0]["before_send_log"] is ss.before_send_log
    assert calls[0]["enable_logs"] is True
    assert logging_kwargs == [{
        "level": logging.INFO,
        "event_level": logging.ERROR,
        "sentry_logs_level": logging.WARNING,
    }]
    assert loguru_kwargs == [{
        "level": "INFO",
        "event_level": None,
        "sentry_logs_level": logging.WARNING,
    }]


@pytest.mark.parametrize(
    "payload",
    [
        {"severity_text": "trace"},
        {"severity_text": "DEBUG"},
        {"severity_text": "info"},
        {"severity_text": " INFO "},
        {"severity_number": 9},
        {"severity_text": "info", "severity_number": 9},
        {"severity_text": "warning", "severity_number": 11},
    ],
)
def test_before_send_log_drops_below_warn(payload):
    assert ss.before_send_log(payload, {}) is None


@pytest.mark.parametrize(
    "payload",
    [
        {"severity_text": "warning"},
        {"severity_text": "WARN"},
        {"severity_text": "error"},
        {"severity_text": "fatal"},
        {"severity_number": 13},
        {"severity_text": "warning", "severity_number": 13},
        {"severity_text": "error", "severity_number": 17},
        {},
    ],
)
def test_before_send_log_keeps_warn_and_above(payload):
    assert ss.before_send_log(payload, {"ignored": True}) is payload


def test_before_send_log_does_not_mutate_kept_log():
    log = {"severity_text": "error", "body": "gateway failed"}
    kept = ss.before_send_log(log, {})
    assert kept is log
    assert log["body"] == "gateway failed"

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


def test_report_incident_skips_client_disconnect(monkeypatch):
    captured: dict = {}

    class _FakeSdk:
        @staticmethod
        def new_scope():
            raise AssertionError("client_disconnect must not open a Sentry scope")

        @staticmethod
        def capture_message(*a, **k):
            captured["called"] = True

    monkeypatch.setattr(ss, "_READY", True)
    monkeypatch.setitem(__import__("sys").modules, "sentry_sdk", _FakeSdk)

    ss.report_incident_snapshot({
        "incident_id": "disc-1",
        "path": "/v1/chat/completions",
        "source": "cancelled",
        "code": "client_disconnect",
        "client_disconnected": True,
        "status_code": 200,
        "error_message": "client disconnected",
        "artifacts": {},
    })
    assert "called" not in captured


def test_report_incident_skips_expected_upstream_invalid_model(monkeypatch):
    captured: dict = {}

    class _FakeSdk:
        @staticmethod
        def new_scope():
            raise AssertionError("INVALID_MODEL_ID must not open a Sentry scope")

        @staticmethod
        def capture_message(*a, **k):
            captured["called"] = True

    monkeypatch.setattr(ss, "_READY", True)
    monkeypatch.setitem(__import__("sys").modules, "sentry_sdk", _FakeSdk)

    ss.report_incident_snapshot({
        "incident_id": "model-1",
        "path": "/v1/chat/completions",
        "source": "expected_upstream",
        "code": "INVALID_MODEL_ID",
        "client_disconnected": False,
        "status_code": 400,
        "error_message": "The requested model is unavailable for this account.",
        "artifacts": {},
    })
    assert "called" not in captured


def test_report_incident_keeps_first_token_streaming_error(monkeypatch):
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
            pass

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
        "incident_id": "ttft-1",
        "path": "/v1/chat/completions",
        "source": "gateway",
        "code": "streaming_error",
        "phase": "streaming",
        "client_disconnected": False,
        "status_code": 500,
        "error_message": "504: Model did not respond within 30.0s after 3 attempts.",
        "artifacts": {},
    })
    assert "streaming_error" in captured["message"]
    assert captured["level"] == "error"
    assert "streaming_error" in captured["fingerprint"]


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
