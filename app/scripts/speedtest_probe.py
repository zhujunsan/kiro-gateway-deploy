# app/scripts/speedtest_probe.py
"""Command-line speed test for the Kiro Gateway ``/speedtest`` endpoints.

Hits ``ping`` / ``download`` / ``upload`` against a base origin and prints
latency + throughput. Point it at both the local origin and the tunnel origin to
see how much the Cloudflare edge + cloudflared hop costs:

    round-trip = client → Cloudflare edge → cloudflared → local gateway

Usage (auto: read config, test local vs tunnel, print a comparison):

    cd app && python -m scripts.speedtest_probe            # both, from config
    python -m scripts.speedtest_probe --local              # local origin only
    python -m scripts.speedtest_probe --tunnel             # tunnel origin only

Manual (explicit origin + key, no config needed):

    python -m scripts.speedtest_probe \\
        --url https://kg-me.example.com --key <proxy_api_key> \\
        --download-mb 25 --upload-mb 5

Notes:
  * The origin is the gateway root (NO ``/v1`` suffix); the script appends
    ``/speedtest/...`` itself. ``--url`` accepts either form and strips ``/v1``.
  * ``--key`` defaults to the ``proxy_api_key`` from the local app config.
"""
from __future__ import annotations

import argparse
import statistics
import sys
import time

import httpx


def _origin(url: str) -> str:
    """Normalise a base URL to the gateway origin (drop trailing / and /v1)."""
    u = url.strip().rstrip("/")
    if u.endswith("/v1"):
        u = u[: -len("/v1")]
    return u


def _fmt_mbps(mbps: float) -> str:
    return f"{mbps:,.1f} Mbps"


def ping(client: httpx.Client, origin: str, headers: dict, n: int = 7) -> float:
    """Return the median round-trip latency in milliseconds."""
    samples = []
    for _ in range(n):
        t0 = time.perf_counter()
        r = client.get(f"{origin}/speedtest/ping", headers=headers, params={"t": time.time()})
        r.raise_for_status()
        samples.append((time.perf_counter() - t0) * 1000)
    return statistics.median(samples)


def download(client: httpx.Client, origin: str, headers: dict, nbytes: int) -> float:
    """Stream ``nbytes`` and return throughput in Mbps."""
    t0 = time.perf_counter()
    received = 0
    with client.stream(
        "GET", f"{origin}/speedtest/download",
        headers=headers, params={"bytes": nbytes, "t": time.time()},
    ) as r:
        r.raise_for_status()
        for chunk in r.iter_bytes():
            received += len(chunk)
    secs = time.perf_counter() - t0
    return (received * 8 / 1_000_000 / secs) if secs > 0 else 0.0


def upload(client: httpx.Client, origin: str, headers: dict, nbytes: int) -> tuple[float, float]:
    """POST ``nbytes`` and return (client_mbps, server_mbps)."""
    import os
    payload = os.urandom(nbytes)
    t0 = time.perf_counter()
    r = client.post(
        f"{origin}/speedtest/upload",
        headers={**headers, "Content-Type": "application/octet-stream"},
        params={"t": time.time()},
        content=payload,
    )
    r.raise_for_status()
    secs = time.perf_counter() - t0
    client_mbps = (nbytes * 8 / 1_000_000 / secs) if secs > 0 else 0.0
    server_mbps = float(r.json().get("server_mbps", 0.0))
    return client_mbps, server_mbps


def run_one(origin: str, key: str, download_mb: int, upload_mb: int, timeout: float) -> dict:
    """Run the full probe against one origin; return the metrics."""
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    # trust_env=False: never let a corp HTTP(S)_PROXY hijack the measurement.
    with httpx.Client(timeout=timeout, trust_env=False, follow_redirects=True) as client:
        rtt = ping(client, origin, headers)
        dl = download(client, origin, headers, download_mb * 1024 * 1024)
        ul_client, ul_server = upload(client, origin, headers, upload_mb * 1024 * 1024)
    return {"rtt_ms": rtt, "dl_mbps": dl, "ul_mbps": ul_client, "ul_server_mbps": ul_server}


def _print_result(label: str, m: dict) -> None:
    print(f"  {label}")
    print(f"    延迟(中位): {m['rtt_ms']:.1f} ms")
    print(f"    下载:       {_fmt_mbps(m['dl_mbps'])}")
    print(f"    上传:       {_fmt_mbps(m['ul_mbps'])}  (服务端计 {_fmt_mbps(m['ul_server_mbps'])})")


def _print_delta(local: dict, tunnel: dict) -> None:
    print("\n绕一圈的开销 (隧道 − 本地):")
    drtt = tunnel["rtt_ms"] - local["rtt_ms"]
    print(f"    额外延迟:   +{drtt:.1f} ms")
    for lbl, key in (("下载", "dl_mbps"), ("上传", "ul_mbps")):
        lv, tv = local[key], tunnel[key]
        pct = (tv / lv * 100) if lv > 0 else 0.0
        print(f"    {lbl}保留:   {tv:,.1f} / {lv:,.1f} Mbps  ({pct:.0f}% 保留)")


def _load_config():
    """Best-effort import of the app config (only needed for auto/local/tunnel)."""
    try:
        from kiro_gateway_tray import appconfig  # type: ignore
    except Exception:
        sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))
        from kiro_gateway_tray import appconfig  # type: ignore
    return appconfig


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Kiro Gateway 测速")
    ap.add_argument("--url", help="网关 origin（本地或隧道，可带/不带 /v1）")
    ap.add_argument("--key", help="proxy_api_key；缺省从本地配置读取")
    ap.add_argument("--local", action="store_true", help="只测本地 origin（读配置）")
    ap.add_argument("--tunnel", action="store_true", help="只测隧道 origin（读配置）")
    ap.add_argument("--download-mb", type=int, default=10, help="下载大小 MB (默认 10)")
    ap.add_argument("--upload-mb", type=int, default=5, help="上传大小 MB (默认 5)")
    ap.add_argument("--timeout", type=float, default=60.0, help="单请求超时秒 (默认 60)")
    args = ap.parse_args(argv)

    dl, ul, to = args.download_mb, args.upload_mb, args.timeout

    # Explicit --url: single-origin run, no config needed.
    if args.url:
        key = args.key or ""
        if not key:
            try:
                key = _load_config().load().gateway.proxy_api_key
            except Exception:
                pass
        origin = _origin(args.url)
        print(f"测速 origin: {origin}")
        _print_result(origin, run_one(origin, key, dl, ul, to))
        return 0

    # Config-driven: resolve local + tunnel origins and the key.
    appconfig = _load_config()
    cfg = appconfig.load()
    key = args.key or cfg.gateway.proxy_api_key
    local_origin = appconfig.gateway_origin(cfg)
    tunnel_full = appconfig.tunnel_url(cfg)
    tunnel_origin = _origin(tunnel_full) if tunnel_full else ""

    want_local = args.local or not args.tunnel
    want_tunnel = args.tunnel or not args.local

    local_m = tunnel_m = None
    if want_local:
        print(f"测速 本地: {local_origin}")
        local_m = run_one(local_origin, key, dl, ul, to)
        _print_result("本地", local_m)
    if want_tunnel:
        if not tunnel_origin:
            print("隧道未配置（cloudflare.hostname 为空），跳过隧道测速。")
        else:
            print(f"\n测速 隧道: {tunnel_origin}")
            tunnel_m = run_one(tunnel_origin, key, dl, ul, to)
            _print_result("隧道", tunnel_m)

    if local_m and tunnel_m:
        _print_delta(local_m, tunnel_m)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
