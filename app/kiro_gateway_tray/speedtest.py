# app/kiro_gateway_tray/speedtest.py
"""Speed-test side-channel: a pure-ASGI middleware wrapped around the vendored
gateway's ``main.app`` (so we never touch ``vendor/``, exactly like
``telemetry.py``).

Purpose: measure how much latency / throughput the Cloudflare edge + cloudflared
hop costs, by hitting the *same* endpoints on the local URL and on the public
tunnel URL and comparing. The round-trip is:

    client → Cloudflare edge → cloudflared → local gateway(127.0.0.1)

Endpoints (all under ``/speedtest``):
  * ``GET  /speedtest``            → the browser test page (HTML, no auth)
  * ``GET  /speedtest/ping``       → tiny JSON, for latency / TTFB
  * ``GET  /speedtest/download``   → ``?bytes=N`` stream of incompressible
                                     random data, for downlink throughput
  * ``POST /speedtest/upload``     → drains the request body, reports the byte
                                     count + server-side duration, for uplink

Everything else passes straight through to the inner app, untouched.

Security: the tunnel URL is public. An unauthenticated ``download`` would let
anyone who learns the hostname burn your bandwidth. So ``ping`` / ``download`` /
``upload`` require the gateway's ``PROXY_API_KEY`` — via ``Authorization:
Bearer <key>`` or a ``?key=<key>`` query param (handy from a browser) — and the
download size is hard-capped. The HTML page itself carries no data and is served
without auth so it loads with a plain navigation.
"""
from __future__ import annotations

import hmac
import json
import os
import time
from collections import OrderedDict
from urllib.parse import parse_qs

from .log import logger

# --- constants ---------------------------------------------------------------

_PREFIX = "/speedtest"
_CHUNK = 256 * 1024              # streamed download chunk size (bytes)
_DEFAULT_DOWNLOAD = 10 * 1024 * 1024      # 10 MiB when ?bytes= is omitted
_MAX_DOWNLOAD = 100 * 1024 * 1024         # hard cap: never stream more than this
_MAX_UPLOAD = 200 * 1024 * 1024           # hard cap on accepted upload body
_STAT_TTL = 60.0                 # keep a download's server-side stat this long
_STAT_MAX = 256                  # cap remembered stats so memory can't grow


def _enabled(env: dict[str, str] | None = None) -> bool:
    """Speed-test is on by default; set ``SPEEDTEST_ENABLED=false`` to disable."""
    e = env if env is not None else os.environ
    return str(e.get("SPEEDTEST_ENABLED", "true")).strip().lower() not in (
        "false", "0", "no", "off",
    )


def _clamp_int(raw: str | None, default: int, lo: int, hi: int) -> int:
    try:
        v = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, v))


# --- ASGI helpers ------------------------------------------------------------

async def _send_json(send, status: int, payload: dict, *, extra_headers=None) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = [
        (b"content-type", b"application/json; charset=utf-8"),
        (b"content-length", str(len(body)).encode("ascii")),
        (b"cache-control", b"no-store"),
    ]
    if extra_headers:
        headers.extend(extra_headers)
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})


def _query(scope: dict) -> dict[str, list[str]]:
    return parse_qs((scope.get("query_string") or b"").decode("latin-1"))


def _header(scope: dict, name: bytes) -> str:
    for k, v in scope.get("headers") or []:
        if k.lower() == name:
            try:
                return v.decode("latin-1")
            except Exception:
                return ""
    return ""


# --- middleware --------------------------------------------------------------

