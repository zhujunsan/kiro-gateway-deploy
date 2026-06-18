# app/kiro_gateway_tray/cloudflared.py
"""Locate the cloudflared binary and manage the cloudflared child process."""
from __future__ import annotations

import platform
import subprocess
import sys
import threading
from pathlib import Path

from . import paths
from .appconfig import AppCfg


def _current_target() -> str:
    sysname = platform.system().lower()
    machine = platform.machine().lower()
    arch = {"x86_64": "amd64", "amd64": "amd64", "arm64": "arm64", "aarch64": "arm64"}[machine]
    return f"{sysname}-{arch}"


def _binary_name() -> str:
    return "cloudflared.exe" if sys.platform.startswith("win") else "cloudflared"


def _candidate_dirs() -> list[Path]:
    here = Path(__file__).resolve().parent.parent   # app/
    dirs = [here / "resources" / "cloudflared" / _current_target()]
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        dirs.append(Path(meipass) / "resources" / "cloudflared" / _current_target())
    return dirs


def _build_log_writer():
    """Return a callable that appends a raw line to a rotating cloudflared.log.

    Mirrors the gateway sink: 2 MB per file, 3 historical files retained.
    Uses a dedicated stdlib logger with a bare formatter so cloudflared's own
    output is preserved verbatim (no extra timestamp/level prefix).
    """
    import logging
    from logging.handlers import RotatingFileHandler

    log_dir = paths.log_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "cloudflared.log"

    logger = logging.getLogger("kiro_gateway_tray.cloudflared.output")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if not logger.handlers:
        handler = RotatingFileHandler(
            log_path, maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8"
        )
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)

    return lambda line: logger.info(line)


def binary_path() -> Path:
    name = _binary_name()
    for d in _candidate_dirs():
        p = d / name
        if p.exists():
            return p
    raise RuntimeError(
        f"cloudflared binary not found for {_current_target()}; "
        f"run scripts/fetch_cloudflared.py. looked in {[str(d) for d in _candidate_dirs()]}"
    )


class CloudflaredProcess:
    """Runs `cloudflared tunnel run --token <run_token>` as a child process."""

    # FRAGILE: connection state is inferred from cloudflared's English stdout.
    # A cloudflared version that rewords these log lines (or a localized build)
    # would silently break is_connected(). If the tray shows "连接中" forever
    # while the tunnel actually works, check these against the pinned
    # cloudflared version's output and update here. Pinned version lives in
    # scripts/fetch_cloudflared.py (CLOUDFLARED_VERSION).
    _LOG_CONNECTED = "Registered tunnel connection"
    _LOG_DISCONNECTED = "Unregistered tunnel connection"

    def __init__(self) -> None:
        self._proc: subprocess.Popen | None = None
        self._connected = False
        self._reader: threading.Thread | None = None

    def start(self, cfg: AppCfg) -> None:
        run_token = cfg.cloudflare.run_token
        if not run_token:
            raise RuntimeError("cloudflare.run_token 未设置，请先完成首启注册。")
        self._connected = False
        cmd = [str(binary_path()), "tunnel", "--no-autoupdate"]
        protocol = getattr(cfg.cloudflare, "protocol", "") or "http2"
        if protocol:
            cmd += ["--protocol", protocol]
        cmd += ["run", "--token", run_token]
        self._proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        self._reader = threading.Thread(target=self._watch_output, daemon=True)
        self._reader.start()

    def _watch_output(self) -> None:
        """Read cloudflared output, detect connection status, and write to log file."""
        proc = self._proc
        if not proc or not proc.stdout:
            return
        logger = _build_log_writer()
        for line in proc.stdout:
            logger(line.rstrip("\n"))
            if self._LOG_CONNECTED in line:
                self._connected = True
            elif self._LOG_DISCONNECTED in line:
                self._connected = False

    def stop(self) -> None:
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        self._connected = False

    def is_alive(self) -> bool:
        return bool(self._proc and self._proc.poll() is None)

    def is_connected(self) -> bool:
        return self._connected
