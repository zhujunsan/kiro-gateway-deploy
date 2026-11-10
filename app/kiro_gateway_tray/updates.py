"""Lightweight update check against GitHub Releases.

Behavior (see plan Task 13):
  - check on startup and whenever the menu is opened, throttled to at most
    once per 4 hours (cached on disk)
  - cache file: <data_dir>/update_check.json
  - all failures are swallowed silently (never bother the user)
  - the check itself never downloads; the "new version" menu line opens the
    installer that matches this machine (browser download), and falls back to
    the release page when this OS/CPU has no published installer

Cache staleness rules (avoid the "高于发布版" confusion after an upgrade):
  - a failed fetch still bumps ``checked_at`` so we don't hammer the (anonymous,
    60 req/hour) GitHub API and trip rate limiting
  - the cache records the app version that wrote it; after an upgrade the
    version mismatch forces a fresh check instead of trusting a pre-upgrade
    ``latest`` that may predate the new release
"""
from __future__ import annotations

import json
import platform
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

import httpx

from . import GITHUB_REPO, __version__, paths
from .httpclient import resolve_proxy
from .log import logger

_TTL_SECONDS = 4 * 60 * 60
_CACHE_NAME = "update_check.json"
_RELEASE_API = "https://api.github.com/repos/{repo}/releases/latest"
_RELEASE_PAGE = "https://github.com/{repo}/releases/latest"
_DOWNLOAD_URL = "https://github.com/{repo}/releases/download/{tag}/{filename}"

# Published installer names (see packaging/make_dist.py and target_arch.py).
# macOS DMGs use amd64/arm64. Windows publishes a single amd64 setup exe
# (it also runs on Windows on ARM via emulation). The menu downloads the Linux
# AppImage (x86_64/aarch64): opening a .deb still needs apt, so the click stays
# on the file the user can run directly. Debian/Ubuntu packages are published
# beside it for apt install.
_MACOS_ARCH = {
    "arm64": "arm64",
    "aarch64": "arm64",
    "x86_64": "amd64",
    "amd64": "amd64",
}
_WINDOWS_ARCH = {
    "amd64": "amd64",
    "x86_64": "amd64",
    "arm64": "amd64",
    "aarch64": "amd64",
}
_LINUX_ARCH = {
    "x86_64": "x86_64",
    "amd64": "x86_64",
    "aarch64": "aarch64",
    "arm64": "aarch64",
}


@dataclass
class UpdateInfo:
    current: str
    latest: str | None
    update_available: bool
    release_url: str
    download_url: str | None = None


@dataclass
class VersionStatus:
    """What the tray's version line needs to render, without reaching into this
    module's private cache helpers.

    ``latest is None`` means no check has landed yet (render "检查中…"). When a
    latest is known, ``upgradable`` says whether it is newer than ``current``;
    both "same" and "we're ahead of the published release" collapse to
    not-upgradable so the UI never shows a confusing "高于发布版"."""
    current: str
    latest: str | None
    upgradable: bool


def _cache_file() -> Path:
    return paths.data_dir() / _CACHE_NAME


def _parse_version(tag: str) -> tuple[int, int, int]:
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", tag or "")
    if not m:
        return (0, 0, 0)
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)))


def _is_newer(current: str, latest: str) -> bool:
    return _parse_version(latest) > _parse_version(current)


def _version_from_tag(tag: str) -> str:
    """Return the version embedded in installer filenames.

    Release tags are ``v1.2.3``; published files use ``1.2.3``.

    Args:
        tag: GitHub release tag, with or without a leading ``v``.

    Returns:
        Version text with a single leading ``v``/``V`` removed, or ``""``.
    """
    text = (tag or "").strip()
    if text[:1] in ("v", "V"):
        return text[1:]
    return text


def _release_tag(tag: str) -> str:
    """Return the GitHub tag used in a release-asset URL.

    Args:
        tag: Version or tag. Bare versions gain a leading ``v``.

    Returns:
        Tag text, empty when ``tag`` is blank.
    """
    text = (tag or "").strip()
    if not text:
        return ""
    if text[:1] in ("v", "V"):
        return text
    return f"v{text}"


def _os_kind(system: str) -> str | None:
    """Map a ``sys.platform`` / ``platform.system()`` value to an installer family.

    Args:
        system: Platform name, any case.

    Returns:
        ``macos``, ``windows``, ``linux``, or None when we ship no installer.
    """
    name = (system or "").lower()
    if name == "darwin" or name.startswith("mac"):
        return "macos"
    if name.startswith("win"):
        return "windows"
    if name.startswith("linux"):
        return "linux"
    return None


