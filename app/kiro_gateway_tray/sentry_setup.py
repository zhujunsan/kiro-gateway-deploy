# app/kiro_gateway_tray/sentry_setup.py
"""Sentry error / tracing init for tray (parent) and gateway (child) processes.

DSN resolution order:
  1. ``SENTRY_DSN`` env var — empty string explicitly disables reporting
  2. ``DEFAULT_DSN`` baked into the build (public; DSN only allows ingest)

Gateway request failures are reported via the vendor ``debug_logger`` snapshot
callback (same payloads previously uploaded to Cloudflare Workers Logs):
metadata as tags/context, request/response bodies as attachments.

Auth headers and secret-looking frame locals are still scrubbed. Request and
response bodies are intentionally retained — they are the primary debugging
signal for gateway incidents.

Sentry Logs (the byte-quota product) only receive ``warning`` and above.
INFO access lines and update-check chatter previously exhausted the free 5 GB.
"""
from __future__ import annotations

import errno
import json
import logging
import os
import sys
from typing import Any, Literal

# Public client key — safe to ship in the binary (ingest-only).
DEFAULT_DSN = (
    "https://feaed57f43188bebb3436a3949c4df05@o51827.ingest.us.sentry.io/4511777499709440"
)

ProcessKind = Literal["tray", "gateway"]

# Per-artifact upload caps. Sentry accepts large attachments; we still bound
# each file so a pathological multi-MB stream cannot stall the request path.
_MAX_ATTACHMENT_BYTES = 5 * 1024 * 1024
# Small text artifacts are also mirrored into event context for inline viewing.
_MAX_CONTEXT_PREVIEW_BYTES = 100 * 1024

_SENSITIVE_HEADER_NAMES = frozenset({
    "authorization",
    "cookie",
    "x-api-key",
    "x-amz-security-token",
    "proxy-authorization",
})

_SENSITIVE_VAR_NAMES = frozenset({
    "password",
    "passwd",
    "secret",
    "token",
    "access_token",
    "refresh_token",
    "proxy_api_key",
    "api_key",
    "authorization",
    "profile_arn",
    "client_secret",
    "shared_secret",
    "telemetry_secret",
    "run_token",
})

_READY = False
_SNAPSHOT_BRIDGE_INSTALLED = False

# OpenTelemetry SeverityNumber: warn starts at 13. INFO/SUCCESS sit at 9–11.
_OTEL_SEVERITY_WARN = 13
_SENTRY_LOGS_DROP_TEXT = frozenset({"trace", "debug", "info"})
# Breadcrumbs stay at INFO so error events still have request context.
# Structured Logs and stdlib capture start at WARNING.
_SENTRY_LOGS_LEVEL = logging.WARNING


def resolve_dsn(env: dict[str, str] | None = None) -> str:
    """Resolve the effective DSN, honouring an explicit empty override.

    Args:
        env: Environment mapping; defaults to ``os.environ``.

    Returns:
        DSN string, or ``""`` when reporting should stay disabled.
    """
    e = os.environ if env is None else env
    if "SENTRY_DSN" in e:
        return (e.get("SENTRY_DSN") or "").strip()
    return DEFAULT_DSN.strip()


def release_name(version: str | None = None) -> str:
    """Build the Sentry release string ``kiro-gateway-tray@<version>``.

    Args:
        version: Explicit version; defaults to package ``__version__``.

    Returns:
        Release identifier for Sentry.
    """
    if version is None:
        from . import __version__ as version
    return f"kiro-gateway-tray@{version}"


def _environment() -> str:
    if os.environ.get("SENTRY_ENVIRONMENT"):
        return os.environ["SENTRY_ENVIRONMENT"].strip() or "production"
    if getattr(sys, "frozen", False):
        return "production"
    return "development"


