# app/kiro_gateway_tray/cloudflared.py
"""Locate the cloudflared binary and manage the cloudflared child process."""
from __future__ import annotations

import platform
import socket
import subprocess
import sys
import threading
from pathlib import Path

from . import paths
from . import proc_guard
from .appconfig import AppCfg
from .log import logger


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


def _port_is_free(port: int) -> bool:
    """True if a TCP listener can bind 127.0.0.1:<port> right now."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def _pick_metrics_port(preferred: int) -> int:
    """Return the preferred metrics port if free, else an OS-assigned free one.

    cloudflared treats a failed metrics bind as fatal and exits, so a stale
    process (or anything else) holding the configured port would silently kill
    the tunnel on the next start. Falling back to a free port keeps the tunnel
    alive; the supervisor probes whatever port we actually bound.
    """
    if _port_is_free(preferred):
        return preferred
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        chosen = s.getsockname()[1]
    logger.warning(
        "cloudflared metrics port {} is busy; falling back to {}",
        preferred, chosen,
    )
    return chosen


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
    """Runs `cloudflared tunnel run --token <run_token>` as a child process.

    Connection state is determined by probing cloudflared's own metrics
    ``/ready`` endpoint (HTTP 200 once at least one edge connection is
    registered), not by parsing stdout. The metrics server is pinned to a fixed
    port via ``--metrics`` so the probe target is stable. stdout is still
    captured verbatim to a rotating log file for debugging.
    """

    def __init__(self) -> None:
        self._proc: subprocess.Popen | None = None
        self._reader: threading.Thread | None = None
        self._metrics_port: int = 20241

    def start(self, cfg: AppCfg) -> None:
        run_token = cfg.cloudflare.run_token
        if not run_token:
            raise RuntimeError("cloudflare.run_token 未设置，请先完成首启注册。")
        # Reap any cloudflared that survived a previous hard-kill of the tray
        # FIRST, so the preferred metrics port is freed before we pick one;
        # otherwise we'd needlessly fall back off a port the orphan is vacating.
        proc_guard.kill_orphan()
        self._metrics_port = _pick_metrics_port(cfg.cloudflare.metrics_port)
        cmd = [str(binary_path()), "tunnel", "--no-autoupdate"]
        cmd += ["--metrics", f"127.0.0.1:{self._metrics_port}"]
        protocol = getattr(cfg.cloudflare, "protocol", "") or "http2"
        if protocol:
            cmd += ["--protocol", protocol]
        cmd += ["run", "--token", run_token]
        self._proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            **proc_guard.spawn_kwargs(),
        )
        proc_guard.after_spawn(self._proc)
        proc_guard.record_pid(self._proc.pid)
        self._reader = threading.Thread(target=self._watch_output, daemon=True)
        self._reader.start()

    def _watch_output(self) -> None:
        """Mirror cloudflared stdout to the rotating log file (debug only)."""
        proc = self._proc
        if not proc or not proc.stdout:
            return
        logger = _build_log_writer()
        for line in proc.stdout:
            logger(line.rstrip("\n"))

    def stop(self) -> None:
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        proc_guard.clear_pid()

    def is_alive(self) -> bool:
        return bool(self._proc and self._proc.poll() is None)

    @property
    def metrics_port(self) -> int:
        return self._metrics_port
