"""Announcement bar shown at the top of the tray menu, under the update notice.

Behavior:
  - fetched from the provision Worker (POST /announcements, authed with the
    activation code) on startup and once an hour thereafter
  - cache file: <data_dir>/announcements.json
  - all failures are swallowed silently: an announcement is never worth an error
    dialog, and the last successful payload keeps rendering until it is replaced
    or expires
  - at most MAX_ITEMS entries ever reach the menu

Who sees what is decided entirely by the Worker (see worker/src/announcements.js):
this module sends the identity it has — anonymous username plus a
``User-Agent`` of the form ``KiroGatewayTray/<version> (<platform>)`` — and
renders whatever comes back. The only filtering done locally is dropping
entries whose ``ends_at`` has passed, which matters when the app has been offline
long enough for the cache to outlive the announcement.

Cache staleness rules mirror ``updates.py``:
  - a failed fetch still bumps ``fetched_at`` so a Worker outage doesn't turn
    into a retry storm
  - the cache records the app version that wrote it; after an upgrade the version
    mismatch forces a fresh check, because version-targeted announcements may
    have become (in)applicable
"""
from __future__ import annotations

import json
import re
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import httpx

from . import __version__, appconfig, paths
from .httpclient import resolve_proxy
from .log import logger

#: Hard cap on rendered announcements. The Worker caps too; this is the local
#: guarantee that a bad payload can never flood the menu.
MAX_ITEMS = 5

_TTL_SECONDS = 60 * 60
_CACHE_NAME = "announcements.json"
_HTTP_TIMEOUT = 10
_ENDPOINT_PATH = "/announcements"

#: Menu rows are single-line by design (multi-line titles only render properly on
#: macOS), so bodies get whitespace-collapsed and truncated. Long content belongs
#: behind ``url``.
_MAX_BODY_CHARS = 120
_MAX_TAG_CHARS = 24
_ELLIPSIS = "…"

_WHITESPACE_RE = re.compile(r"\s+")
_HTTP_URL_RE = re.compile(r"^https?://", re.IGNORECASE)

_LEVEL_EMOJI = {
    "info": "📢",
    "warning": "⚠️",
    "critical": "🚨",
}
_DEFAULT_LEVEL = "info"


@dataclass(frozen=True)
class _Endpoint:
    """Everything needed to ask the Worker, resolved once per check."""
    url: str
    shared_secret: str
    username: str


@dataclass(frozen=True)
class Announcement:
    """One rendered menu row.

    ``dimmed`` is cloud-controlled: when true the tray renders the row gray
    (``enabled=False``). ``url`` is independent — empty means a click is a
    no-op; having a url does not by itself decide gray vs normal.

    ``ends_at`` is a Unix timestamp used to drop the entry locally once it
    expires, so a stale cache can't keep showing a finished notice.
    """
    id: int
    body: str
    tag: str = ""
    url: str = ""
    level: str = _DEFAULT_LEVEL
    dimmed: bool = False
    ends_at: int | None = None


def _coerce_bool(value: object) -> bool:
    """Accept JSON bools and the 0/1 integers D1/SQL often use."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value == 1
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes")
    return False


def _coerce_positive_id(value: object) -> int | None:
    """Require a positive integer announcement id; reject bools and junk."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        item_id = int(value)
    except (TypeError, ValueError):
        return None
    return item_id if item_id > 0 else None


def platform_slug() -> str:
    """Platform identifier for ``User-Agent`` / ``target_platforms`` matching.

    Returns "" on anything unrecognised; the Worker treats an unknown platform as
    "don't show platform-scoped announcements", which is the safe default.
    """
    if sys.platform == "darwin":
        return "macos"
    if sys.platform == "win32":
        return "windows"
    if sys.platform.startswith("linux"):
        return "linux"
    return ""


def user_agent() -> str:
    """Tray User-Agent, e.g. ``KiroGatewayTray/0.4.22 (macos)``.

    The Worker reads version and platform from this header (same idea as a
    browser UA), so announcement targeting does not depend on body fields.
    """
    plat = platform_slug()
    base = f"KiroGatewayTray/{__version__}"
    return f"{base} ({plat})" if plat else base


def _cache_file() -> Path:
    return paths.data_dir() / _CACHE_NAME


def _collapse(text: object, limit: int) -> str:
    """Flatten arbitrary text into one bounded menu-safe line.

    Newlines would break the row on Windows/Linux and tabs are the tray's
    gray-suffix delimiter, so every whitespace run collapses to a single space.
    """
    if not isinstance(text, str):
        return ""
    flat = _WHITESPACE_RE.sub(" ", text).strip()
    if len(flat) > limit:
        return flat[: limit - 1].rstrip() + _ELLIPSIS
    return flat


def _safe_url(value: object) -> str:
    """Accept only http(s) links.

    The Worker already filters these, but this value ends up in
    ``webbrowser.open`` — a second check here means a compromised or
    misconfigured backend still can't hand the client a ``file://`` target.
    """
    if not isinstance(value, str):
        return ""
    url = value.strip()
    return url if _HTTP_URL_RE.match(url) else ""


def _coerce_ends_at(value: object) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _parse_item(raw: object) -> Announcement | None:
    """Build an Announcement from one payload entry, or None if unusable."""
    if not isinstance(raw, dict):
        return None
    item_id = _coerce_positive_id(raw.get("id"))
    body = _collapse(raw.get("body"), _MAX_BODY_CHARS)
    if item_id is None or not body:
        return None
    level = raw.get("level")
    return Announcement(
        id=item_id,
        body=body,
        tag=_collapse(raw.get("tag"), _MAX_TAG_CHARS),
        url=_safe_url(raw.get("url")),
        level=level if level in _LEVEL_EMOJI else _DEFAULT_LEVEL,
        dimmed=_coerce_bool(raw.get("dimmed")),
        ends_at=_coerce_ends_at(raw.get("ends_at")),
    )