def _scrub_headers(event: dict[str, Any]) -> None:
    request = event.get("request")
    if not isinstance(request, dict):
        return
    headers = request.get("headers")
    if not isinstance(headers, dict):
        return
    for key in list(headers):
        if str(key).lower() in _SENSITIVE_HEADER_NAMES:
            headers[key] = "[Filtered]"


def _scrub_frame_vars(event: dict[str, Any]) -> None:
    exception = event.get("exception")
    if not isinstance(exception, dict):
        return
    values = exception.get("values")
    if not isinstance(values, list):
        return
    for exc in values:
        if not isinstance(exc, dict):
            continue
        stacktrace = exc.get("stacktrace")
        if not isinstance(stacktrace, dict):
            continue
        frames = stacktrace.get("frames")
        if not isinstance(frames, list):
            continue
        for frame in frames:
            if not isinstance(frame, dict):
                continue
            vars_ = frame.get("vars")
            if not isinstance(vars_, dict):
                continue
            for key in list(vars_):
                lowered = str(key).lower()
                if lowered in _SENSITIVE_VAR_NAMES or any(
                    part in lowered for part in ("token", "secret", "password", "api_key")
                ):
                    vars_[key] = "[Filtered]"


# OSError.errno values for "address already in use" across platforms.
_ADDR_IN_USE_ERRNOS = frozenset({
    errno.EADDRINUSE,
    getattr(errno, "WSAEADDRINUSE", 10048),
})

# Client/account feedback from Kiro — not gateway bugs (see kiro_errors).
_EXPECTED_UPSTREAM_CODES = frozenset({
    "INVALID_MODEL_ID",
})

# Gateway codes meaning "the user must sign in to Kiro again". Kept as literals
# rather than imported from login_state so this filter also matches events from
# gateway versions that predate those constants.
_SIGNED_OUT_CODES = frozenset({
    "usage_auth_required",
    "account_auth_required",
    "account_not_configured",
})

# Client-error markers that, combined with the AWS SSO OIDC token endpoint, mean
# our refresh token was rejected. 4xx only: a 5xx or a connect failure against
# the same host is a real outage and must still report.
_OIDC_REJECT_MARKERS = (
    "400 bad request",
    "401 unauthorized",
    "403 forbidden",
    "invalid_grant",
    "invalid_client",
)

# Incident sources that describe the caller's request, not the gateway. The
# client sent something the gateway correctly rejected (malformed body, missing
# fields), so there is nothing for us to fix and every misconfigured client
# would otherwise open an Issue.
_CLIENT_FAULT_SOURCES = frozenset({
    "client_request",
})

# Incident codes that are always the caller's fault, regardless of source.
_CLIENT_FAULT_CODES = frozenset({
    "validation_error",
})

# Gateway statuses that mean "the request was wrong", excluding 429 (rate limit,
# which is account state worth tracking) and 5xx (our side).
_CLIENT_FAULT_STATUSES = frozenset({400, 401, 403, 404, 405, 413, 415, 422})


def _exception_text(exc: BaseException) -> str:
    """Flatten exception type + message for substring matching."""
    return f"{type(exc).__name__}: {exc}".lower()


def _looks_like_addr_in_use(text: str) -> bool:
    """True when ``text`` describes a TCP bind conflict (EADDRINUSE)."""
    lowered = text.lower()
    needles = (
        "address already in use",
        "eaddrinuse",
        "only one usage of each socket address",  # Windows English
        "通常每个套接字地址",  # Windows Chinese WSAEADDRINUSE
    )
    return any(n in lowered for n in needles)


def _tag_map(event: dict[str, Any]) -> dict[str, str]:
    """Normalize event tags to a flat ``str → str`` map.

    Sentry may present tags as a dict or as a list of ``[key, value]`` pairs
    (or ``{"key": ..., "value": ...}`` objects) depending on SDK stage.
    """
    tags = event.get("tags")
    if isinstance(tags, dict):
        return {str(k): str(v) for k, v in tags.items()}
    if not isinstance(tags, list):
        return {}
    out: dict[str, str] = {}
    for item in tags:
        if isinstance(item, (list, tuple)) and len(item) >= 2:
            out[str(item[0])] = str(item[1])
        elif isinstance(item, dict) and "key" in item:
            out[str(item["key"])] = str(item.get("value") or "")
    return out


