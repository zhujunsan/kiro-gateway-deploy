# app/kiro_gateway_tray/log.py
"""Logging for the tray (parent) process.

The gateway runs in a child process with its own loguru file sink (see
gateway.run_gateway_blocking). This module gives the parent process its own
rotating ``tray.log`` so UI/supervisor failures are diagnosable instead of
vanishing into ``except Exception: pass`` or stderr that nobody sees once the
app is a windowless menu-bar agent.

Use ``logger`` for messages. ``setup()`` is idempotent and safe to call from any
entry point (tray, CLI). It never raises.
"""
from __future__ import annotations

import sys

from loguru import logger

from . import paths

_READY = False


def setup() -> None:
    global _READY
    if _READY:
        return
    try:
        log_dir = paths.log_dir()
        log_dir.mkdir(parents=True, exist_ok=True)
        logger.add(
            str(log_dir / "tray.log"),
            rotation="2 MB",
            retention=3,
            encoding="utf-8",
            enqueue=True,
            level="DEBUG",
        )
    except Exception:
        # Logging setup must never block app startup; fall back to the default
        # stderr sink that loguru installs.
        pass
    _READY = True


__all__ = ["logger", "setup"]
