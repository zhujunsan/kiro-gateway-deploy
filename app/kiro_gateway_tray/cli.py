# app/kiro_gateway_tray/cli.py
"""Headless fallback when no tray is available (typically Ubuntu/GNOME)."""
from __future__ import annotations

import signal
import sys
import threading

from . import appconfig, paths, usage
from .supervisor import Supervisor


def _base_url(cfg) -> str:
    if cfg.cloudflare.hostname:
        return f"https://{cfg.cloudflare.hostname}/v1"
    return f"http://127.0.0.1:{cfg.gateway.port}/v1"


def _first_run_setup_cli(cfg) -> str:
    """CLI guided setup: prompt for provision_url (if empty) + shared secret."""
    print("\n=== Kiro Tray 首次配置 ===")
    if not cfg.cloudflare.provision_url:
        print("请输入 Worker 服务地址（provision URL）：", end="", flush=True)
        url = input().strip()
        cfg.cloudflare.provision_url = url
        appconfig.save(cfg)
        print(f"  已保存: {url}")

    print(f"\nWorker: {cfg.cloudflare.provision_url}")
    print("请输入激活码（共享密钥）：", end="", flush=True)
    secret = input().strip()
    if not secret:
        raise RuntimeError("激活码为空，已取消。")
    return secret


def run() -> int:
    cfg = appconfig.load()
    sup = Supervisor()
    sup.provision_callback = _first_run_setup_cli

    print("Kiro Gateway (CLI 模式)")
    print(f"  配置文件: {paths.config_file()}")
    print(f"  日志目录: {paths.log_dir()}")
    print("  启动中...")

    try:
        sup.start()
    except Exception as e:
        print(f"  启动失败: {e}", file=sys.stderr)
        return 1

    cfg = appconfig.load()  # reload after potential provision
    print(f"  Base URL: {_base_url(cfg)}")
    print("  按 Ctrl-C 退出；输入 u + 回车查额度。")

    stop = threading.Event()

    def _sig(_signum, _frame):
        stop.set()

    signal.signal(signal.SIGINT, _sig)
    signal.signal(signal.SIGTERM, _sig)

    def _input_loop():
        for line in sys.stdin:
            if line.strip().lower() == "u":
                try:
                    print(usage.format_summary(usage.fetch()))
                except Exception as e:
                    print(f"查额度失败: {e}")
            if stop.is_set():
                break

    threading.Thread(target=_input_loop, daemon=True).start()
    stop.wait()
    print("\n  停止中...")
    sup.stop()
    return 0