def _event_text_parts(event: dict[str, Any]) -> list[str]:
    """Collect human-readable strings from message / logentry / exception."""
    parts: list[str] = []
    message = event.get("message")
    if message:
        parts.append(str(message))

    logentry = event.get("logentry")
    if isinstance(logentry, dict):
        for key in ("formatted", "message"):
            val = logentry.get(key)
            if val:
                parts.append(str(val))

    exception = event.get("exception")
    if isinstance(exception, dict):
        values = exception.get("values")
        if isinstance(values, list):
            for item in values:
                if not isinstance(item, dict):
                    continue
                parts.append(f"{item.get('type') or ''} {item.get('value') or ''}")
    return parts


def _event_text_blob(event: dict[str, Any]) -> str:
    """Lowercased concatenation of event text parts for substring matching."""
    return " ".join(_event_text_parts(event)).lower()


def _is_addr_in_use_event(event: dict[str, Any], hint: dict[str, Any]) -> bool:
    """Detect port-bind conflicts that tray already surfaces to the user.

    These are environmental (another process holds the gateway port), not
    actionable application bugs — drop them to cut Sentry noise.

    Covers:
      * ``OSError`` / ``WSAEADDRINUSE`` via ``hint["exc_info"]``
      * exception values on the event
      * uvicorn / LoggingIntegration message + logentry text
    """
    exc_info = hint.get("exc_info")
    if exc_info and len(exc_info) >= 2 and isinstance(exc_info[1], BaseException):
        exc = exc_info[1]
        if isinstance(exc, OSError) and getattr(exc, "errno", None) in _ADDR_IN_USE_ERRNOS:
            return True
        if _looks_like_addr_in_use(_exception_text(exc)):
            return True

    for part in _event_text_parts(event):
        if _looks_like_addr_in_use(part):
            return True
    return False