def _parse_items(raw: object) -> list[Announcement]:
    if not isinstance(raw, list):
        return []
    items = [parsed for parsed in map(_parse_item, raw) if parsed is not None]
    return items[:MAX_ITEMS]


def _drop_expired(items: list[Announcement], now: float | None = None) -> list[Announcement]:
    now = time.time() if now is None else now
    return [i for i in items if i.ends_at is None or i.ends_at > now]


def menu_title(item: Announcement) -> str:
    """Render one tray menu row.

    A ``\\t`` splits the title into main text and a right-aligned gray tag on
    macOS (see macos_menu.install_menu_gray_suffix); other platforms show it as
    plain trailing text, which still reads fine.
    """
    emoji = _LEVEL_EMOJI.get(item.level, _LEVEL_EMOJI[_DEFAULT_LEVEL])
    line = f"{emoji} {item.body}"
    return f"{line}\t{item.tag}" if item.tag else line


def _read_cache() -> dict | None:
    try:
        return json.loads(_cache_file().read_text(encoding="utf-8"))
    except Exception:
        return None


def _write_cache(items: list[Announcement]) -> None:
    try:
        paths.ensure_dirs()
        _cache_file().write_text(
            json.dumps({
                "items": [asdict(i) for i in items],
                "fetched_at": time.time(),
                "app_version": __version__,
            }, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception:
        logger.debug("announcements: cache write failed", exc_info=True)


def _should_check() -> bool:
    cached = _read_cache()
    if not cached:
        return True
    # Just upgraded: version-targeted announcements may now apply (or stop
    # applying), so don't wait out the TTL on a pre-upgrade answer.
    if cached.get("app_version") != __version__:
        return True
    return (time.time() - cached.get("fetched_at", 0)) >= _TTL_SECONDS


def _resolve_endpoint(cfg) -> _Endpoint | None:
    """Where and as whom to ask, or None while setup is still incomplete.

    Distinguishing "not ready yet" from "the request failed" matters on first
    run: the activation code is persisted by a background thread shortly after
    the tray starts, so the very first check can legitimately arrive too early.
    Treating that as a failure would stamp the hourly TTL and leave a brand-new
    user without announcements for an hour.
    """
    provision_url = cfg.cloudflare.provision_url
    shared_secret = cfg.cloudflare.shared_secret
    if not provision_url or not shared_secret:
        return None
    try:
        from . import provision
        username = provision._get_username(cfg)
    except Exception:
        # No Kiro token yet (first run, or a non-standard install). Without a
        # username the Worker cannot target anything.
        logger.debug("announcements: username unavailable", exc_info=True)
        return None
    return _Endpoint(
        url=provision_url.rstrip("/") + _ENDPOINT_PATH,
        shared_secret=shared_secret,
        username=username,
    )


def _fetch(endpoint: _Endpoint) -> list[Announcement] | None:
    """POST the Worker once. Returns the list, or None when nothing was learned.

    The None-vs-empty distinction is load-bearing: an empty list is a real answer
    ("everything has been taken down") and must overwrite the cache, while None
    means we failed to ask and the previous answer should stand.
    """
    try:
        resp = httpx.post(
            endpoint.url,
            headers={"User-Agent": user_agent()},
            json={
                "shared_secret": endpoint.shared_secret,
                "username": endpoint.username,
            },
            timeout=_HTTP_TIMEOUT,
            proxy=resolve_proxy(),
        )
    except httpx.HTTPError:
        logger.debug("announcements: request failed", exc_info=True)
        return None

    if resp.status_code != 200:
        # 404 is expected against a Worker that predates this feature; anything
        # else is worth a debug line but still not worth bothering the user.
        logger.debug("announcements: worker returned {}", resp.status_code)
        return None

    try:
        return _parse_items(resp.json().get("announcements"))
    except Exception:
        logger.debug("announcements: malformed payload", exc_info=True)
        return None


def peek_cached(now: float | None = None) -> list[Announcement]:
    """Announcements from the on-disk cache only (no network).

    Used by the tray so rows can appear on the first menu open without waiting
    for a fetch. Entries past their ``ends_at`` are dropped here rather than at
    write time, so an app left running (or asleep) still retires them on schedule.
    """
    cached = _read_cache() or {}
    items = _parse_items(cached.get("items"))
    return _drop_expired(items, now)


def check(force: bool = False) -> list[Announcement]:
    """Refresh if the cache is stale (or force=True) and return what to render.

    Never raises: on any failure the previously cached announcements are
    returned, so a Worker outage degrades to "slightly stale" rather than
    "the bar vanishes".
    """
    try:
        if force or _should_check():
            endpoint = _resolve_endpoint(appconfig.load(use_cache=True))
            if endpoint is None:
                # Setup not finished. Leave the TTL untouched so the first check
                # after activation isn't delayed by a full hour.
                return peek_cached()
            items = _fetch(endpoint)
            if items is None:
                # The request failed. Re-stamp the existing entries so a Worker
                # outage doesn't turn every menu open into a retry.
                _write_cache(peek_cached())
            else:
                logger.info("announcements: fetched {} item(s)", len(items))
                _write_cache(items)
    except Exception:
        logger.warning("announcements: check failed", exc_info=True)
    return peek_cached()
