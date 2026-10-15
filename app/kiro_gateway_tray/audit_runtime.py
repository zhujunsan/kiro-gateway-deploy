"""Process-wide audit pipeline.

One background thread serves every Supervisor start/stop. ``restart()`` must
not spawn a second thread and must not interrupt an upload that Task 7 adds
beside this pipeline.
"""
from __future__ import annotations

import os
import threading

from .audit_pipeline import AuditPipeline
from .audit_uploader import AuditUploader
from .log import logger

_lock = threading.Lock()
_pipeline: AuditPipeline | None = None
_uploader: AuditUploader | None = None


def ensure() -> AuditPipeline | None:
    """Start the pipeline thread once. A later call reuses the live thread.

    ``KIRO_AUDIT_AUTOSTART=0`` skips the thread. The test suite sets it so a
    Supervisor per test does not leave a process-wide writer pointed at
    whatever data dir the next test inherits.
    """
    global _pipeline, _uploader
    if os.environ.get("KIRO_AUDIT_AUTOSTART") == "0":
        return _pipeline
    with _lock:
        if _pipeline is None or not _pipeline.thread_alive():
            _pipeline = AuditPipeline()
            _pipeline.start(background=True)
        if _uploader is None or not _uploader.thread_alive():
            _uploader = AuditUploader()
            _uploader.start()
        return _pipeline


def flush() -> None:
    """Process ``.ready`` files left by a gateway child that just stopped."""
    with _lock:
        pipeline = _pipeline
    if pipeline is None:
        return
    try:
        pipeline.flush_ready()
    except Exception:
        logger.debug("audit runtime: flush failed", exc_info=True)
