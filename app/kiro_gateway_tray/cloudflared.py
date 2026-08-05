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


# Upstream default (and effective ceiling) for cloudflared's hidden
# `ha-connections` flag: it opens 4 independent edge connections per tunnel.
# The real count can be LOWER when the edge hands us fewer usable addresses
# (`if s.config.HAConnections > availableAddrs { ... }`), so callers pass the
# count they actually observed and this only caps it.
HA_CONNECTIONS_MAX = 4

# One line accepted by cloudflared's `stdinControl` goroutine. Each HA
# connection waits on the same unbuffered-ish `reconnectCh`, and the signal is
# consumed by exactly ONE of them ("one randomly chosen connection" per the
# upstream help text), so N connections need N separate commands.
_RECONNECT_COMMAND = "reconnect\n"


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

    stdin is kept open as a control channel (``--stdin-control``) so a network
    change can be answered with an immediate, backoff-free edge reconnect
    instead of killing and respawning the process.
    """

    def __init__(self) -> None:
        self._proc: subprocess.Popen | None = None
        self._reader: threading.Thread | None = None
        self._metrics_port: int = 20241
        # stdin is written from the network-watcher thread while the tray/
        # supervisor threads may be stopping the process; serialize both so
        # commands never interleave mid-line and stdin is not written after
        # close.
        self._stdin_lock = threading.Lock()

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
        # `--stdin-control` is a hidden upstream flag (absent from the public
        # docs) but has been in cloudflared for years and is safe to rely on:
        # if it ever disappears the process fails fast at startup, and every
        # caller of request_reconnect() falls back to a full process restart.
        cmd += ["--stdin-control"]
        cmd += ["run", "--token", run_token]
        # Force UTF-8: on Windows, text=True alone uses the system ANSI code
        # page (often GBK), which raises UnicodeDecodeError on cloudflared's
        # UTF-8 log lines and kills the reader thread (pipe backpressure risk).
        self._proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
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
        write = _build_log_writer()
        try:
            for line in proc.stdout:
                write(line.rstrip("\n"))
        except Exception as exc:  # noqa: BLE001 — last-resort reader-thread guard
            logger.warning("cloudflared stdout reader stopped: {}", exc)

    def request_reconnect(self, count: int) -> bool:
        """Ask cloudflared to rebuild ``count`` edge connections right now.

        Writes the ``reconnect`` control command to cloudflared's stdin. Upstream
        treats a ``ReconnectSignal`` specially: the connection is restarted
        immediately, skipping the exponential backoff (1/2/4/8/16s) and without
        incrementing the retry counter. That is what makes this faster than
        waiting for cloudflared to notice a dead connection on its own.

        The signal is unicast — each command wakes exactly one HA connection —
        so ``count`` commands are written to cover ``count`` connections.

        Args:
            count: Number of connections to recycle, as observed by the caller.
                Clamped to ``1..HA_CONNECTIONS_MAX``.

        Returns:
            True if all commands were written and flushed. False if the process
            is gone or the pipe is unusable, in which case the caller should
            escalate to a full process restart.
        """
        target = max(1, min(int(count), HA_CONNECTIONS_MAX))
        with self._stdin_lock:
            proc = self._proc
            if proc is None or proc.poll() is not None:
                logger.debug("cloudflared reconnect skipped: process not running")
                return False
            stdin = proc.stdin
            if stdin is None:
                logger.debug("cloudflared reconnect skipped: stdin not piped")
                return False
            try:
                for _ in range(target):
                    stdin.write(_RECONNECT_COMMAND)
                stdin.flush()
            # BrokenPipeError: cloudflared died between poll() and write.
            # ValueError: stdin was closed concurrently by stop().
            # OSError: pipe-level failure (also the parent of BrokenPipeError).
            except (BrokenPipeError, ValueError, OSError) as exc:
                logger.warning("cloudflared reconnect command failed: {}", exc)
                return False
        logger.info("cloudflared reconnect requested for {} connection(s)", target)
        return True

    def stop(self) -> None:
        # Close the control channel first: it also gives cloudflared's stdin
        # reader goroutine an EOF instead of leaving it blocked on a pipe we are
        # about to abandon.
        with self._stdin_lock:
            proc = self._proc
            if proc is not None and proc.stdin is not None:
                try:
                    proc.stdin.close()
                except (BrokenPipeError, OSError) as exc:
                    logger.debug("cloudflared stdin close failed: {}", exc)
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
