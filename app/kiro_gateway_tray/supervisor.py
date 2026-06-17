# app/kiro_gateway_tray/supervisor.py
"""Orchestrate gateway + cloudflared and handle first-run provisioning."""
from __future__ import annotations

import threading
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
        self._cached_secret: str | None = None
        self.provision_callback: Callable[[AppCfg], str] | None = None
        # Cached gateway health status (never block the main/UI thread)
        self._gw_health: str = "stopped"
        self._health_thread: threading.Thread | None = None
        self._health_stop = threading.Event()

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
        self.register(cfg, shared_secret)

    def register(self, cfg: AppCfg, shared_secret: str) -> None:
        """Call the Worker with the shared secret, then persist tunnel creds.

        Shared by the CLI (via _ensure_provisioned) and the tray, which must run
        its dialogs on the main thread before the tray loop starts."""
        self._cached_secret = shared_secret
        from . import provision
        hostname, run_token = provision.run(cfg, shared_secret)
        cfg.cloudflare.hostname = hostname
        cfg.cloudflare.run_token = run_token
        cfg.cloudflare.registered_port = cfg.gateway.port
        appconfig.save(cfg)

    def _sync_port_if_changed(self, cfg: AppCfg) -> None:
        """If local port differs from the one registered with Worker, update it."""
        if not appconfig.is_provisioned(cfg):
            return
        if cfg.cloudflare.registered_port == cfg.gateway.port:
            return
        secret = self._cached_secret
        if not secret:
            return  # no cached secret, skip silently
        from . import provision
        try:
            provision.update_port(cfg, secret)
            cfg.cloudflare.registered_port = cfg.gateway.port
            appconfig.save(cfg)
        except Exception:
            pass  # non-fatal: tunnel still works on old port

    def start(self) -> bool:
        cfg = self._load()
        self._ensure_provisioned(cfg)
        self._sync_port_if_changed(cfg)
        self._gw_health = "starting"
        self.gateway.start(cfg)
        healthy = self._wait_healthy()
        if healthy:
            self._gw_health = "running"
        self.tunnel.start(cfg)
        self._start_health_loop()
        return healthy

    def stop(self) -> None:
        self._health_stop.set()
        self.tunnel.stop()
        self.gateway.stop()
        self._gw_health = "stopped"

    def restart(self) -> bool:
        self.stop()
        return self.start()

    def _start_health_loop(self) -> None:
        """Start a background thread that probes gateway /health every 3s."""
        if self._health_thread and self._health_thread.is_alive():
            return
        self._health_stop.clear()

        def _loop():
            while not self._health_stop.is_set():
                if not self.gateway.is_alive():
                    self._gw_health = "stopped"
                else:
                    cfg = self._cfg or self._load()
                    url = f"http://127.0.0.1:{cfg.gateway.port}/health"
                    try:
                        if httpx.get(url, timeout=1).status_code == 200:
                            self._gw_health = "running"
                        else:
                            self._gw_health = "starting"
                    except Exception:
                        self._gw_health = "starting"
                self._health_stop.wait(3)

        self._health_thread = threading.Thread(target=_loop, daemon=True)
        self._health_thread.start()

    def _tunnel_status(self) -> str:
        if not self.tunnel.is_alive():
            return "stopped"
        if hasattr(self.tunnel, 'is_connected') and callable(self.tunnel.is_connected):
            return "running" if self.tunnel.is_connected() else "connecting"
        return "running"

    def status(self) -> dict[str, str]:
        """Non-blocking: reads cached health state, never does I/O."""
        cfg = self._cfg or self._load()
        provisioned = appconfig.is_provisioned(cfg)
        return {
            "gateway": self._gw_health,
            "tunnel": self._tunnel_status(),
            "hostname": cfg.cloudflare.hostname if provisioned else "(未注册)",
        }
