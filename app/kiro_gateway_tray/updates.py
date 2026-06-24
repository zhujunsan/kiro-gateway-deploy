"""Lightweight update check against GitHub Releases.

Behavior (see plan Task 13):
  - check on startup and whenever the menu is opened, throttled to at most
    once per 10 minutes (cached on disk)
  - cache file: <data_dir>/update_check.json
  - all failures are swallowed silently (never bother the user)
  - never auto-downloads; UI just surfaces a "new version" menu line
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

from . import GITHUB_REPO, __version__, paths

_TTL_SECONDS = 10 * 60
_CACHE_NAME = "update_check.json"
_RELEASE_API = "https://api.github.com/repos/{repo}/releases/latest"
_RELEASE_PAGE = "https://github.com/{repo}/releases/latest"


@dataclass
class UpdateInfo:
    current: str
    latest: str | None
    update_available: bool
    release_url: str


def _cache_file() -> Path:
    return paths.data_dir() / _CACHE_NAME


def _parse_version(tag: str) -> tuple[int, int, int]:
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", tag or "")
    if not m:
        return (0, 0, 0)
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)))


def _is_newer(current: str, latest: str) -> bool:
    return _parse_version(latest) > _parse_version(current)


def _read_cache() -> dict | None:
    try:
        return json.loads(_cache_file().read_text(encoding="utf-8"))
    except Exception:
        return None


def _write_cache(latest: str | None) -> None:
    try:
        paths.ensure_dirs()
        _cache_file().write_text(
            json.dumps({"latest": latest, "checked_at": time.time()}),
            encoding="utf-8",
        )
    except Exception:
        pass


def _should_check() -> bool:
    cached = _read_cache()
    if not cached:
        return True
    return (time.time() - cached.get("checked_at", 0)) >= _TTL_SECONDS


def _fetch_latest() -> str | None:
    url = _RELEASE_API.format(repo=GITHUB_REPO)
    resp = httpx.get(url, timeout=8, headers={"Accept": "application/vnd.github+json"})
    if resp.status_code != 200:
        return None
    return resp.json().get("tag_name")


def peek_cached(current: str | None = None) -> UpdateInfo | None:
    """Return update info from the on-disk cache only (no network).

    Used by the tray menu so the "new version" line can appear on the first
    menu open without waiting for a background fetch to finish.
    """
    current = current or __version__
    latest = (_read_cache() or {}).get("latest")
    if not latest or not _is_newer(current, latest):
        return None
    return UpdateInfo(
        current=current,
        latest=latest,
        update_available=True,
        release_url=_RELEASE_PAGE.format(repo=GITHUB_REPO),
    )


def check(current: str | None = None, force: bool = False) -> UpdateInfo:
    """Return update info. Uses cache unless stale (or force=True).

    Never raises: on any failure returns update_available=False.
    """
    current = current or __version__
    release_url = _RELEASE_PAGE.format(repo=GITHUB_REPO)
    try:
        if force or _should_check():
            latest = _fetch_latest()
            if latest is not None:
                _write_cache(latest)
            else:                       # failed fetch: fall back to cache if any
                latest = (_read_cache() or {}).get("latest")
        else:
            latest = (_read_cache() or {}).get("latest")
    except Exception:
        latest = (_read_cache() or {}).get("latest")

    available = bool(latest) and _is_newer(current, latest)
    return UpdateInfo(current=current, latest=latest,
                      update_available=available, release_url=release_url)
