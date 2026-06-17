# app/kiro_gateway_tray/provision.py
"""First-run registration: call the Cloudflare Worker to provision a tunnel."""
from __future__ import annotations

import json
from pathlib import Path

import httpx

from . import appconfig
from .appconfig import AppCfg

_USERNAME_LEN = 12  # first 12 hex chars of clientIdHash (~48 bits, collision-safe)


def _read_kiro_token(cfg: AppCfg) -> dict | None:
    """Read the entire Kiro SSO token file as a dict."""
    creds_file = cfg.gateway.kiro_creds_file or appconfig.default_creds_file()
    try:
        return json.loads(Path(creds_file).read_text())
    except Exception:
        return None


def _read_client_id_hash(cfg: AppCfg) -> str | None:
    data = _read_kiro_token(cfg)
    return data.get("clientIdHash") if data else None


def read_profile_arn(cfg: AppCfg) -> str:
    """Read profileArn from the Kiro token file, or empty string."""
    data = _read_kiro_token(cfg)
    return (data.get("profileArn") or "") if data else ""


def read_api_region(cfg: AppCfg) -> str:
    """Extract API region from profileArn (e.g. us-east-1)."""
    arn = read_profile_arn(cfg)
    if arn:
        parts = arn.split(":")
        if len(parts) > 3 and parts[3]:
            return parts[3]
    return ""


def _base_url(cfg: AppCfg) -> str:
    if not cfg.cloudflare.provision_url:
        raise RuntimeError(
            "provision_url 未配置。请在 config.toml 的 [cloudflare] 段填入 Worker URL。\n"
            "示例：provision_url = \"https://kiro-gateway-provision.example.com\""
        )
    return cfg.cloudflare.provision_url.rstrip("/")


def _get_username(cfg: AppCfg) -> str:
    cid = _read_client_id_hash(cfg)
    if not cid:
        raise RuntimeError(
            "无法从 Kiro token 文件中读取 clientIdHash。\n"
            "请确认已用 Kiro IDE 登录（~/.aws/sso/cache/kiro-auth-token.json 存在）。"
        )
    return cid[:_USERNAME_LEN].lower()


def run(cfg: AppCfg, shared_secret: str) -> tuple[str, str]:
    """Call the Worker and return (hostname, run_token). Idempotent."""
    username = _get_username(cfg)
    url = _base_url(cfg) + "/provision"

    resp = httpx.post(
        url,
        json={
            "shared_secret": shared_secret,
            "username": username,
            "port": cfg.gateway.port,
        },
        timeout=30,
    )

    if resp.status_code == 401:
        raise RuntimeError("共享密钥错误，请确认你输入的激活码正确。")

    if resp.status_code not in (200, 201):
        raise RuntimeError(f"Worker 返回错误 {resp.status_code}: {resp.text[:200]}")

    data = resp.json()
    hostname = data["hostname"]
    run_token = data.get("run_token")

    if not run_token:
        raise RuntimeError(
            f"Worker 未返回 run_token（username={username}）。\n"
            "请联系管理员检查 KV 数据。"
        )

    return hostname, run_token


def update_port(cfg: AppCfg, shared_secret: str) -> bool:
    """Tell the Worker to update the tunnel ingress port. Returns True if changed."""
    username = _get_username(cfg)
    url = _base_url(cfg) + "/update-port"

    resp = httpx.post(
        url,
        json={
            "shared_secret": shared_secret,
            "username": username,
            "port": cfg.gateway.port,
        },
        timeout=30,
    )

    if resp.status_code == 401:
        raise RuntimeError("共享密钥错误。")

    if resp.status_code != 200:
        raise RuntimeError(f"update-port 失败 {resp.status_code}: {resp.text[:200]}")

    return resp.json().get("changed", False)
