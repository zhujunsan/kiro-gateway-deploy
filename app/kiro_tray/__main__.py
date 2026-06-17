# app/kiro_tray/__main__.py
"""Entry dispatch: tray by default, CLI fallback, plus --print-config."""
from __future__ import annotations

import argparse
import os
import sys

from . import appconfig, paths


def _has_display() -> bool:
    if sys.platform.startswith("linux"):
        return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
    return True


def main() -> int:
    parser = argparse.ArgumentParser(prog="kiro-tray")
    parser.add_argument("--cli", action="store_true", help="force headless CLI mode")
    parser.add_argument("--print-config", action="store_true",
                        help="print config file path and exit")
    args = parser.parse_args()

    if args.print_config:
        appconfig.load()
        print(paths.config_file())
        return 0

    if not args.cli and _has_display():
        try:
            from . import tray
            tray.run()
            return 0
        except tray.TrayUnavailable as e:
            print(f"[tray unavailable: {e}] 退化到 CLI 模式", file=sys.stderr)

    from . import cli
    return cli.run()


if __name__ == "__main__":
    raise SystemExit(main())
