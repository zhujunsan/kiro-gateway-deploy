# app/kiro_tray/supervisor.py
"""Orchestrate gateway + cloudflared and handle first-run provisioning."""
from __future__ import annotations

import time
from typing import Callable

import httpx

from . import appconfig
from .appconfig import AppCfg
from .gateway import GatewayThread
from .cloudflared import CloudflaredProcess


class Supervisor:
    def __init__(self, gateway=None, tunnel=None) -> None:
        self.gateway = gateway or GatewayThread()
        self.tunnel = tunnel or CloudflaredProcess()
        self._cfg: AppCfg | None = None
        # Injected by tray/cli: called when provisioning is needed.
        # Signature: (cfg: AppCfg) -> str   (returns shared_secret entered by user)
        self.provision_callback: Callable[[AppCfg], str] | None = None

    def _load(self) -> AppCfg:
        self._cfg = appconfig.load()
        return self._cfg

    def _wait_healthy(self, timeout: int = 30) -> bool:
        cfg = self._cfg or self._load()
        url = f"http://127.0.0.1:{cfg.gateway.port}/health"
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                if httpx.get(url, timeout=3).status_code == 200:
                    return True
            except httpx.HTTPError:
                pass
            time.sleep(1)
        return False

    def _ensure_provisioned(self, cfg: AppCfg) -> None:
        """Run provision flow if not yet registered. Updates cfg and saves."""
        if appconfig.is_provisioned(cfg):
            return
        if self.provision_callback is None:
            raise RuntimeError(
                "未完成首启注册，且没有注入 provision_callback。\n"
                "请先在 config.toml 填写 [cloudflare] provision_url，"
                "然后重新启动 App 完成激活。"
            )
        shared_secret = self.provision_callback(cfg)
        from . import provision
        hostname, run_token = provision.run(cfg, shared_secret)
        cfg.cloudflare.hostname = hostname
        cfg.cloudflare.run_token = run_token
        appconfig.save(cfg)

    def start(self) -> bool:
        cfg = self._load()
        self._ensure_provisioned(cfg)
        self.gateway.start(cfg)
        healthy = self._wait_healthy()
        self.tunnel.start(cfg)
        return healthy

    def stop(self) -> None:
        self.tunnel.stop()
        self.gateway.stop()

    def restart(self) -> bool:
        self.stop()
        return self.start()

    def status(self) -> dict[str, str]:
        cfg = self._cfg or self._load()
        provisioned = appconfig.is_provisioned(cfg)
        return {
            "gateway": "running" if self.gateway.is_alive() else "stopped",
            "tunnel": "running" if self.tunnel.is_alive() else "stopped",
            "hostname": cfg.cloudflare.hostname if provisioned else "(未注册)",
        }
