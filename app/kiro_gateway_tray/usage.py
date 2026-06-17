# app/kiro_gateway_tray/usage.py
"""Query the gateway's own GET /usage endpoint on localhost."""
from __future__ import annotations

import httpx

from . import appconfig

# Reused connection pool for localhost gateway calls (usage + models). Avoids
# building a fresh client/connection on every menu refresh.
_client = httpx.Client(timeout=30.0)


def fetch(timeout: float = 30.0) -> dict:
    cfg = appconfig.load()
    url = f"http://127.0.0.1:{cfg.gateway.port}/usage"
    headers = {"Authorization": f"Bearer {cfg.gateway.proxy_api_key}"}
    resp = _client.get(url, headers=headers, timeout=timeout)
    if resp.status_code != 200:
        raise RuntimeError(f"/usage returned {resp.status_code}: {resp.text[:200]}")
    return resp.json()


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
    cfg = appconfig.load()
    url = f"http://127.0.0.1:{cfg.gateway.port}/v1/models"
    headers = {"Authorization": f"Bearer {cfg.gateway.proxy_api_key}"}
    resp = _client.get(url, headers=headers, timeout=timeout)
    if resp.status_code != 200:
        raise RuntimeError(f"/v1/models returned {resp.status_code}")
    data = resp.json().get("data") or []
    return sorted(m["id"] for m in data if "id" in m)
