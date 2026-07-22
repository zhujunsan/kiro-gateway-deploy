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
"""
from __future__ import annotations

import json
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


def before_send(event: dict[str, Any], hint: dict[str, Any]) -> dict[str, Any] | None:
    """Drop non-actionable exits and scrub secrets from outbound events.

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
            event_scrubber=EventScrubber(denylist=denylist, recursive=True),
            integrations=[
                # Breadcrumbs only — ERROR log lines must not become duplicate
                # Issues alongside capture_exception / framework handlers.
                LoguruIntegration(level="INFO", event_level=None),
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
    "capture_exception",
    "flush",
    "init_sentry",
    "install_debug_snapshot_bridge",
    "install_gateway_verify_route",
    "release_name",
    "report_incident_snapshot",
    "resolve_dsn",
]
