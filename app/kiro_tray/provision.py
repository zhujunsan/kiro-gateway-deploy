# app/kiro_tray/provision.py
"""First-run registration: call the Cloudflare Worker to provision a tunnel."""
from __future__ import annotations

import json
import re
from pathlib import Path

import httpx

from . import appconfig
from .appconfig import AppCfg


def _read_kiro_email(cfg: AppCfg) -> str | None:
    """Extract email from Kiro SSO token file."""
    creds_file = cfg.gateway.kiro_creds_file or appconfig.default_creds_file()
    try:
        data = json.loads(Path(creds_file).read_text())
        return data.get("email") or data.get("Email")
    except Exception:
        return None


def _email_to_username(email: str) -> str:
    """Convert email prefix to a valid tunnel username."""
    prefix = email.split("@")[0].lower()
    # Replace non-alphanumeric chars with hyphens, collapse multiple hyphens
    username = re.sub(r"[^a-z0-9]+", "-", prefix).strip("-")
    return username[:32]  # max 32 chars


def run(cfg: AppCfg, shared_secret: str) -> tuple[str, str]:
    """Call the Worker and return (hostname, run_token).

    Raises RuntimeError on failure or if already provisioned (run_token lost).
    """
    if not cfg.cloudflare.provision_url:
        raise RuntimeError(
            "provision_url 未配置。请在 config.toml 的 [cloudflare] 段填入 Worker URL。\n"
            "示例：provision_url = \"https://kiro-gateway-provision.botsonny.top\""
        )

    email = _read_kiro_email(cfg)
    if not email:
        raise RuntimeError(
            "无法从 Kiro token 文件中读取 email。\n"
            "请确认已用 Kiro IDE 登录（~/.aws/sso/cache/kiro-auth-token.json 存在）。"
        )

    username = _email_to_username(email)
    url = cfg.cloudflare.provision_url.rstrip("/") + "/provision"

    resp = httpx.post(
        url,
        json={"shared_secret": shared_secret, "username": username},
        timeout=30,
    )

    if resp.status_code == 401:
        raise RuntimeError("共享密钥错误，请确认你输入的激活码正确。")

    if resp.status_code not in (200, 201):
        raise RuntimeError(f"Worker 返回错误 {resp.status_code}: {resp.text[:200]}")

    data = resp.json()
    hostname = data["hostname"]
    run_token = data.get("run_token")

    if resp.status_code == 200 and not run_token:
        raise RuntimeError(
            f"此 username ({username}) 已注册，子域名为 {hostname}，\n"
            "但 run_token 已无法再次获取。请联系管理员重新签发。"
        )

    return hostname, run_token
