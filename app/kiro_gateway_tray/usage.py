# app/kiro_gateway_tray/usage.py
"""Query the gateway's own GET /usage endpoint on localhost."""
from __future__ import annotations

import atexit

import httpx

from . import appconfig
from .httpclient import local_client
from .login_state import LoginState, parse_login_required

# Reused connection pool for localhost gateway calls (usage + models). Avoids
# building a fresh client/connection on every menu refresh. Released at process
# exit so the pool doesn't outlive us during interpreter shutdown. See
# httpclient.local_client for the trust_env=False rationale (avoid a corp proxy
# hijacking 127.0.0.1 probes).
_client = local_client(timeout=30.0)
atexit.register(_client.close)


class LoginRequiredError(RuntimeError):
    """Kiro credentials are expired or absent; only the user can fix this.

    Raised instead of a generic RuntimeError so callers can stop polling and
    prompt for re-login rather than retrying a state that cannot recover on its
    own (see :mod:`login_state`).
    """

    def __init__(self, state: LoginState) -> None:
        """
        Args:
            state: Parsed credential state from the gateway response.
        """
        super().__init__(state.message or "Kiro 登录已过期，请重新登录。")
        self.state = state


def _authed_get(path: str, timeout: float) -> httpx.Response:
    """GET a localhost gateway endpoint with the proxy API key.

    Args:
        path: Gateway path, e.g. ``/usage``.
        timeout: Per-request timeout in seconds.

    Returns:
        The 200 response.

    Raises:
        LoginRequiredError: Gateway reported that Kiro needs a re-login.
        RuntimeError: Any other non-200, including status code and body prefix
            for diagnosis.
    """
    cfg = appconfig.load()
    url = f"{appconfig.gateway_origin(cfg)}{path}"
    headers = {"Authorization": f"Bearer {cfg.gateway.proxy_api_key}"}
    resp = _client.get(url, headers=headers, timeout=timeout)
    if resp.status_code != 200:
        state = _login_state_from_response(resp)
        if state.login_required:
            raise LoginRequiredError(state)
        raise RuntimeError(f"{path} returned {resp.status_code}: {resp.text[:200]}")
    return resp


def _login_state_from_response(resp: httpx.Response) -> LoginState:
    """Parse credential state from a non-200 body, tolerating non-JSON."""
    try:
        payload = resp.json()
    except ValueError:
        return LoginState()
    return parse_login_required(payload)


def fetch(timeout: float = 30.0) -> dict:
    """Return the gateway's parsed /usage payload.

    Raises:
        LoginRequiredError: Kiro credentials need renewing.
        RuntimeError: Other gateway failures.
    """
    return _authed_get("/usage", timeout).json()


def fetch_health(timeout: float = 5.0) -> LoginState:
    """Probe GET /health and return the credential state it reports.

    /health is unauthenticated and never touches the Kiro upstream, so this is
    the cheap way to learn whether the user is signed out — including when the
    gateway started in degraded mode and ``/usage`` would only say "no account".

    Args:
        timeout: Per-request timeout in seconds.

    Returns:
        Parsed LoginState; a signed-in state when /health is unreachable or
        gives an unexpected body (never guess a login prompt from a failure).
    """
    cfg = appconfig.load()
    url = f"{appconfig.gateway_origin(cfg)}/health"
    try:
        resp = _client.get(url, timeout=timeout)
        return parse_login_required(resp.json())
    except (httpx.HTTPError, ValueError):
        return LoginState()


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


# Native models pinned before alias-backed and other canonical model rows.
_MENU_PINNED_MODELS = frozenset({"auto"})


def split_models_for_menu(
    ids: list[str],
    aliases: dict[str, str] | None = None,
) -> tuple[list[str], list[str]]:
    """Split model IDs into (canonical, aliases) with matching order.

    The gateway list mixes real IDs (``claude-haiku-4.5``) with Cursor-safe
    aliases (for example, ``kiro-h-4.5``). The tray menu shows two blocks;
    paired rows must stay 1:1 so the N-th alias lines up with the N-th
    canonical model above the separator.

    - Canonical entries use the real model ID.
    - Native ``auto`` has no alias and is pinned first in the canonical block.
    - Alias entries follow the same order as their paired canonical rows.
    - Other models with no menu alias appear at the end of the canonical list.

    When ``aliases`` is omitted, the static ``MODEL_ALIASES`` table is loaded
    and then augmented with ``generate_model_alias`` for every canonical ID in
    ``ids`` so newly discovered models (e.g. ``claude-opus-5`` → ``kiro-o-5``)
    pair correctly even before the static table is republished.
    """
    if aliases is None:
        try:
            from kiro.config import MODEL_ALIASES as aliases  # type: ignore
        except (ImportError, AttributeError):
            aliases = {}

    alias_to_real = dict(aliases or {})
    # Augment with auto-generated aliases for canonical IDs present in the list.
    try:
        from kiro.model_aliases import generate_model_alias  # type: ignore
    except ImportError:
        generate_model_alias = None  # type: ignore[assignment]

    if generate_model_alias is not None:
        for mid in ids:
            # Skip names that are already known aliases.
            if mid in alias_to_real:
                continue
            generated = generate_model_alias(mid)
            if generated:
                alias_to_real.setdefault(generated, mid)

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
        if alias is None and generate_model_alias is not None:
            alias = generate_model_alias(real)
        alias_present = bool(alias and alias in id_set)
        real_present = real in id_set
        if not real_present and not alias_present:
            continue
        # Native models use their real IDs and stay at the front of the menu.
        if real in _MENU_PINNED_MODELS:
            pinned.append(real)
            continue
        if alias_present:
            # Show real name even when API only listed the alias.
            paired_canonical.append(real)
            paired_aliases.append(alias)  # type: ignore[arg-type]
        elif real_present:
            unpaired.append(real)
    return pinned + paired_canonical + unpaired, paired_aliases
