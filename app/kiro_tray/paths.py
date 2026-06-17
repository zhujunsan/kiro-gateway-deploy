"""Cross-platform config/data/log directories for the Kiro tray app."""
from __future__ import annotations

import os
from pathlib import Path

from platformdirs import user_config_dir, user_data_dir, user_log_dir

_APP_NAME = "KiroTray"
_APP_AUTHOR = "KiroTray"


def _home_override() -> Path | None:
    raw = os.environ.get("KIRO_TRAY_HOME")
    return Path(raw).expanduser() if raw else None


def config_dir() -> Path:
    base = _home_override()
    return base / "config" if base else Path(user_config_dir(_APP_NAME, _APP_AUTHOR))


def data_dir() -> Path:
    base = _home_override()
    return base / "data" if base else Path(user_data_dir(_APP_NAME, _APP_AUTHOR))


def log_dir() -> Path:
    base = _home_override()
    return base / "logs" if base else Path(user_log_dir(_APP_NAME, _APP_AUTHOR))


def config_file() -> Path:
    return config_dir() / "config.toml"


def ensure_dirs() -> None:
    for d in (config_dir(), data_dir(), log_dir()):
        d.mkdir(parents=True, exist_ok=True)
