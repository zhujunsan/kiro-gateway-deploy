# app/kiro_gateway_tray/usage.py
"""Query the gateway's own GET /usage endpoint on localhost."""
from __future__ import annotations

import atexit

import httpx

from . import appconfig
from .httpclient import local_client

# Reused connection pool for localhost gateway calls (usage + models). Avoids
# building a fresh client/connection on every menu refresh. Released at process
# exit so the pool doesn't outlive us during interpreter shutdown. See
# httpclient.local_client for the trust_env=False rationale (avoid a corp proxy
# hijacking 127.0.0.1 probes).
_client = local_client(timeout=30.0)
atexit.register(_client.close)


def _authed_get(path: str, timeout: float) -> httpx.Response:
    """GET a localhost gateway endpoint with the proxy API key. Raises on non-200,
    including the status code and the start of the response body for diagnosis."""
    cfg = appconfig.load()
    url = f"{appconfig.gateway_origin(cfg)}{path}"
    headers = {"Authorization": f"Bearer {cfg.gateway.proxy_api_key}"}
    resp = _client.get(url, headers=headers, timeout=timeout)
    if resp.status_code != 200:
        raise RuntimeError(f"{path} returned {resp.status_code}: {resp.text[:200]}")
    return resp


def fetch(timeout: float = 30.0) -> dict:
    return _authed_get("/usage", timeout).json()


def format_summary(data: dict) -> str:
    sub = data.get("subscription") or "unknown"
    lines = [f"订阅: {sub}"]
    for b in data.get("breakdowns") or []:
        used = b.get("used", 0)
        limit = b.get("limit", 0)
        line = f"  用量: {used} / {limit}"
        overage = b.get("overage", 0) or 0
        if overage > 0:
            line += f" (超额 {overage}, ${b.get('overageCostUsd', 0)})"
        lines.append(line)
    if not data.get("breakdowns"):
        lines.append("  (无用量明细)")
    cost = data.get("overageCostUsd", 0) or 0
    if cost > 0:
        rate = data.get("overageRateUsd", 0.04)
        credits = data.get("overageCreditsTotal", 0)
        lines.append(f"预计超额费用: ${cost} ({credits} credits x ${rate})")
    return "\n".join(lines)


def format_menu_line(data: dict) -> str:
    """One-liner for the tray menu's quota row, e.g. "1732.9 / 1000".

    Uses the first breakdown. Appends the projected overage cost when the
    account is over its monthly limit. Returns "无数据" when there is none.
    """
    breakdowns = data.get("breakdowns") or []
    if not breakdowns:
        return "无数据"
    b = breakdowns[0]
    line = f"{b.get('used', 0)} / {b.get('limit', 0)}"
    cost = data.get("overageCostUsd", 0) or 0
    if cost > 0:
        line += f" (${cost})"
    return line


def fetch_models(timeout: float = 10.0) -> list[str]:
    """Return sorted list of model IDs from the gateway's /v1/models endpoint."""
    data = _authed_get("/v1/models", timeout).json().get("data") or []
    return sorted(m["id"] for m in data if "id" in m)


# Shown only as the real id in the tray menu — no Cursor alias row.
_MENU_NO_ALIAS = frozenset({"auto"})


def split_models_for_menu(
    ids: list[str],
    aliases: dict[str, str] | None = None,
) -> tuple[list[str], list[str]]:
    """Split model IDs into (canonical, aliases) with matching order.

    The gateway list mixes real IDs (``claude-haiku-4.5``) with Cursor-safe
    aliases (``kiro-h-4.5``, ``auto-kiro``). The tray menu shows two blocks;
    paired rows must stay 1:1 so the N-th alias lines up with the N-th
    canonical model above the separator.

    - Canonical entries use the *real* id (``auto``, not ``auto-kiro``).
    - ``auto`` has no alias row and is pinned first in the canonical block.
    - Alias entries follow the same order as their paired canonical rows.
    - Other models with no menu alias appear at the end of the canonical list.
    """
    if aliases is None:
        try:
            from kiro.config import MODEL_ALIASES as aliases  # type: ignore
        except Exception:
            aliases = {"auto-kiro": "auto"}

    alias_to_real = dict(aliases or {})
    real_to_alias = {real: alias for alias, real in alias_to_real.items()}
    id_set = set(ids)

    order: list[str] = []
    seen: set[str] = set()
    for mid in ids:
        real = alias_to_real.get(mid, mid)
        if real in seen:
            continue
        seen.add(real)
        order.append(real)

    pinned: list[str] = []
    paired_canonical: list[str] = []
    paired_aliases: list[str] = []
    unpaired: list[str] = []
    for real in order:
        alias = real_to_alias.get(real)
        alias_present = bool(alias and alias in id_set)
        real_present = real in id_set
        if not real_present and not alias_present:
            continue
        # auto: real name only, pinned first; hide auto-kiro from alias block.
        if real in _MENU_NO_ALIAS:
            pinned.append(real)
            continue
        if alias_present:
            # Show real name even when API only listed the alias.
            paired_canonical.append(real)
            paired_aliases.append(alias)  # type: ignore[arg-type]
        elif real_present:
            unpaired.append(real)
    return pinned + paired_canonical + unpaired, paired_aliases
