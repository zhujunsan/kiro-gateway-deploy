# app/kiro_gateway_tray/__main__.py
"""Entry dispatch: tray by default, CLI fallback, plus --print-config."""
from __future__ import annotations

import argparse
import os
import sys

# Works both as a package module (python -m kiro_gateway_tray) and as a PyInstaller
# entry script (where there is no parent package for relative imports).
try:
    from . import appconfig, paths, platform_compat
    from .log import logger, setup as _setup_logging
except ImportError:  # frozen entry script: no parent package
    from kiro_gateway_tray import appconfig, paths, platform_compat
    from kiro_gateway_tray.log import logger, setup as _setup_logging

_lock = None


def _acquire_lock() -> bool:
    """Try to acquire an exclusive lock file. Returns False if another instance is running."""
    global _lock
    paths.ensure_dirs()
    _lock = platform_compat.SingleInstanceLock(paths.data_dir() / "kiro-gateway-tray.lock")
    return _lock.acquire()


def _has_display() -> bool:
    if sys.platform.startswith("linux"):
        return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
    return True


def main() -> int:
    parser = argparse.ArgumentParser(prog="kiro-gateway-tray")
    parser.add_argument("--cli", action="store_true", help="force headless CLI mode")
    parser.add_argument("--print-config", action="store_true",
                        help="print config file path and exit")
    parser.add_argument("--run-gateway", action="store_true",
                        help=argparse.SUPPRESS)  # internal: gateway child process
    args = parser.parse_args()

    if args.run_gateway:
        # Child process entry: run only the gateway, no lock/tray/CLI. The parent
        # populated os.environ + CWD before spawning us.
        try:
            from . import gateway
        except ImportError:
            from kiro_gateway_tray import gateway
        return gateway.run_gateway_blocking()

    if args.print_config:
        appconfig.load()
        print(paths.config_file())
        return 0

    _setup_logging()  # parent-process log sink (tray.log); child has its own

    if not _acquire_lock():
        print("Kiro Tray 已在运行中，不允许启动多个实例。", file=sys.stderr)
        logger.warning("another instance is already running; refusing to start")
        return 1

    if not args.cli and _has_display():
        try:
            try:
                from . import tray
            except ImportError:
                from kiro_gateway_tray import tray
            tray.run()
            return 0
        except tray.TrayUnavailable as e:
            print(f"[tray unavailable: {e}] 退化到 CLI 模式", file=sys.stderr)
            logger.info("tray unavailable ({}); falling back to CLI mode", e)

    try:
        from . import cli
    except ImportError:
        from kiro_gateway_tray import cli
    return cli.run()


if __name__ == "__main__":
    raise SystemExit(main())
