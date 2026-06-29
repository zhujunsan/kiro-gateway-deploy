# app/kiro_gateway_tray/provision.py
"""First-run registration: call the Cloudflare Worker to provision a tunnel."""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

import httpx

from . import appconfig
from .appconfig import AppCfg

_USERNAME_LEN = 12  # chars used from the per-user profile ID
_HTTP_RETRIES = 3   # attempts for transient network errors
_HTTP_TIMEOUT = 30


def _post_with_retry(url: str, payload: dict) -> httpx.Response:
    """POST with bounded retries on transient network/5xx errors.

    Auth failures (401) and other 4xx are returned immediately so callers can
    surface a precise message instead of retrying a doomed request."""
    last_err: Exception | None = None
    for attempt in range(1, _HTTP_RETRIES + 1):
        try:
            resp = httpx.post(url, json=payload, timeout=_HTTP_TIMEOUT)
        except httpx.HTTPError as e:
            last_err = e
        else:
            # Retry only on server-side transient failures.
            if resp.status_code < 500:
                return resp
            last_err = RuntimeError(f"Worker {resp.status_code}: {resp.text[:200]}")
        if attempt < _HTTP_RETRIES:
            time.sleep(2 * attempt)
    raise RuntimeError(f"请求 {url} 失败（重试 {_HTTP_RETRIES} 次）：{last_err}")


def _read_kiro_token(cfg: AppCfg) -> dict | None:
    """Read the entire Kiro SSO token file as a dict."""
    creds_file = cfg.gateway.kiro_creds_file or appconfig.default_creds_file()
    try:
        return json.loads(Path(creds_file).read_text())
    except Exception:
        return None


def _read_client_id_hash(data: dict | None) -> str | None:
    return data.get("clientIdHash") if data else None


def _read_per_user_client_id(cfg: AppCfg, data: dict | None) -> str | None:
    """Read the per-user clientId from the AWS SSO cache file.

    kiro-auth-token.json stores clientIdHash, which is the SHA1-derived filename
    of the SSO OIDC cache entry (~/.aws/sso/cache/<clientIdHash>.json).
    That file contains clientId, which is unique per user (unlike clientIdHash
    which is the same for everyone in the same organisation).

    ``data`` is the already-parsed kiro-auth-token.json (passed in so a single
    provision doesn't re-read it for each lookup).
    """
    if not data:
        return None
    client_id_hash = data.get("clientIdHash")
    if not client_id_hash:
        return None
    creds_file = cfg.gateway.kiro_creds_file or appconfig.default_creds_file()
    sso_cache_dir = Path(creds_file).parent
    cache_file = sso_cache_dir / f"{client_id_hash}.json"
    try:
        sso_data = json.loads(cache_file.read_text())
        return sso_data.get("clientId") or None
    except Exception:
        return None


def read_profile_arn(cfg: AppCfg) -> str:
    """Return profileArn: user-entered config value first, then the token file.

    The Kiro Gateway only writes profileArn back into kiro-auth-token.json after
    it has run successfully, so on first run it is usually absent. The user fills
    it in during setup, after which the config value is authoritative."""
    if cfg.gateway.profile_arn:
        return cfg.gateway.profile_arn
    data = _read_kiro_token(cfg)
    return (data.get("profileArn") or "") if data else ""


def region_from_arn(arn: str) -> str:
    """Extract the API region (e.g. us-east-1) from a profileArn string."""
    if arn:
        parts = arn.split(":")
        if len(parts) > 3 and parts[3]:
            return parts[3]
    return ""


def read_api_region(cfg: AppCfg) -> str:
    """Extract API region from profileArn (e.g. us-east-1)."""
    return region_from_arn(read_profile_arn(cfg))


def _base_url(cfg: AppCfg) -> str:
    if not cfg.cloudflare.provision_url:
        raise RuntimeError(
            "provision_url 未配置。请在 config.toml 的 [cloudflare] 段填入 Worker URL。\n"
            "示例：provision_url = \"https://kiro-gateway-provision.example.com\""
        )
    return cfg.cloudflare.provision_url.rstrip("/")


def _get_username(cfg: AppCfg) -> str:
    """Return a per-user unique slug for tunnel naming.

    Uses the per-user clientId from ~/.aws/sso/cache/<clientIdHash>.json.
    clientId is unique per Kiro/CodeWhisperer user; clientIdHash (its filename)
    looks per-user but is actually shared across the whole organisation, and
    profileArn last segment is also a company-wide profile — both would map
    every user to the same tunnel name, causing mutual re-provisioning conflicts.

    clientId may contain base64 characters, so we SHA-1-hash it and use the
    first _USERNAME_LEN hex digits as a stable, URL-safe slug.
    """
    data = _read_kiro_token(cfg)  # read once, reused by both lookups below
    client_id = _read_per_user_client_id(cfg, data)
    if client_id:
        return hashlib.sha1(client_id.encode()).hexdigest()[:_USERNAME_LEN]

    # Fallback: clientIdHash (org-shared — only used when the SSO cache file is
    # missing, e.g. non-standard Kiro installs)
    cid = _read_client_id_hash(data)
    if not cid:
        raise RuntimeError(
            "无法从 Kiro token 文件中读取用户唯一标识（clientIdHash）。\n"
            "请确认已用 Kiro IDE 登录（~/.aws/sso/cache/kiro-auth-token.json 存在）。"
        )
    return cid[:_USERNAME_LEN].lower()