def _coerce_status(value: Any) -> int | None:
    """Parse an incident status code from a tag/context value."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _is_client_fault_incident(code: str, source: str, status: int | None) -> bool:
    """True when an incident describes a bad client request, not a gateway bug."""
    if code in _CLIENT_FAULT_CODES:
        return True
    if source in _CLIENT_FAULT_SOURCES and status in _CLIENT_FAULT_STATUSES:
        return True
    return False


def _is_noisy_incident_event(event: dict[str, Any]) -> bool:
    """True for client-caused / expected-upstream incidents that must not Issue.

    Primary drop is in ``report_incident_snapshot``; this is defense-in-depth for
    events that already carry incident tags / message text.
    """
    tags = _tag_map(event)
    if tags.get("incident.client_disconnected", "").lower() == "true":
        return True
    code = tags.get("incident.code", "")
    source = tags.get("incident.source", "")
    if code == "client_disconnect" or source == "cancelled":
        return True
    if source == "expected_upstream" or code in _EXPECTED_UPSTREAM_CODES:
        return True
    if _is_client_fault_incident(
        code, source, _coerce_status(tags.get("incident.status_code"))
    ):
        return True

    contexts = event.get("contexts")
    if isinstance(contexts, dict):
        incident = contexts.get("incident")
        if isinstance(incident, dict):
            if incident.get("client_disconnected"):
                return True
            if str(incident.get("code") or "") == "client_disconnect":
                return True
            if str(incident.get("source") or "") in ("cancelled", "expected_upstream"):
                return True
            if str(incident.get("code") or "") in _EXPECTED_UPSTREAM_CODES:
                return True
            if _is_client_fault_incident(
                str(incident.get("code") or ""),
                str(incident.get("source") or ""),
                _coerce_status(incident.get("status_code")),
            ):
                return True

    blob = _event_text_blob(event)
    if "gateway incident: client_disconnect" in blob:
        return True
    if "gateway incident: invalid_model_id" in blob and "expected_upstream" in blob:
        return True
    if "gateway incident: validation_error" in blob:
        return True
    return False


def _is_duplicate_startup_log_event(event: dict[str, Any], hint: dict[str, Any]) -> bool:
    """Drop uvicorn's wrapper logentries that duplicate a real exception Issue.

    Covers:
      * ``Application startup failed. Exiting.``
      * Traceback-as-message dumps of ``Failed to initialize any account``
        (Sentry TRAY-1W; the real Issue is TRAY-W)

    Keep exception events; only drop pure message / logentry events.
    """
    exc_info = hint.get("exc_info")
    if exc_info and len(exc_info) >= 2 and isinstance(exc_info[1], BaseException):
        return False

    exception = event.get("exception")
    if isinstance(exception, dict):
        values = exception.get("values")
        if isinstance(values, list) and any(isinstance(v, dict) for v in values):
            return False

    blob = _event_text_blob(event)
    if "application startup failed" in blob:
        return True
    if "failed to initialize any account" in blob:
        return True
    return False


def _is_signed_out_event(event: dict[str, Any], hint: dict[str, Any]) -> bool:
    """Drop events that only mean "the user is signed out of Kiro".

    Being signed out is user state, not an application fault: the tray now shows
    an actionable menu prompt and stops polling, so a Sentry Issue adds nothing.
    Sentry KIRO-GATEWAY-TRAY-D was 1195 events / 36 users of exactly this, and
    one account alone reached ``consecutive=6883`` because each poll re-reported.

    Matched in three ways, newest first:

    1. tags/contexts carrying the gateway's stable auth codes;
    2. ``usage_outage`` events whose text is an OIDC token 400 — the shape older
       gateways produced before credential failures were classified apart;
    3. bare ``invalid_grant`` text.

    Keep genuine outages (DNS/TLS/timeout/proxy): those are actionable.
    """
    tags = _tag_map(event)
    for key in ("incident.code", "error.code", "code", "usage_auth_reason"):
        if tags.get(key, "") in _SIGNED_OUT_CODES:
            return True
    if tags.get("login_required", "").lower() == "true":
        return True

    contexts = event.get("contexts")
    if isinstance(contexts, dict):
        for section in contexts.values():
            if not isinstance(section, dict):
                continue
            if str(section.get("code") or "") in _SIGNED_OUT_CODES:
                return True
            if section.get("login_required") is True:
                return True

    blob = _event_text_blob(event)
    if any(code in blob for code in _SIGNED_OUT_CODES):
        return True
    if "invalid_grant" in blob:
        return True

    # Legacy shape: released gateways report a signed-out account as an
    # "upstream unreachable" outage whose error is a 400 from the OIDC token
    # endpoint. Those clients cannot be fixed by a gateway change, so filter the
    # pattern rather than wait for everyone to upgrade.
    if _looks_like_oidc_token_rejection(blob):
        return True
    return False


def _looks_like_oidc_token_rejection(blob: str) -> bool:
    """True when text describes an OIDC token endpoint rejecting our credentials.

    Deliberately narrow: it must mention the token endpoint *and* a client-error
    status, so a 500 or a connect failure against the same host still reports.

    Args:
        blob: Lowercased event text.

    Returns:
        Whether the text is a credential rejection from AWS SSO OIDC.
    """
    if "oidc." not in blob or "amazonaws.com/token" not in blob:
        return False
    return any(marker in blob for marker in _OIDC_REJECT_MARKERS)


def _is_environment_noise_event(event: dict[str, Any], hint: dict[str, Any]) -> bool:
    """Drop desktop / local-setup failures that are not application bugs.

    Covers:
      * Xlib / pystray display connection resets (user logged out / tray host died)
      * Missing vendored gateway when running from an incomplete source checkout
      * Starlette-captured HTTPException 502/504 that already have an incident Issue
        from ``debug_logger.flush_on_error``
    """
    exc_info = hint.get("exc_info")
    if exc_info and len(exc_info) >= 2 and isinstance(exc_info[1], BaseException):
        exc = exc_info[1]
        name = type(exc).__name__
        text = _exception_text(exc)
        if name == "ConnectionClosedError" or "display connection closed" in text:
            return True
        if "vendored gateway not found" in text:
            return True
        # FastAPI HTTPException is not always importable here; match by shape.
        status = getattr(exc, "status_code", None)
        if status in (502, 504) and name == "HTTPException":
            return True

    blob = _event_text_blob(event)
    if "display connection closed" in blob:
        return True
    if "vendored gateway not found" in blob:
        return True

    exception = event.get("exception")
    if isinstance(exception, dict):
        values = exception.get("values")
        if isinstance(values, list):
            for item in values:
                if not isinstance(item, dict):
                    continue
                typ = str(item.get("type") or "")
                val = str(item.get("value") or "").lower()
                if typ == "ConnectionClosedError" or "display connection closed" in val:
                    return True
                if typ == "HTTPException" and (
                    "read timeout" in val
                    or "connection failed" in val
                    or "request timeout" in val
                    or "did not respond within" in val
                ):
                    return True
    return False


def _should_skip_incident_snapshot(snapshot: dict[str, Any]) -> bool:
    """Return True when a debug_logger snapshot must not create a Sentry Issue.

    Drops:
      * client disconnect / cancelled (user abort — not a bug)
      * expected_upstream rejections (esp. INVALID_MODEL_ID)
      * client-fault requests (4xx validation errors from a misconfigured caller)

    Keeps actionable incidents such as first-token timeouts and truncated
    upstream responses.
    """
    if bool(snapshot.get("client_disconnected")):
        return True
    code = str(snapshot.get("code") or "")
    source = str(snapshot.get("source") or "")
    if code == "client_disconnect" or source == "cancelled":
        return True
    if source == "expected_upstream" or code in _EXPECTED_UPSTREAM_CODES:
        return True
    if _is_client_fault_incident(
        code, source, _coerce_status(snapshot.get("status_code"))
    ):
        return True
    return False


def before_send(event: dict[str, Any], hint: dict[str, Any]) -> dict[str, Any] | None:
    """Drop non-actionable noise and scrub secrets from outbound events.

    Args:
        event: Sentry event payload about to be sent.
        hint: SDK hint (may contain ``exc_info``).

    Returns:
        Mutated event, or ``None`` to drop it.
    """
    exc_info = hint.get("exc_info")
    if exc_info and exc_info[0] is not None:
        try:
            if issubclass(exc_info[0], (KeyboardInterrupt, SystemExit)):
                return None
        except TypeError:
            pass

    if _is_addr_in_use_event(event, hint):
        return None
    if _is_noisy_incident_event(event):
        return None
    if _is_duplicate_startup_log_event(event, hint):
        return None
    if _is_signed_out_event(event, hint):
        return None
    if _is_environment_noise_event(event, hint):
        return None

    _scrub_headers(event)
    _scrub_frame_vars(event)
    return event


def _traces_sampler(sampling_context: dict[str, Any]) -> float:
    """Sample HTTP traces lightly; drop health / static probes."""
    parent = sampling_context.get("parent_sampled")
    if parent is not None:
        return float(parent)

    txn = sampling_context.get("transaction_context") or {}
    name = str(txn.get("name") or "")
    lowered = name.lower()
    if any(part in lowered for part in ("/health", "/ready", "/favicon", "/speedtest")):
        return 0.0
    if txn.get("op") == "http.server":
        return 0.15
    return 0.05


def before_send_log(
    log: dict[str, Any],
    hint: dict[str, Any],
) -> dict[str, Any] | None:
    """Drop info/debug/trace so Sentry Logs only keep warn+error.

    Integrations already filter at WARNING, but uvicorn access logs and
    loguru INFO can arrive via more than one path. This is the last gate
    before ingest, independent of which integration captured the record.

    Args:
        log: Sentry structured log payload (severity_text / severity_number).
        hint: SDK hint dict; unused, required by the callback signature.

    Returns:
        The original ``log`` when severity is warning or higher, otherwise
        ``None`` to discard.
    """
    del hint
    text = str(log.get("severity_text") or "").strip().lower()
    if text in _SENTRY_LOGS_DROP_TEXT:
        return None
    number = log.get("severity_number")
    if isinstance(number, int) and number < _OTEL_SEVERITY_WARN:
        return None
    return log


def init_sentry(*, process: ProcessKind) -> bool:
    """Initialize the Sentry SDK for this process.

    Must run before the FastAPI/Starlette app object is constructed in the
    gateway child so auto-instrumentation can wrap the ASGI stack.

    Args:
        process: ``"tray"`` for the parent UI process, ``"gateway"`` for the
            uvicorn child.

    Returns:
        ``True`` when the SDK was initialized, ``False`` when disabled / failed.
    """
    global _READY
    if _READY:
        return True

    dsn = resolve_dsn()
    if not dsn:
        return False

    try:
        import sentry_sdk
        from sentry_sdk.integrations.logging import LoggingIntegration
        from sentry_sdk.integrations.loguru import LoguruIntegration
        from sentry_sdk.scrubber import DEFAULT_DENYLIST, EventScrubber
    except ImportError:
        return False

    denylist = list(DEFAULT_DENYLIST) + sorted(_SENSITIVE_VAR_NAMES)

    try:
        sentry_sdk.init(
            dsn=dsn,
            environment=_environment(),
            release=release_name(),
            send_default_pii=False,
            # Keep ASGI request bodies on framework-captured exceptions; gateway
            # incident snapshots also attach bodies explicitly.
            max_request_body_size="always",
            traces_sampler=_traces_sampler,
            enable_logs=True,
            before_send=before_send,
            before_send_log=before_send_log,
            event_scrubber=EventScrubber(denylist=denylist, recursive=True),
            integrations=[
                # Default LoggingIntegration ships INFO into Sentry Logs
                # (uvicorn.access via callHandlers even when propagate=False).
                LoggingIntegration(
                    level=logging.INFO,
                    event_level=logging.ERROR,
                    sentry_logs_level=_SENTRY_LOGS_LEVEL,
                ),
                # Breadcrumbs at INFO; do not turn ERROR log lines into Issues
                # (those already go through capture_exception / snapshots).
                LoguruIntegration(
                    level="INFO",
                    event_level=None,
                    sentry_logs_level=_SENTRY_LOGS_LEVEL,
                ),
            ],
            in_app_include=["kiro_gateway_tray", "kiro"],
        )
        sentry_sdk.set_tag("process", process)
        username = (os.environ.get("TELEMETRY_USERNAME") or "").strip()
        if username and username != "unknown":
            sentry_sdk.set_user({"id": username, "username": username})
        upstream = (os.environ.get("GATEWAY_UPSTREAM_SHA") or "").strip()
        if upstream:
            sentry_sdk.set_tag("upstream_sha", upstream)
    except Exception:
        return False

    _READY = True
    return True


def capture_exception(error: BaseException | None = None) -> None:
    """Best-effort capture; never raises.

    Args:
        error: Exception to report; ``None`` captures the active ``exc_info``.
    """
    if not _READY:
        return
    try:
        import sentry_sdk
        if error is None:
            sentry_sdk.capture_exception()
        else:
            sentry_sdk.capture_exception(error)
    except Exception:
        pass


def flush(timeout: float = 2.0) -> None:
    """Flush the Sentry transport before process exit.

    Args:
        timeout: Seconds to wait for pending events.
    """
    if not _READY:
        return
    try:
        import sentry_sdk
        sentry_sdk.flush(timeout=timeout)
    except Exception:
        pass


def _artifact_content_type(name: str) -> str:
    lowered = name.lower()
    if lowered.endswith(".json"):
        return "application/json"
    if lowered.endswith(".txt"):
        return "text/plain"
    return "application/octet-stream"


def _decode_preview(data: bytes, *, limit: int = _MAX_CONTEXT_PREVIEW_BYTES) -> str | None:
    """Return a UTF-8 preview of ``data``, or ``None`` if not useful as text."""
    chunk = data[:limit]
    try:
        text = chunk.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if len(data) > limit:
        text += f"\n… [truncated {len(data) - limit} bytes]"
    return text


def _incident_metadata(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Strip bulky artifacts and keep searchable incident fields."""
    artifacts = snapshot.get("artifacts") or {}
    artifact_bytes = {
        name: len(blob) if isinstance(blob, (bytes, bytearray)) else 0
        for name, blob in artifacts.items()
    }
    return {
        "incident_id": snapshot.get("incident_id") or "",
        "path": snapshot.get("path") or "",
        "model": snapshot.get("model") or "unknown",
        "stream": snapshot.get("stream"),
        "status_code": snapshot.get("status_code"),
        "gateway_status": snapshot.get("gateway_status"),
        "upstream_status": snapshot.get("upstream_status"),
        "source": snapshot.get("source") or "unknown",
        "code": snapshot.get("code") or "unknown",
        "phase": snapshot.get("phase") or "unknown",
        "client_disconnected": bool(snapshot.get("client_disconnected")),
        "error_message": str(snapshot.get("error_message") or "")[:2000],
        "duration_ms": int(snapshot.get("duration_ms") or 0),
        "ts": snapshot.get("ts"),
        "artifact_names": sorted(artifacts.keys()),
        "artifact_bytes": artifact_bytes,
        "username": (os.environ.get("TELEMETRY_USERNAME") or "unknown").strip() or "unknown",
        "upstream_sha": (os.environ.get("GATEWAY_UPSTREAM_SHA") or "unknown").strip() or "unknown",
        "app_version": (os.environ.get("APP_VERSION") or "").strip() or None,
    }


