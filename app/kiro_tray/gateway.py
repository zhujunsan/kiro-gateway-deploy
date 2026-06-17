# app/kiro_tray/gateway.py
"""Run the vendored kiro-gateway in-process on a background thread.

CRITICAL ORDER (see plan 关键约束 #1/#2):
  1. set env vars   (config.py reads them at import time)
  2. os.chdir(data) (legacy mode rewrites credentials.json/state.json in CWD)
  3. add vendor/ to sys.path, THEN import main
"""
from __future__ import annotations

import os
import sys
import threading
from pathlib import Path

from . import appconfig, paths
from .appconfig import AppCfg


def _candidate_vendor_roots() -> list[Path]:
    here = Path(__file__).resolve().parent
    roots = [here / "vendor"]
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        roots.append(Path(meipass) / "kiro_tray" / "vendor")
    return roots


def _vendor_root() -> Path:
    for r in _candidate_vendor_roots():
        if (r / "main.py").exists():
            return r
    raise RuntimeError(
        "vendored gateway not found; run scripts/vendor_sync.py before building. "
        f"looked in: {[str(r) for r in _candidate_vendor_roots()]}"
    )


def _apply_env(cfg: AppCfg) -> None:
    for k, v in appconfig.to_gateway_env(cfg).items():
        os.environ[k] = v


class GatewayThread:
    def __init__(self) -> None:
        self._server = None
        self._thread: threading.Thread | None = None

    def start(self, cfg: AppCfg) -> None:
        _apply_env(cfg)
        paths.ensure_dirs()
        os.chdir(paths.data_dir())
        vendor = _vendor_root()
        if str(vendor) not in sys.path:
            sys.path.insert(0, str(vendor))
        import uvicorn
        main = __import__("main")
        config = uvicorn.Config(
            app=main.app,
            host=os.environ["SERVER_HOST"],
            port=int(os.environ["SERVER_PORT"]),
            log_config=getattr(main, "UVICORN_LOG_CONFIG", None),
        )
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, daemon=True, name="kiro-gateway")
        self._thread.start()

    def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=10)

    def is_alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive())