class SpeedTestMiddleware:
    """Pure-ASGI handler for ``/speedtest`` routes; transparent otherwise."""

    def __init__(self, app, api_key: str) -> None:
        self.app = app
        self.api_key = api_key or ""
        # Download is streamed, so the browser's fetch() can't read a trailing
        # server-side timing (HTTP trailers aren't exposed). We instead time the
        # push here, stash it under the client-supplied nonce, and let the page
        # fetch it back from /speedtest/download/stat. Bounded + TTL'd so it
        # can't grow without limit.
        self._dl_stats: "OrderedDict[str, tuple[float, dict]]" = OrderedDict()

    async def __call__(self, scope: dict, receive, send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        path = (scope.get("path") or "").rstrip("/") or "/"
        if path != _PREFIX and not path.startswith(_PREFIX + "/"):
            await self.app(scope, receive, send)
            return

        method = scope.get("method", "GET")
        try:
            if path == _PREFIX and method == "GET":
                await self._page(send)
            elif path == _PREFIX + "/ping":
                await self._ping(scope, send)
            elif path == _PREFIX + "/download/stat" and method == "GET":
                await self._download_stat(scope, send)
            elif path == _PREFIX + "/download" and method == "GET":
                await self._download(scope, send)
            elif path == _PREFIX + "/upload" and method == "POST":
                await self._upload(scope, receive, send)
            else:
                await _send_json(send, 404, {"error": "not found", "path": path})
        except Exception:
            # A speed-test failure must never crash the worker; report as 500.
            logger.debug("speedtest: handler failed", exc_info=True)
            await _send_json(send, 500, {"error": "speedtest handler failed"})

    # -- auth --
    def _authorized(self, scope: dict) -> bool:
        """Accept the proxy API key via Bearer header or ``?key=`` query param.

        Constant-time compare. When no key is configured (shouldn't happen for a
        provisioned gateway) we fail closed."""
        if not self.api_key:
            return False
        presented = ""
        auth = _header(scope, b"authorization")
        if auth.lower().startswith("bearer "):
            presented = auth[7:].strip()
        if not presented:
            presented = (_query(scope).get("key") or [""])[0]
        if not presented:
            return False
        return hmac.compare_digest(presented, self.api_key)

    # -- endpoints --
    async def _ping(self, scope: dict, send) -> None:
        if not self._authorized(scope):
            await _send_json(send, 401, {"error": "unauthorized"})
            return
        await _send_json(send, 200, {"pong": True, "server_time": time.time()})

    def _remember_stat(self, nonce: str, stat: dict) -> None:
        """Store a download's server-side timing under its nonce, bounded+TTL'd."""
        if not nonce:
            return
        now = time.time()
        # Drop expired entries and enforce the size cap (oldest first).
        for key in list(self._dl_stats):
            ts, _ = self._dl_stats[key]
            if now - ts > _STAT_TTL:
                self._dl_stats.pop(key, None)
        self._dl_stats[nonce] = (now, stat)
        while len(self._dl_stats) > _STAT_MAX:
            self._dl_stats.popitem(last=False)

    async def _download_stat(self, scope: dict, send) -> None:
        """Return the server-measured timing for a completed download by nonce."""
        if not self._authorized(scope):
            await _send_json(send, 401, {"error": "unauthorized"})
            return
        nonce = (_query(scope).get("nonce") or [""])[0]
        entry = self._dl_stats.get(nonce)
        if not entry:
            await _send_json(send, 404, {"error": "no stat for nonce"})
            return
        await _send_json(send, 200, entry[1])

    async def _download(self, scope: dict, send) -> None:
        if not self._authorized(scope):
            await _send_json(send, 401, {"error": "unauthorized"})
            return
        q = _query(scope)
        n = _clamp_int((q.get("bytes") or [None])[0], _DEFAULT_DOWNLOAD, 1, _MAX_DOWNLOAD)
        nonce = (q.get("nonce") or [""])[0]
        headers = [
            (b"content-type", b"application/octet-stream"),
            (b"content-length", str(n).encode("ascii")),
            # no-store + no-transform: keep Cloudflare from caching or
            # recompressing the stream, which would inflate the measured speed.
            (b"cache-control", b"no-store, no-transform"),
            (b"x-speedtest-bytes", str(n).encode("ascii")),
        ]
        await send({"type": "http.response.start", "status": 200, "headers": headers})
        start = time.perf_counter()
        remaining = n
        while remaining > 0:
            size = min(_CHUNK, remaining)
            remaining -= size
            # Fresh random per chunk: incompressible, so a proxy that gzips the
            # response can't shrink it and make the link look faster than it is.
            await send({
                "type": "http.response.body",
                "body": os.urandom(size),
                "more_body": remaining > 0,
            })
        elapsed = time.perf_counter() - start
        mbps = (n * 8 / 1_000_000 / elapsed) if elapsed > 0 else 0.0
        self._remember_stat(nonce, {
            "sent_bytes": n,
            "server_seconds": round(elapsed, 6),
            "server_mbps": round(mbps, 3),
        })

    async def _upload(self, scope: dict, receive, send) -> None:
        if not self._authorized(scope):
            await _send_json(send, 401, {"error": "unauthorized"})
            return
        start = time.perf_counter()
        received = 0
        capped = False
        while True:
            message = await receive()
            if message.get("type") == "http.request":
                received += len(message.get("body", b"") or b"")
                if received > _MAX_UPLOAD:
                    capped = True
                if not message.get("more_body", False):
                    break
            elif message.get("type") == "http.disconnect":
                break
        elapsed = time.perf_counter() - start
        mbps = (received * 8 / 1_000_000 / elapsed) if elapsed > 0 else 0.0
        await _send_json(send, 200, {
            "received_bytes": received,
            "server_seconds": round(elapsed, 6),
            "server_mbps": round(mbps, 3),
            "capped": capped,
        })

    async def _page(self, send) -> None:
        body = _HTML.encode("utf-8")
        await send({
            "type": "http.response.start",
            "status": 200,
            "headers": [
                (b"content-type", b"text/html; charset=utf-8"),
                (b"content-length", str(len(body)).encode("ascii")),
                (b"cache-control", b"no-store"),
            ],
        })
        await send({"type": "http.response.body", "body": body})


def wrap_app(app, *, env: dict[str, str] | None = None):
    """Wrap ``app`` in :class:`SpeedTestMiddleware` unless disabled.

    Reads ``PROXY_API_KEY`` from the env (the gateway child already has it).
    Returns the app unchanged when disabled, so behaviour is identical to the
    stock gateway when the feature is turned off."""
    e = env if env is not None else os.environ
    if not _enabled(e):
        return app
    api_key = (e.get("PROXY_API_KEY") or "").strip()
    logger.info("speedtest endpoints enabled at {}", _PREFIX)
    return SpeedTestMiddleware(app, api_key)


# --- browser test page -------------------------------------------------------

_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Kiro Gateway 测速</title>
<style>
  :root { color-scheme: light dark; }
  * { box-sizing: border-box; }
  body { font: 15px/1.5 -apple-system, "Segoe UI", system-ui, sans-serif;
         margin: 0; padding: 24px; background: #0b0d12; color: #e6e8ee; }
  .wrap { max-width: 720px; margin: 0 auto; }
  h1 { font-size: 20px; margin: 0 0 4px; }
  p.sub { margin: 0 0 20px; color: #98a0b3; font-size: 13px; }
  .card { background: #151922; border: 1px solid #232838; border-radius: 12px;
          padding: 18px; margin-bottom: 16px; }
  label { display: block; font-size: 12px; color: #98a0b3; margin-bottom: 6px; }
  input, select { width: 100%; padding: 9px 11px; border-radius: 8px;
                  border: 1px solid #2b3143; background: #0f131b; color: #e6e8ee;
                  font-size: 14px; }
  .row { display: flex; gap: 12px; flex-wrap: wrap; }
  .row > div { flex: 1; min-width: 140px; }
  button { margin-top: 16px; width: 100%; padding: 11px; border: 0;
           border-radius: 9px; background: #4c7dff; color: #fff; font-size: 15px;
           font-weight: 600; cursor: pointer; }
  button:disabled { opacity: .5; cursor: default; }
  .row button { flex: 1; min-width: 120px; }
  button.secondary { background: #2b3143; }
  table.results { width: 100%; border-collapse: collapse; }
  table.results th, table.results td { padding: 12px 10px; text-align: right;
            border-bottom: 1px solid #232838; }
  table.results th { color: #98a0b3; font-size: 12px; font-weight: 600; }
  table.results td:first-child, table.results th:first-child { text-align: left; }
  table.results tbody th { color: #c3c9d6; font-size: 13px; }
  table.results td { font-size: 20px; font-weight: 700; font-variant-numeric: tabular-nums; }
  table.results .unit { font-size: 11px; color: #98a0b3; font-weight: 400; margin-left: 3px; }
  table.results tr:last-child td, table.results tr:last-child th { border-bottom: 0; }
  pre { background: #0f131b; border: 1px solid #232838; border-radius: 8px;
        padding: 12px; font-size: 12px; overflow: auto; white-space: pre-wrap;
        color: #98a0b3; margin: 12px 0 0; }
  .ok { color: #45d18f; } .err { color: #ff6b6b; }
</style>
</head>
<body>
<div class="wrap">
  <h1>Kiro Gateway 测速</h1>
  <p class="sub">测量当前访问路径的延迟与吞吐。用隧道域名打开测"绕一圈"的结果，用 127.0.0.1 打开测本地基准，两者相减即 Cloudflare + cloudflared 的开销。</p>

  <div class="card">
    <div class="row">
      <div>
        <label>网关密码（proxy_api_key）</label>
        <input id="key" type="password" placeholder="Bearer key" autocomplete="off">
      </div>
      <div>
        <label>下载大小</label>
        <select id="dl">
          <option value="1048576">1 MB</option>
          <option value="2097152" selected>2 MB</option>
          <option value="5242880">5 MB</option>
          <option value="10485760">10 MB</option>
        </select>
      </div>
      <div>
        <label>上传大小</label>
        <select id="ul">
          <option value="1048576">1 MB</option>
          <option value="2097152" selected>2 MB</option>
          <option value="5242880">5 MB</option>
          <option value="10485760">10 MB</option>
        </select>
      </div>
    </div>
    <div class="row">
      <button id="go">开始测速</button>
      <button id="stop" class="secondary" disabled>停止</button>
    </div>
  </div>

  <div class="card">
    <table class="results">
      <thead>
        <tr><th>指标</th><th>客户端侧</th><th>服务端侧</th></tr>
      </thead>
      <tbody>
        <tr>
          <th>延迟 (中位数)</th>
          <td id="m-ping">–<span class="unit">ms</span></td>
          <td>–</td>
        </tr>
        <tr>
          <th>下载</th>
          <td id="m-dl-c">–<span class="unit">Mbps</span></td>
          <td id="m-dl-s">–<span class="unit">Mbps</span></td>
        </tr>
        <tr>
          <th>上传</th>
          <td id="m-ul-c">–<span class="unit">Mbps</span></td>
          <td id="m-ul-s">–<span class="unit">Mbps</span></td>
        </tr>
      </tbody>
    </table>
    <p class="sub" style="margin:12px 0 0">客户端侧＝浏览器端到端实测（含链路往返，日常以此为准）；服务端侧＝网关只计自身收/发数据的耗时。经隧道时服务端侧会因边缘缓冲偏高，仅供对照。</p>
    <pre id="log">就绪。</pre>
  </div>
</div>
<script>
const $ = (id) => document.getElementById(id);
// Set a metric cell's number while keeping its <span class="unit"> suffix.
const setVal = (id, text, unit) => {
  const el = $(id);
  const u = unit != null ? unit : (el.querySelector(".unit")?.textContent || "");
  el.innerHTML = text + (u ? `<span class="unit">${u}</span>` : "");
};
const log = (msg, cls) => {
  const el = $("log");
  const line = cls ? `<span class="${cls}">${msg}</span>` : msg;
  el.innerHTML = (el.innerHTML === "就绪。" ? "" : el.innerHTML + "\\n") + line;
};
// Replace the last log line in place (for live-updating progress).
const logReplace = (msg, cls) => {
  const el = $("log");
  const line = cls ? `<span class="${cls}">${msg}</span>` : msg;
  const parts = el.innerHTML === "就绪。" ? [] : el.innerHTML.split("\\n");
  parts[parts.length ? parts.length - 1 : 0] = line;
  el.innerHTML = parts.join("\\n");
};
const key = () => $("key").value.trim();
const authInit = (signal) => ({
  signal,
  headers: key() ? { Authorization: "Bearer " + key() } : {},
});

// Prefill the key from ?key= so opening from the menu needs no paste. We strip
// it from the visible address bar afterwards so the password isn't left there.
(function prefillKey() {
  try {
    const params = new URLSearchParams(location.search);
    const k = params.get("key");
    if (k) {
      $("key").value = k;
      history.replaceState(null, "", location.pathname);
    }
  } catch (e) {}
})();

let controller = null;   // AbortController for the in-flight run
let stopped = false;

async function ping(signal, n = 7) {
  const samples = [];
  for (let i = 0; i < n; i++) {
    if (stopped) throw new DOMException("stopped", "AbortError");
    logReplace(`测延迟… (${i + 1}/${n})`);
    const t0 = performance.now();
    const r = await fetch("./speedtest/ping?t=" + Date.now(), authInit(signal));
    if (!r.ok) throw new Error("ping HTTP " + r.status);
    await r.json();
    samples.push(performance.now() - t0);
  }
  samples.sort((a, b) => a - b);
  return samples[Math.floor(samples.length / 2)];
}

const nonce = () => (crypto.randomUUID ? crypto.randomUUID()
                     : String(Date.now()) + Math.random().toString(16).slice(2));

async function download(signal, bytes) {
  const id = nonce();
  const t0 = performance.now();
  const r = await fetch("./speedtest/download?bytes=" + bytes + "&nonce=" + id + "&t=" + Date.now(), authInit(signal));
  if (!r.ok) throw new Error("download HTTP " + r.status);
  const reader = r.body.getReader();
  let received = 0;
  let lastTick = t0;
  let lastBytes = 0;
  const total = bytes;
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    received += value.length;
    // Live speed, refreshed ~once per second.
    const now = performance.now();
    if (now - lastTick >= 1000) {
      const inst = ((received - lastBytes) * 8 / 1e6) / ((now - lastTick) / 1000);
      const pct = total ? Math.min(100, (received / total) * 100) : 0;
      setVal("m-dl-c", inst.toFixed(1));
      logReplace(`下载中… ${pct.toFixed(0)}%  当前 ${inst.toFixed(1)} Mbps`);
      lastTick = now;
      lastBytes = received;
    }
  }
  const secs = (performance.now() - t0) / 1000;
  // Fetch the server-side timing for this download (best-effort; the number
  // isn't critical, so we swallow any error).
  let server = null;
  try {
    const s = await fetch("./speedtest/download/stat?nonce=" + id, authInit(signal));
    if (s.ok) server = await s.json();
  } catch (e) {}
  return { mbps: (received * 8 / 1e6) / secs, bytes: received, secs, server };
}

async function upload(signal, bytes) {
  const payload = new Uint8Array(bytes);
  crypto.getRandomValues(payload.subarray(0, Math.min(bytes, 65536)));
  const t0 = performance.now();
  const r = await fetch("./speedtest/upload?t=" + Date.now(), {
    method: "POST",
    signal,
    headers: { ...authInit().headers, "Content-Type": "application/octet-stream" },
    body: payload,
  });
  if (!r.ok) throw new Error("upload HTTP " + r.status);
  const server = await r.json();
  const secs = (performance.now() - t0) / 1000;
  return { clientMbps: (bytes * 8 / 1e6) / secs, server };
}

function setRunning(running) {
  $("go").disabled = running;
  $("stop").disabled = !running;
  ["key", "dl", "ul"].forEach((id) => ($(id).disabled = running));
}

$("stop").addEventListener("click", () => {
  stopped = true;
  if (controller) controller.abort();
});

$("go").addEventListener("click", async () => {
  stopped = false;
  controller = new AbortController();
  const signal = controller.signal;
  setRunning(true);
  $("log").innerHTML = "";
  ["m-ping", "m-dl-c", "m-dl-s", "m-ul-c", "m-ul-s"].forEach((id) => setVal(id, "…"));
  try {
    log("测延迟…");
    const p = await ping(signal);
    setVal("m-ping", p.toFixed(1));
    logReplace(`延迟中位数 ${p.toFixed(1)} ms`, "ok");

    log("测下载…");
    const d = await download(signal, parseInt($("dl").value, 10));
    setVal("m-dl-c", d.mbps.toFixed(1));
    setVal("m-dl-s", d.server ? d.server.server_mbps.toFixed(1) : "—");
    const dlSrv = d.server ? `，服务端侧 ${d.server.server_mbps.toFixed(1)} Mbps` : "";
    logReplace(`下载 客户端侧 ${d.mbps.toFixed(1)} Mbps（${(d.bytes/1048576).toFixed(1)} MB / ${d.secs.toFixed(2)} s）${dlSrv}`, "ok");

    log("测上传…");
    const u = await upload(signal, parseInt($("ul").value, 10));
    setVal("m-ul-c", u.clientMbps.toFixed(1));
    setVal("m-ul-s", u.server ? u.server.server_mbps.toFixed(1) : "—");
    const ulSrv = u.server ? `，服务端侧 ${u.server.server_mbps.toFixed(1)} Mbps` : "";
    logReplace(`上传 客户端侧 ${u.clientMbps.toFixed(1)} Mbps${ulSrv}`, "ok");

    log("完成。", "ok");
  } catch (e) {
    if (e.name === "AbortError" || stopped) {
      log("已停止。", "err");
    } else {
      log("失败：" + e.message + "（密码填了吗？）", "err");
    }
  } finally {
    controller = null;
    setRunning(false);
  }
});

// Abort any in-flight run when leaving/refreshing, so a big download doesn't
// hold the connection and make the page unreloadable.
window.addEventListener("pagehide", () => { stopped = true; if (controller) controller.abort(); });
</script>
</body>
</html>
"""