def report_incident_snapshot(snapshot: dict[str, Any]) -> None:
    """Send a ``debug_logger`` error snapshot to Sentry with full artifacts.

    Args:
        snapshot: Immutable-enough dict from vendor ``DebugSession.build_snapshot``.
    """
    if not _READY or not isinstance(snapshot, dict):
        return
    if _should_skip_incident_snapshot(snapshot):
        return
    try:
        import sentry_sdk
    except ImportError:
        return

    try:
        meta = _incident_metadata(snapshot)
        source = str(meta["source"])
        code = str(meta["code"])
        path = str(meta["path"])
        status = meta.get("status_code")
        err = str(meta.get("error_message") or "")

        with sentry_sdk.new_scope() as scope:
            scope.set_tag("incident.source", source[:64])
            scope.set_tag("incident.code", code[:128])
            scope.set_tag("incident.phase", str(meta.get("phase") or "unknown")[:64])
            if path:
                scope.set_tag("incident.path", path[:128])
            if meta.get("model"):
                scope.set_tag("incident.model", str(meta["model"])[:128])
            if status is not None:
                scope.set_tag("incident.status_code", str(status))
            if meta.get("client_disconnected"):
                scope.set_tag("incident.client_disconnected", "true")

            scope.set_context("incident", meta)

            previews: dict[str, str] = {}
            artifacts = snapshot.get("artifacts") or {}
            if isinstance(artifacts, dict):
                for name, blob in artifacts.items():
                    if not isinstance(name, str):
                        continue
                    if isinstance(blob, bytearray):
                        data = bytes(blob)
                    elif isinstance(blob, bytes):
                        data = blob
                    elif isinstance(blob, str):
                        data = blob.encode("utf-8")
                    else:
                        try:
                            data = json.dumps(blob, ensure_ascii=False).encode("utf-8")
                        except (TypeError, ValueError):
                            data = repr(blob).encode("utf-8", errors="replace")

                    truncated = False
                    attach = data
                    if len(attach) > _MAX_ATTACHMENT_BYTES:
                        attach = attach[:_MAX_ATTACHMENT_BYTES]
                        truncated = True
                    filename = name if not truncated else f"{name}.truncated"
                    scope.add_attachment(
                        bytes=attach,
                        filename=filename,
                        content_type=_artifact_content_type(name),
                    )
                    preview = _decode_preview(data)
                    if preview is not None:
                        # Context keys must stay short; keep basename only.
                        key = name.replace("/", "_")[:40]
                        previews[key] = preview

            if previews:
                scope.set_context("incident_artifacts", previews)

            scope.fingerprint = [
                "kiro-gateway-incident",
                source,
                code,
                path or "{{ default }}",
            ]

            message = f"Gateway incident: {code} ({source})"
            if path:
                message += f" {path}"
            if status is not None:
                message += f" status={status}"
            if err:
                message += f": {err[:500]}"
            level = "warning" if meta.get("client_disconnected") else "error"
            sentry_sdk.capture_message(message, level=level)
    except Exception:
        # Never affect the request path.
        pass