def installer_filename(version: str, system: str, machine: str) -> str | None:
    """Return the published installer filename for this version, OS, and CPU.

    Args:
        version: Release tag or version (``v1.2.3`` or ``1.2.3``).
        system: ``sys.platform`` or ``platform.system()``.
        machine: ``platform.machine()``.

    Returns:
        Filename such as ``KiroGatewayTray-1.2.3-macos-arm64.dmg``, or None
        when this OS/CPU has no published installer.
    """
    ver = _version_from_tag(version)
    kind = _os_kind(system)
    arch_table = {
        "macos": _MACOS_ARCH,
        "windows": _WINDOWS_ARCH,
        "linux": _LINUX_ARCH,
    }.get(kind or "")
    if not ver or arch_table is None:
        return None
    arch = arch_table.get(machine.lower())
    if arch is None:
        return None
    if kind == "macos":
        return f"KiroGatewayTray-{ver}-macos-{arch}.dmg"
    if kind == "windows":
        return f"KiroGatewayTray-{ver}-windows-{arch}-setup.exe"
    return f"kiro-gateway-tray-{ver}-linux-{arch}.AppImage"


def download_url_for(
    tag: str,
    *,
    repo: str | None = None,
    system: str | None = None,
    machine: str | None = None,
) -> str | None:
    """Direct download URL for this machine's installer of ``tag``.

    Opening the URL in a browser starts the download. Callers that get None
    should open the release page so the user can still pick a file.

    Args:
        tag: Release tag or version the menu is offering.
        repo: ``owner/repo``. Defaults to ``GITHUB_REPO``.
        system: Platform name. Defaults to ``sys.platform``.
        machine: CPU name. Defaults to ``platform.machine()``.

    Returns:
        ``https://github.com/<repo>/releases/download/<tag>/<filename>``,
        or None when no installer matches.
    """
    filename = installer_filename(
        tag,
        sys.platform if system is None else system,
        platform.machine() if machine is None else machine,
    )
    release_tag = _release_tag(tag)
    if filename is None or not release_tag:
        return None
    return _DOWNLOAD_URL.format(
        repo=repo or GITHUB_REPO,
        tag=quote(release_tag, safe=""),
        filename=quote(filename, safe=""),
    )


def _update_info(current: str, latest: str | None, available: bool) -> UpdateInfo:
    """Build an ``UpdateInfo``, including this machine's download URL."""
    return UpdateInfo(
        current=current,
        latest=latest,
        update_available=available,
        release_url=_RELEASE_PAGE.format(repo=GITHUB_REPO),
        download_url=download_url_for(latest) if latest else None,
    )


def _read_cache() -> dict | None:
    try:
        return json.loads(_cache_file().read_text(encoding="utf-8"))
    except Exception:
        return None


def _write_cache(latest: str | None) -> None:
    try:
        paths.ensure_dirs()
        _cache_file().write_text(
            json.dumps({
                "latest": latest,
                "checked_at": time.time(),
                "app_version": __version__,
            }),
            encoding="utf-8",
        )
    except Exception:
        pass


def _should_check() -> bool:
    cached = _read_cache()
    if not cached:
        return True
    # Just upgraded: the cache was written by a different app version, so its
    # ``latest`` may predate the release for the version we're now running.
    # Force a fresh check instead of waiting out the TTL.
    if cached.get("app_version") != __version__:
        return True
    return (time.time() - cached.get("checked_at", 0)) >= _TTL_SECONDS


def _fetch_latest() -> str | None:
    url = _RELEASE_API.format(repo=GITHUB_REPO)
    resp = httpx.get(
        url, timeout=8, headers={"Accept": "application/vnd.github+json"},
        proxy=resolve_proxy(),
    )
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
    return _update_info(current, latest, True)


def check(current: str | None = None, force: bool = False) -> UpdateInfo:
    """Return update info. Uses cache unless stale (or force=True).

    Never raises: on any failure returns update_available=False.
    """
    current = current or __version__
    try:
        if force or _should_check():
            logger.info("update check: querying GitHub releases")
            latest = _fetch_latest()
            if latest is not None:
                _write_cache(latest)
            else:
                # Failed fetch (network error or 403 rate limit). Fall back to
                # the cached value but still bump ``checked_at`` so we honour
                # the TTL instead of retrying on every menu open and burning
                # the anonymous 60 req/hour budget.
                latest = (_read_cache() or {}).get("latest")
                _write_cache(latest)
        else:
            latest = (_read_cache() or {}).get("latest")
    except Exception:
        latest = (_read_cache() or {}).get("latest")

    available = bool(latest) and _is_newer(current, latest)
    return _update_info(current, latest, available)


def version_status(current: str | None = None) -> VersionStatus:
    """Read-only view of the cached latest version for the tray's version line.

    No network: reflects whatever the background ``check()`` last wrote. Keeps
    the tray out of this module's private cache/compare helpers."""
    current = current or __version__
    latest = (_read_cache() or {}).get("latest")
    upgradable = bool(latest) and _is_newer(current, latest)
    return VersionStatus(current=current, latest=latest, upgradable=upgradable)
