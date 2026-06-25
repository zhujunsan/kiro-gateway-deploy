# app/kiro_gateway_tray/gateway.py
"""Run the vendored kiro-gateway as a **child process**.

Running it out-of-process (rather than an in-process thread) means:
  - a restart spawns a fresh interpreter, so the gateway re-reads all config at
    import time (config.py reads env vars on import) — no stale-config caveat;
  - a gateway crash cannot take down the tray UI.

The child is launched by re-executing this same app with the hidden
``--run-gateway`` subcommand. Under PyInstaller (frozen) ``sys.executable`` is
the bundled app binary; from source it is the Python interpreter running the
package. Both are handled by ``_child_command``.

CRITICAL ORDER inside the child (see run_gateway_blocking):
  1. set env vars   (config.py reads them at import time)
  2. os.chdir(data) (legacy mode rewrites credentials.json/state.json in CWD)
  3. add vendor/ to sys.path, THEN import main
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from . import appconfig, paths
from .appconfig import AppCfg


def _candidate_vendor_roots() -> list[Path]:
    here = Path(__file__).resolve().parent
    roots = [here / "vendor"]
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        roots.append(Path(meipass) / "vendor")
    return roots


def _vendor_root() -> Path:
    for r in _candidate_vendor_roots():
        if (r / "main.py").exists():
            return r
    raise RuntimeError(
        "vendored gateway not found; run scripts/vendor_sync.py before building. "
        f"looked in: {[str(r) for r in _candidate_vendor_roots()]}"
    )


def _gateway_env(cfg: AppCfg) -> dict[str, str]:
    """Build the gateway's env vars (does not touch os.environ)."""
    env = appconfig.to_gateway_env(cfg)
    # DEBUG_DIR depends on the runtime log path, so it can't live in the static
    # appconfig defaults. Point it at a *subdirectory* of the log dir: in
    # "errors" mode the gateway rmtree's and recreates DEBUG_DIR on each failed
    # request, so it must never be the log dir itself. Respect a user override
    # if one was set explicitly under [gateway_extra].
    env.setdefault("DEBUG_DIR", str(paths.log_dir() / "debug_logs"))
    return env


def _apply_env(cfg: AppCfg) -> None:
    """Apply the gateway env to this process's os.environ.

    Used by the child (config.py + uvicorn read os.environ at import/run time).
    The parent process must NOT use this — it would leak secrets like
    PROXY_API_KEY/PROFILE_ARN into the tray's own long-lived environment and any
    subprocess it later spawns. The parent passes a per-launch env to Popen
    instead (see GatewayProcess.start).
    """
    for k, v in _gateway_env(cfg).items():
        os.environ[k] = v


def _child_command() -> list[str]:
    """Command to launch the gateway child process.

    Frozen (PyInstaller): re-exec the app binary with the subcommand.
    From source: re-exec the interpreter with ``-m kiro_gateway_tray``.
    """
    if getattr(sys, "frozen", False):
        return [sys.executable, "--run-gateway"]
    return [sys.executable, "-m", "kiro_gateway_tray", "--run-gateway"]


class GatewayProcess:
    """Manage the gateway child process lifecycle."""

    def __init__(self) -> None:
        self._proc: subprocess.Popen | None = None
        self._bootstrap_log = None

    def start(self, cfg: AppCfg) -> None:
        paths.ensure_dirs()
        # Pass the gateway config to the child via Popen(env=...) rather than
        # mutating our own os.environ: the tray is long-lived, and leaking
        # secrets (PROXY_API_KEY/PROFILE_ARN) into its environment would expose
        # them to every later subprocess it spawns.
        env = {**os.environ, **_gateway_env(cfg)}
        if not getattr(sys, "frozen", False):
            # Source mode: CWD is the data dir, so the package isn't importable
            # via `-m` unless its parent (app/) is on PYTHONPATH. Frozen mode
            # re-execs the bundled binary and doesn't need this.
            app_root = Path(__file__).resolve().parent.parent
            existing = env.get("PYTHONPATH", "")
            env["PYTHONPATH"] = (
                f"{app_root}{os.pathsep}{existing}" if existing else str(app_root)
            )
        # Capture the child's stdout/stderr to a file. The child only installs
        # its own loguru sink AFTER importing the vendored gateway, so any early
        # crash (missing vendor, bad env, import error) would otherwise vanish
        # into the parent's stderr — invisible in a windowless .app. This file
        # keeps those bootstrap failures diagnosable.
        bootstrap_path = paths.log_dir() / "gateway-bootstrap.log"
        self._bootstrap_log = open(bootstrap_path, "w", encoding="utf-8")
        self._proc = subprocess.Popen(
            _child_command(),
            cwd=str(paths.data_dir()),
            env=env,
            stdout=self._bootstrap_log,
            stderr=subprocess.STDOUT,
        )

    def _close_bootstrap_log(self) -> None:
        if self._bootstrap_log is not None:
            try:
                self._bootstrap_log.close()
            except OSError:
                pass
            self._bootstrap_log = None

    def stop(self) -> None:
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        self._proc = None
        self._close_bootstrap_log()

    def is_alive(self) -> bool:
        return bool(self._proc and self._proc.poll() is None)


def _setup_child_logging() -> None:
    """In the child: route loguru (gateway) and stdlib logging (uvicorn) to a
    rotating file sink under the app log dir."""
    import logging

    log_dir = paths.log_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = str(log_dir / "gateway.log")

    from loguru import logger as _loguru

    class _InterceptHandler(logging.Handler):
        def emit(self, record):
            try:
                level = _loguru.level(record.levelname).name
            except ValueError:
                level = record.levelno
            frame, depth = logging.currentframe(), 2
            while frame and frame.f_code.co_filename == logging.__file__:
                frame = frame.f_back
                depth += 1
            _loguru.opt(depth=depth, exception=record.exc_info).log(level, record.getMessage())

    intercept = _InterceptHandler()
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        log = logging.getLogger(name)
        log.handlers = [intercept]
        log.propagate = False

    _loguru.add(log_file, rotation="2 MB", retention=5, encoding="utf-8", enqueue=True)


def run_gateway_blocking() -> int:
    """Child entry point: run uvicorn in the foreground until terminated.

    Assumes the parent already populated os.environ with the gateway config and
    set CWD to the data dir. Reads SERVER_HOST/SERVER_PORT from the environment,
    falling back to config if launched standalone.
    """
    if not os.environ.get("SERVER_PORT"):
        _apply_env(appconfig.load())

    vendor = _vendor_root()
    if str(vendor) not in sys.path:
        sys.path.insert(0, str(vendor))

    import uvicorn
    import importlib
    main = importlib.import_module("main")

    _setup_child_logging()

    # Wrap the vendored app in the telemetry side-channel middleware. This is
    # the one injection point that doesn't touch vendor/: build-time vendor_sync
    # overwrites that whole tree. No-op when TELEMETRY_URL is unset.
    from . import telemetry
    app = telemetry.wrap_app(main.app)

    config = uvicorn.Config(
        app=app,
        host=os.environ.get("SERVER_HOST", "127.0.0.1"),
        port=int(os.environ.get("SERVER_PORT", "64005")),
        log_config=getattr(main, "UVICORN_LOG_CONFIG", None),
    )
    # uvicorn installs its own SIGINT/SIGTERM handlers and exits cleanly on
    # terminate(), which is exactly what GatewayProcess.stop() sends.
    uvicorn.Server(config).run()
    return 0