def install_debug_snapshot_bridge() -> bool:
    """Hook vendor ``debug_logger`` so failed-request snapshots go to Sentry.

    Must run AFTER the vendored package is importable. No-op when Sentry is
    disabled or the callback cannot be registered.

    Returns:
        ``True`` when the callback was installed.
    """
    global _SNAPSHOT_BRIDGE_INSTALLED
    if _SNAPSHOT_BRIDGE_INSTALLED:
        return True
    if not _READY:
        return False
    try:
        from kiro.debug_logger import set_error_snapshot_callback
        set_error_snapshot_callback(report_incident_snapshot)
    except Exception:
        return False
    _SNAPSHOT_BRIDGE_INSTALLED = True
    return True


class _SentryVerifyMiddleware:
    """ASGI middleware that raises on ``GET /_sentry_verify`` for setup checks."""

    def __init__(self, app: Any, marker: str) -> None:
        self.app = app
        self.marker = marker

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if (
            scope.get("type") == "http"
            and scope.get("method") == "GET"
            and scope.get("path") == "/_sentry_verify"
        ):
            raise RuntimeError(f"Sentry verify: {self.marker}")
        await self.app(scope, receive, send)


def install_gateway_verify_route(app: Any) -> Any:
    """Optionally wrap the app with ``GET /_sentry_verify`` when ``SENTRY_VERIFY=1``.

    Used only for end-to-end setup confirmation; leave the env unset in normal
    builds so the route does not exist. Implemented as ASGI middleware so it
    still works after telemetry / activity wrappers replace the root app object.

    Args:
        app: ASGI application.

    Returns:
        Wrapped app when verify mode is on, otherwise the original ``app``.
    """
    if (os.environ.get("SENTRY_VERIFY") or "").strip() not in ("1", "true", "yes"):
        return app
    if not _READY:
        return app
    marker = (os.environ.get("SENTRY_VERIFY_MARKER") or "sentry-verify").strip()
    return _SentryVerifyMiddleware(app, marker)


__all__ = [
    "DEFAULT_DSN",
    "before_send",
    "before_send_log",
    "capture_exception",
    "flush",
    "init_sentry",
    "install_debug_snapshot_bridge",
    "install_gateway_verify_route",
    "release_name",
    "report_incident_snapshot",
    "resolve_dsn",
]
