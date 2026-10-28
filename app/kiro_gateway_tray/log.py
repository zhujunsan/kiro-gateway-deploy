"""Logging for the tray (parent) process, plus the shared file-sink installer.

The gateway runs in a child process with its own loguru file sink (see
gateway._setup_child_logging). This module gives the parent process its own
rotating ``tray.log`` so UI/supervisor failures are diagnosable instead of
vanishing into ``except Exception: pass`` or stderr that nobody sees once the
app is a windowless menu-bar agent.

Both processes install their file sink through ``add_file_sink``, which owns
the degradation policy when the OS refuses the resources a sink needs:

  1. ``enqueue=True``  — preferred: writes happen on loguru's worker thread.
     On macOS this allocates ~7 named POSIX semaphores per sink; when the
     system-wide pool (``kern.posix.sem.max``) is exhausted, ``sem_open`` fails
     with ``ENOSPC`` ("No space left on device") although the disk is fine.
  2. ``enqueue=False`` — same file, synchronous writes, no semaphores.
  3. no file sink     — keep only the already-installed stderr sink, which the
     tray captures into ``gateway-bootstrap.log`` for the child.

Logging setup must never crash the process for an OS-level reason, so only
``OSError`` is absorbed; programming errors (bad arguments) still raise.

Use ``logger`` for messages. ``setup()`` is idempotent and safe to call from any
entry point (tray, CLI).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from loguru import logger

from . import paths, sentry_setup

_READY = False

SinkMode = Literal["enqueued", "synchronous", "stderr_only"]

# Sentry breadcrumb category for a degraded log sink.
FALLBACK_BREADCRUMB_CATEGORY = "logging.fallback"


@dataclass(frozen=True)
class FileSinkResult:
    """Outcome of ``add_file_sink``.

    Attributes:
        mode: Which rung of the degradation ladder was reached.
        handler_id: Loguru handler id of the file sink, ``None`` when no file
            sink could be installed.
        errors: ``"<ExcType>: <message>"`` for every failed attempt, in order.
    """

    mode: SinkMode
    handler_id: int | None
    errors: tuple[str, ...] = field(default_factory=tuple)

    @property
    def degraded(self) -> bool:
        """True when the preferred enqueued sink could not be installed."""
        return self.mode != "enqueued"


def _describe(error: OSError) -> str:
    """Render an OSError compactly for logs and breadcrumbs.

    Args:
        error: The failure raised while installing a sink.

    Returns:
        ``"<ExcType>: <message>"``; the message already carries errno text.
    """
    return f"{type(error).__name__}: {error}"


def _try_add(target: Any, path: Path, *, enqueue: bool, sink_options: dict[str, Any]) -> int:
    """Create the log directory and add one file sink.

    Args:
        target: Loguru logger to attach the sink to.
        path: Log file path.
        enqueue: Forwarded to ``logger.add``.
        sink_options: Remaining ``logger.add`` keyword arguments.

    Returns:
        The new loguru handler id.

    Raises:
        OSError: The directory, the file, or the enqueue machinery could not
            be created.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    return target.add(str(path), enqueue=enqueue, **sink_options)


def _report_degradation(target: Any, component: str, path: Path, result: FileSinkResult) -> None:
    """Record a degraded sink locally (WARNING) and as one Sentry breadcrumb.

    The WARNING is marked local-only so the Sentry loguru integration neither
    turns it into a second breadcrumb nor ships it to Sentry Logs; the explicit
    breadcrumb below is the single Sentry-side record and is only sent along
    with a later event. No event is reported for the degradation itself.

    Args:
        target: Loguru logger used for the local WARNING.
        component: ``"tray"`` or ``"gateway"`` — which process degraded.
        path: Log file that could not be opened as intended.
        result: Outcome of the install attempts.
    """
    if result.mode == "synchronous":
        effect = "file logging continues without the background writer"
    else:
        effect = "file logging disabled; only stderr output remains"
    target.bind(**{sentry_setup.LOCAL_ONLY_EXTRA: True}).warning(
        "{} log sink degraded to {} ({}): {}. Log file: {}",
        component, result.mode, effect, "; ".join(result.errors), path,
    )
    sentry_setup.add_breadcrumb(
        category=FALLBACK_BREADCRUMB_CATEGORY,
        message=f"{component} log sink degraded to {result.mode}",
        level="warning",
        # File name only: the full path embeds the OS user name.
        data={"component": component, "mode": result.mode,
              "file": path.name, "errors": list(result.errors)},
    )


def add_file_sink(target: Any, path: Path, *, component: str, **sink_options: Any) -> FileSinkResult:
    """Attach a file sink, degrading instead of raising on OS-level failures.

    Args:
        target: Loguru logger to attach the sink to.
        path: Log file path; its parent directory is created if missing.
        component: Process label for diagnostics (``"tray"`` / ``"gateway"``).
        **sink_options: Extra ``logger.add`` arguments (rotation, retention,
            encoding, level, ...). ``enqueue`` is owned by this function.

    Returns:
        A ``FileSinkResult`` describing which mode was reached.

    Raises:
        Any non-``OSError`` raised by loguru (e.g. ``TypeError`` / ``ValueError``
        for invalid options) — those are bugs, not environment problems.
    """
    errors: list[str] = []
    result: FileSinkResult | None = None
    for enqueue, mode in ((True, "enqueued"), (False, "synchronous")):
        try:
            handler_id = _try_add(target, path, enqueue=enqueue, sink_options=sink_options)
        except OSError as e:
            errors.append(_describe(e))
            continue
        result = FileSinkResult(mode=mode, handler_id=handler_id, errors=tuple(errors))
        break
    if result is None:
        result = FileSinkResult(mode="stderr_only", handler_id=None, errors=tuple(errors))
    if result.degraded:
        _report_degradation(target, component, path, result)
    return result


def setup() -> FileSinkResult | None:
    """Install the tray's rotating ``tray.log`` sink once per process.

    Returns:
        The install outcome on the first call, ``None`` on repeat calls.
    """
    global _READY
    if _READY:
        return None
    result = add_file_sink(
        logger,
        paths.log_dir() / "tray.log",
        component="tray",
        rotation="2 MB",
        retention=3,
        encoding="utf-8",
        level="DEBUG",
    )
    _READY = True
    return result


__all__ = ["FileSinkResult", "add_file_sink", "logger", "setup"]
