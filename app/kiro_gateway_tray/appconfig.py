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
    port: int = 64005
    api_region: str = "us-east-1"
    kiro_creds_file: str = ""


@dataclass
class CloudflareCfg:
    hostname: str = ""        # kg-<username>.example.com, written by provision flow
    run_token: str = ""       # per-tunnel run token, written by provision flow
    provision_url: str = ""   # Worker URL, set by user once before first activation
    registered_port: int = 0  # port sent to Worker at provision/update-port time
    protocol: str = "http2"   # quic | http2; http2 avoids UDP blocking


@dataclass
class AppCfg:
    gateway: GatewayCfg = field(default_factory=GatewayCfg)
    cloudflare: CloudflareCfg = field(default_factory=CloudflareCfg)
    gateway_extra: dict = field(default_factory=lambda: {
        "FAKE_REASONING": "false",
        "AUTO_TRIM_PAYLOAD": "true",
        "KIRO_MAX_PAYLOAD_BYTES": "600000",
        "TRUNCATION_RECOVERY": "true",
        "WEB_SEARCH_ENABLED": "false",
        "FIRST_TOKEN_TIMEOUT": "30",
        "FIRST_TOKEN_MAX_RETRIES": "3",
        "STREAMING_READ_TIMEOUT": "300",
    })


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
    raw_gateway = dict(raw.get("gateway") or {})
    extra = dict(raw.get("gateway_extra") or {})
    # Back-compat: fake_reasoning used to be a typed [gateway] field; it is now
    # a plain passthrough env var under [gateway_extra]. Migrate old configs so
    # GatewayCfg(**...) does not choke on the unexpected key.
    legacy_fake = raw_gateway.pop("fake_reasoning", None)
    if legacy_fake is not None and "FAKE_REASONING" not in extra:
        extra["FAKE_REASONING"] = "true" if legacy_fake else "false"
    return AppCfg(
        gateway=GatewayCfg(**{**asdict(GatewayCfg()), **raw_gateway}),
        cloudflare=CloudflareCfg(**{**asdict(CloudflareCfg()), **(raw.get("cloudflare") or {})}),
        gateway_extra=extra,
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
    env = {
        "PROFILE_ARN": cfg.gateway.profile_arn,
        "PROXY_API_KEY": cfg.gateway.proxy_api_key,
        "KIRO_CREDS_FILE": creds,
        "KIRO_API_REGION": cfg.gateway.api_region,
        "SERVER_HOST": "127.0.0.1",
        "SERVER_PORT": str(cfg.gateway.port),
    }
    for k, v in cfg.gateway_extra.items():
        env[k.upper()] = str(v)
    return env
