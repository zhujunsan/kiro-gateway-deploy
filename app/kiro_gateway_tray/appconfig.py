"""Load/save the user-edited TOML config and map it to gateway env vars."""
from __future__ import annotations

import os
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
    shared_secret: str = ""   # activation code; persisted so update-port works
                              # across restarts (config is chmod 0600 on POSIX)
    metrics_port: int = 20241  # cloudflared metrics server; probed at /ready to
                               # detect tunnel connectivity without parsing logs


@dataclass
class TelemetryCfg:
    # Usage telemetry (see docs/2026-06-25-telemetry-design.md). There is no
    # on/off switch by design; an empty endpoint_url simply means "not
    # configured" so the gateway stays dormant instead of failing.
    endpoint_url: str = ""    # telemetry worker URL, e.g. .../telemetry
    secret: str = ""          # pre-shared bearer secret (Authorization header)
    bucket_seconds: int = 600           # 10-minute aggregation window
    flush_interval: int = 600           # timer wake cadence (aligned to bucket)
    max_retention_days: int = 30        # local pending.jsonl retention


@dataclass
class AppCfg:
    gateway: GatewayCfg = field(default_factory=GatewayCfg)
    cloudflare: CloudflareCfg = field(default_factory=CloudflareCfg)
    telemetry: TelemetryCfg = field(default_factory=TelemetryCfg)
    gateway_extra: dict = field(default_factory=lambda: {
        "FAKE_REASONING": "false",
        # 关闭按字节自动裁剪：Kiro 的上下文上限是按 token 算的（~200k），
        # 字节阈值无法可靠对齐。超限时让 gateway 回 400 context_length_exceeded，
        # 由客户端（如 Cursor）自行压缩上下文重试。
        "AUTO_TRIM_PAYLOAD": "false",
        "TRUNCATION_RECOVERY": "true",
        "WEB_SEARCH_ENABLED": "false",
        "FIRST_TOKEN_TIMEOUT": "30",
        "FIRST_TOKEN_MAX_RETRIES": "3",
        "STREAMING_READ_TIMEOUT": "300",
        # 默认开启详细日志 + 失败请求抓包，便于排查 Cursor 报错（如
        # "Invalid tool use format"）。DEBUG_MODE=errors 只在请求失败时把
        # 请求体/响应落盘，正常请求零额外开销；落盘目录由 gateway.py 在运行时
        # 指到 log 目录下的 debug_logs/（DEBUG_DIR）。
        "LOG_LEVEL": "DEBUG",
        "DEBUG_MODE": "errors",
    })


def path():
    return paths.config_file()


_CACHE: AppCfg | None = None


def load(*, use_cache: bool = False) -> AppCfg:
    """Load config from disk. With use_cache=True, return a process-wide cached
    instance (populated on first load, invalidated by save()). The cache is for
    read-hot paths like tray menu rendering, which would otherwise re-read and
    re-parse the TOML on every redraw."""
    global _CACHE
    if use_cache and _CACHE is not None:
        return _CACHE
    cfg = _load_from_disk()
    if use_cache:
        _CACHE = cfg
    return cfg


def _load_from_disk() -> AppCfg:
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
    # Backfill debug-capture defaults for configs written before these keys
    # existed, so updating the app turns them on without a fresh install. This
    # runs on every load: removing a key won't stick (it gets re-added), which
    # is intentional — these are managed defaults, not user-tunable here.
    extra.setdefault("LOG_LEVEL", "DEBUG")
    extra.setdefault("DEBUG_MODE", "errors")
    return AppCfg(
        gateway=GatewayCfg(**{**asdict(GatewayCfg()), **raw_gateway}),
        cloudflare=CloudflareCfg(**{**asdict(CloudflareCfg()), **(raw.get("cloudflare") or {})}),
        telemetry=TelemetryCfg(**{**asdict(TelemetryCfg()), **(raw.get("telemetry") or {})}),
        gateway_extra=extra,
    )


def save(cfg: AppCfg) -> None:
    global _CACHE
    paths.ensure_dirs()
    p = path()
    p.write_text(tomli_w.dumps(asdict(cfg)), encoding="utf-8")
    # Config holds secrets (proxy_api_key, run_token); restrict to owner on POSIX.
    if os.name == "posix":
        try:
            p.chmod(0o600)
        except OSError:
            pass
    _CACHE = cfg


def invalidate_cache() -> None:
    global _CACHE
    _CACHE = None


def gateway_origin(cfg: AppCfg) -> str:
    """Base origin of the locally-running gateway (no path), e.g.
    ``http://127.0.0.1:64005``. Single source for the localhost host:port so
    health/usage/models probes don't each hardcode it."""
    return f"http://127.0.0.1:{cfg.gateway.port}"


def local_url(cfg: AppCfg) -> str:
    return f"{gateway_origin(cfg)}/v1"


def tunnel_url(cfg: AppCfg) -> str:
    if cfg.cloudflare.hostname:
        return f"https://{cfg.cloudflare.hostname}/v1"
    return ""


def base_url(cfg: AppCfg) -> str:
    """Prefer the public tunnel URL, fall back to the local one."""
    return tunnel_url(cfg) or local_url(cfg)


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
    _inject_telemetry_env(cfg, env)
    return env


def _inject_telemetry_env(cfg: AppCfg, env: dict[str, str]) -> None:
    """Add telemetry env vars consumed by telemetry.from_env() in the child.

    The report URL is derived from the provision Worker (telemetry uses the same
    Worker/domain — scheme A): when ``[telemetry].endpoint_url`` is empty we fall
    back to ``cloudflare.provision_url`` + ``/telemetry``. An explicit
    endpoint_url still wins (escape hatch for testing/overrides). Telemetry stays
    dormant only when neither is set (gateway.py won't wrap the app).

    The anonymous username comes from provision._get_username(), which can raise
    when the Kiro token file is missing — that must never block gateway startup,
    so we degrade to "unknown" on any failure."""
    tel = cfg.telemetry
    provision_url = cfg.cloudflare.provision_url
    endpoint_url = tel.endpoint_url or (
        provision_url.rstrip("/") + "/telemetry" if provision_url else ""
    )
    if not endpoint_url:
        return
    from . import __version__
    env["TELEMETRY_URL"] = endpoint_url
    env["TELEMETRY_SECRET"] = tel.secret
    env["TELEMETRY_BUCKET_SECONDS"] = str(tel.bucket_seconds)
    env["TELEMETRY_FLUSH_INTERVAL"] = str(tel.flush_interval)
    env["TELEMETRY_MAX_RETENTION_DAYS"] = str(tel.max_retention_days)
    env["APP_VERSION"] = __version__
    # Inputs for on-401 secret refresh (design §8): the refresh endpoint is
    # same-origin as /provision and authed with the activation code, both of
    # which are persisted in [cloudflare]. Absent either, the child simply
    # can't refresh and keeps spooling — no crash.
    if provision_url:
        env["TELEMETRY_PROVISION_URL"] = provision_url
    if cfg.cloudflare.shared_secret:
        env["TELEMETRY_SHARED_SECRET"] = cfg.cloudflare.shared_secret
    try:
        from . import provision
        env["TELEMETRY_USERNAME"] = provision._get_username(cfg)
    except Exception:
        env["TELEMETRY_USERNAME"] = "unknown"
