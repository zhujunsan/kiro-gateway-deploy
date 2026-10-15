"""Resolve the audit on/off switch and publish it for the gateway child.

The child only reads ``audit/control.json``. Priority (highest first):

1. ``[audit] mode_override`` in config.toml (``off`` or ``best_effort``)
2. the cloud mode just fetched, or the one cached in ``state.json``
3. ``off``
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from .appconfig import AppCfg

MODES = frozenset({"off", "best_effort"})


def audit_dir(root: Path | None = None) -> Path:
    """Return the audit directory, creating it as a private folder on POSIX."""
    from . import paths

    directory = root if root is not None else paths.data_dir() / "audit"
    directory.mkdir(parents=True, exist_ok=True)
    if os.name == "posix":
        try:
            os.chmod(directory, 0o700)
        except OSError:
            pass
    return directory


def _chmod_file(path: Path) -> None:
    if os.name != "posix":
        return
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def atomic_write(path: Path, payload: str) -> None:
    """Replace ``path`` with ``payload``. A crash leaves the previous file intact."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            if os.name == "posix":
                os.fchmod(handle.fileno(), 0o600)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
        temp_name = ""
        _chmod_file(path)
    finally:
        if temp_name:
            try:
                os.unlink(temp_name)
            except OSError:
                pass


def load_state(root: Path) -> dict[str, Any]:
    path = root / "state.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save_state(root: Path, state: dict[str, Any]) -> None:
    atomic_write(root / "state.json", json.dumps(state, ensure_ascii=False, indent=2))


def cached_cloud_mode(state: dict[str, Any]) -> str | None:
    cloud = state.get("cloud")
    if not isinstance(cloud, dict):
        return None
    mode = cloud.get("mode")
    return mode if mode in MODES else None


def resolve_mode(cfg: AppCfg, state: dict[str, Any], cloud_mode: str | None = None) -> str:
    """Pick the mode the gateway child should apply."""
    override = (cfg.audit.mode_override or "").strip()
    if override in MODES:
        return override
    if cloud_mode in MODES:
        return cloud_mode
    cached = cached_cloud_mode(state)
    if cached in MODES:
        return cached
    return "off"


def publish_control(
    root: Path,
    cfg: AppCfg,
    state: dict[str, Any] | None = None,
    cloud_mode: str | None = None,
) -> str:
    """Write ``control.json`` and return the mode that was published."""
    directory = audit_dir(root)
    current = state if state is not None else load_state(directory)
    mode = resolve_mode(cfg, current, cloud_mode)
    max_mb = cfg.audit.max_spool_mb
    if not isinstance(max_mb, int) or isinstance(max_mb, bool) or max_mb <= 0:
        max_mb = 1024
    rotate = cfg.audit.rotate_seconds
    if not isinstance(rotate, int) or isinstance(rotate, bool) or rotate <= 0:
        rotate = 600
    payload = {
        "mode": mode,
        "max_spool_bytes": max_mb * 1024 * 1024,
        "rotate_seconds": rotate,
    }
    atomic_write(directory / "control.json", json.dumps(payload))
    return mode