def run(cfg: AppCfg, shared_secret: str) -> tuple[str, str, str]:
    """Call the Worker and return (hostname, run_token, telemetry_secret). Idempotent.

    ``telemetry_secret`` is the first-dispatch of the usage-telemetry pre-shared
    key (design §8 "密钥分发与轮换"). It is only present when the Worker has
    TELEMETRY_SECRET configured; older Workers omit it, in which case "" is
    returned and the caller leaves the existing config value untouched."""
    username = _get_username(cfg)
    url = _base_url(cfg) + "/provision"

    resp = _post_with_retry(
        url,
        {
            "shared_secret": shared_secret,
            "username": username,
            "port": cfg.gateway.port,
        },
    )

    if resp.status_code == 401:
        raise RuntimeError("共享密钥错误，请确认你输入的激活码正确。")

    if resp.status_code not in (200, 201):
        raise RuntimeError(f"Worker 返回错误 {resp.status_code}: {resp.text[:200]}")

    data = resp.json()
    hostname = data["hostname"]
    run_token = data.get("run_token")
    telemetry_secret = data.get("telemetry_secret") or ""

    if not run_token:
        raise RuntimeError(
            f"Worker 未返回 run_token（username={username}）。\n"
            "请联系管理员检查 KV 数据。"
        )

    return hostname, run_token, telemetry_secret


def refresh_telemetry_secret(provision_url: str, shared_secret: str, username: str) -> str:
    """Fetch the current telemetry secret from POST /telemetry-secret.

    Called by the gateway child process after /telemetry returns 401 (the local
    secret rotated server-side). Authenticated with the activation code
    ``shared_secret`` — NOT the telemetry secret — because the whole point is
    that the telemetry secret is already stale (design §8). The Worker only
    echoes the current secret; it never rebuilds the tunnel.

    ``provision_url`` is the same base used by /provision (the telemetry refresh
    endpoint is same-origin). Returns the new secret, or "" on any failure
    (auth error, network, not-configured) so the caller can decide to keep
    spooling rather than crash.
    """
    if not provision_url or not shared_secret or not username:
        return ""
    url = provision_url.rstrip("/") + "/telemetry-secret"
    try:
        resp = httpx.post(
            url,
            json={"shared_secret": shared_secret, "username": username},
            timeout=_HTTP_TIMEOUT,
            trust_env=False,
        )
    except httpx.HTTPError:
        return ""
    if resp.status_code != 200:
        return ""
    try:
        return (resp.json().get("telemetry_secret") or "")
    except Exception:
        return ""


def tunnel_exists(cfg: AppCfg, shared_secret: str) -> bool | None:
    """Check whether the tunnel still exists on the cloud side.

    Returns True (exists), False (definitively deleted), or None (unable to
    determine — network error, auth failure, etc.). The caller should only
    re-provision on an explicit False; None means "unknown, stay conservative".
    """
    if not cfg.cloudflare.provision_url or not shared_secret:
        return None
    try:
        username = _get_username(cfg)
    except Exception:
        return None
    url = cfg.cloudflare.provision_url.rstrip("/") + "/tunnel-status"
    try:
        resp = httpx.post(
            url,
            json={"shared_secret": shared_secret, "username": username},
            timeout=_HTTP_TIMEOUT,
            trust_env=False,
        )
    except httpx.HTTPError:
        return None
    if resp.status_code != 200:
        return None
    try:
        return bool(resp.json().get("exists"))
    except Exception:
        return None


def update_port(cfg: AppCfg, shared_secret: str) -> int:
    """Tell the Worker to update the tunnel ingress port.

    Returns the port the Worker reports as actually in effect (it may clamp an
    invalid value to its default), so the caller can persist the truth rather
    than the value it asked for."""
    username = _get_username(cfg)
    url = _base_url(cfg) + "/update-port"

    resp = _post_with_retry(
        url,
        {
            "shared_secret": shared_secret,
            "username": username,
            "port": cfg.gateway.port,
        },
    )

    if resp.status_code == 401:
        raise RuntimeError("共享密钥错误。")

    if resp.status_code != 200:
        raise RuntimeError(f"update-port 失败 {resp.status_code}: {resp.text[:200]}")

    # Older Workers don't echo the port back; fall back to the requested value.
    return int(resp.json().get("port", cfg.gateway.port))
