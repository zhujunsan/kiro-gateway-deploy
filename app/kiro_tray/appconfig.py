"""Load/save the user-edited TOML config and map it to gateway env vars."""
from __future__ import annotations

import tomllib
from dataclasses import dataclass, asdict, field

import tomli_w

from . import paths


@dataclass
class GatewayCfg:
    profile_arn: str = ""
    proxy_api_key: str = "change-me"
    port: int = 18000
    api_region: str = "us-east-1"
    kiro_creds_file: str = ""
    fake_reasoning: bool = False


@dataclass
class CloudflareCfg:
    hostname: str = ""        # kg-<username>.botsonny.top, written by provision flow
    run_token: str = ""       # per-tunnel run token, written by provision flow
    provision_url: str = ""   # Worker URL, set by user once before first activation


@dataclass
class AppCfg:
    gateway: GatewayCfg = field(default_factory=GatewayCfg)
    cloudflare: CloudflareCfg = field(default_factory=CloudflareCfg)


def path():
    return paths.config_file()


def load() -> AppCfg:
    paths.ensure_dirs()
    p = path()
    if not p.exists():
        cfg = AppCfg()
        save(cfg)
        return cfg
    raw = tomllib.loads(p.read_text(encoding="utf-8"))
    return AppCfg(
        gateway=GatewayCfg(**{**asdict(GatewayCfg()), **(raw.get("gateway") or {})}),
        cloudflare=CloudflareCfg(**{**asdict(CloudflareCfg()), **(raw.get("cloudflare") or {})}),
    )


def save(cfg: AppCfg) -> None:
    paths.ensure_dirs()
    path().write_text(tomli_w.dumps(asdict(cfg)), encoding="utf-8")


def is_provisioned(cfg: AppCfg) -> bool:
    return bool(cfg.cloudflare.hostname and cfg.cloudflare.run_token)


def default_creds_file() -> str:
    from pathlib import Path
    return str(Path.home() / ".aws" / "sso" / "cache" / "kiro-auth-token.json")


def to_gateway_env(cfg: AppCfg) -> dict[str, str]:
    """Translate config into env vars the vendored gateway reads at import."""
    creds = cfg.gateway.kiro_creds_file or default_creds_file()
    return {
        "PROFILE_ARN": cfg.gateway.profile_arn,
        "PROXY_API_KEY": cfg.gateway.proxy_api_key,
        "KIRO_CREDS_FILE": creds,
        "KIRO_API_REGION": cfg.gateway.api_region,
        "SERVER_HOST": "127.0.0.1",
        "SERVER_PORT": str(cfg.gateway.port),
        "FAKE_REASONING": "true" if cfg.gateway.fake_reasoning else "false",
    }
