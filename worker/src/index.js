// worker/src/index.js
// Cloudflare Worker: provision a per-user cloudflared tunnel + usage telemetry.
// Tunnel source of truth is the Cloudflare API. Concurrent /provision for the
// same username is serialized via D1 provision_lock (see migrations/).
// Telemetry side persists to D1 (binding TELEMETRY_DB), see docs/2026-06-25-telemetry-design.md.
//
// POST /provision
//   body: { shared_secret, username, port? }
//   → 201 { hostname, run_token, telemetry_secret? }
//   Idempotent: existing tunnel is reused (ingress/DNS repaired, run_token reused).
//   Concurrent requests for the same username are serialized via D1 provision_lock;
//   the waiter gets 409 { error, retry_after } and must not delete anything.
//   telemetry_secret 仅当 TELEMETRY_SECRET 已配置时附带（首次下发）。
//
// POST /update-port
//   body: { shared_secret, username, port }
//   → 200 { ok: true, changed, port }   (port = value actually in effect)
//
// POST /tunnel-status
//   body: { shared_secret, username }
//   → 200 { exists: boolean }            ← 只读查询，绝不修改 tunnel/DNS
//   → 401 { error: "unauthorized" }
//
// POST /ensure-dns
//   body: { shared_secret, username }
//   → 200 { ok: true, hostname, repaired, api_record, authoritative }
//   → 404 { error: "tunnel not found" }  ← 隧道已不在，客户端应走完整 re-provision
//   幂等：CNAME 已正确则不动；缺失 / 未代理 / 指错隧道则删掉重建。
//   api_record = Cloudflare DNS API 中记录正确；authoritative = 公网 DoH 能解析 CNAME
//   （无法探测时为 null）。二者同时为 true 才表示公网已可解析，不只是 API 写成功。
//
// POST /reconcile-dns
//   body: { shared_secret }
//   → 200 { ok, checked, unchanged, repaired, repaired_hostnames, errors }
//   给账号下所有 kg-* 隧道补 proxied CNAME。不删隧道。
//
// POST /telemetry-secret
//   body: { shared_secret, username }    ← 用激活码鉴权（同 /provision），不用 TELEMETRY_SECRET
//   → 200 { telemetry_secret }           ← 只回当前密钥，绝不重建隧道
//   → 401 { error: "unauthorized" }
//   → 500 { error: "telemetry not configured" }  // TELEMETRY_SECRET 未配置
//
// POST /telemetry
//   headers: { Authorization: "Bearer <TELEMETRY_SECRET>" }
//   body: { schema_version, rows: [...] }
//   → 200 { ok: true, accepted: N }
//   → 401 { error: "unauthorized" }
//   Idempotent overwrite (last-write-wins) via ON CONFLICT DO UPDATE.
//
// POST /announcements
//   headers: { User-Agent: "KiroGatewayTray/<version> (<platform>)" }
//   body: { shared_secret, username }
//   → 200 { ok: true, announcements: [ { id, body, tag, url, level, priority, dimmed, ends_at } ] }
//   → 401 { error: "unauthorized" }
//   托盘顶部公告栏。版本/平台从 User-Agent 解析（body 里的 app_version/platform 仅作 curl 兜底）。
//   定向规则见 src/announcements.js，运维模板见 announcements.example.sql。
//   最多回 5 条；D1 行走边缘缓存（TTL 5 分钟），读次数与在线人数无关。
//
// GET|POST /q/*
//   只读查询端点，仅开放参数化的固定查询，默认查 usage_daily。
//   注意：/q/* 不自校验密钥 —— 由 Cloudflare Access 在边缘挡（见设计文档第十二/十三节）。
//   Worker 侧只做：白名单查询 + 仅 SELECT + 结果缓存（TTL 60 分钟）。
//
// scheduled(): cron 每小时跑（闲置隧道清理 + 补 DNS）；usage_daily 只在 UTC 0 点那一拍
//   卷已结束的自然日（当天明细仍看 usage_rollup）。幂等可重入。
//
// Required Worker Secrets (set via wrangler secret put):
//   SHARED_SECRET   — the secret distributed to users out-of-band
//   CF_API_TOKEN    — scoped: Tunnel:Edit + DNS:Edit (example.com only)
//   CF_ACCOUNT_ID
//   CF_ZONE_ID
//   DOMAIN_SUFFIX   — e.g. "example.com"
//   HOSTNAME_PREFIX — e.g. "kg"  → final hostname = kg-<username>.<DOMAIN_SUFFIX>
//   TELEMETRY_SECRET — 客户端上报 /telemetry 用的预共享密钥（独立于 SHARED_SECRET）
// Optional:
//   IDLE_CLEANUP_DAYS — 闲置隧道清理阈值（天）；未配置则不清理
// Required bindings (wrangler.toml):
//   TELEMETRY_DB    — D1 database (kiro-telemetry)

import {
  ANNOUNCEMENTS_SELECT_SQL,
  ANNOUNCEMENT_CACHE_TTL,
  ANNOUNCEMENT_CACHE_GEN,
  ANNOUNCEMENT_ROW_LIMIT,
  clientContextFromRequest,
  selectAnnouncements,
} from "./announcements.js";
import {
  ProvisionConflictError,
  StaleGenerationError,
  createD1LockStore,
  createMemoryLockStore,
  lookupAuthoritativeCname,
  provisionTunnel,
} from "./provision.js";

const CF_API = "https://api.cloudflare.com/client/v4";
const DEFAULT_PORT = 64005;

// 用户名约束，与 /provision 保持一致：小写字母数字 + 连字符，1-32 位
const USERNAME_RE = /^[a-z0-9][a-z0-9-]{0,31}$/;

// 查询结果缓存 TTL（秒）。设计文档第十二节定调 60 分钟，把 D1 读次数与
// 看板刷新次数/人数解耦，稳在 D1 Free 额度内。
const QUERY_CACHE_TTL = 3600;

async function cfFetch(env, path, method = "GET", body = null) {
  const opts = {
    method,
    headers: {
      "Authorization": `Bearer ${env.CF_API_TOKEN}`,
      "Content-Type": "application/json",
    },
  };
  if (body) opts.body = JSON.stringify(body);
  const res = await fetch(`${CF_API}${path}`, opts);
  const json = await res.json();
  if (!json.success) {
    throw new Error(`CF API ${method} ${path} failed: ${JSON.stringify(json.errors)}`);
  }
  return json.result;
}

function validatePort(port) {
  const p = parseInt(port, 10);
  return Number.isFinite(p) && p >= 1 && p <= 65535 ? p : DEFAULT_PORT;
}

function tunnelMeta(env, username) {
  const prefix = env.HOSTNAME_PREFIX || "kg";
  return {
    hostname: `${prefix}-${username}.${env.DOMAIN_SUFFIX}`,
    tunnelName: `${prefix}-${username}`,
  };
}

function cnameContent(tunnelId) {
  return `${tunnelId}.cfargotunnel.com`;
}

function tunnelIsServing(t) {
  // degraded = still on the edge and able to serve; only skip healthy would
  // let a 2/4 HA tunnel (or a reconnect blip) fall through to idle cleanup.
  return t.status === "healthy" || t.status === "degraded";
}

function idleSinceMs(t, nowMs) {
  // conns_inactive_at is the only real "went idle" signal. Falling back to
  // created_at is what deleted a live user's CNAME: a months-old tunnel that
  // briefly showed status=down during cloudflared restart looked 30+ days idle.
  if (t.conns_inactive_at) {
    const inactiveAt = new Date(t.conns_inactive_at).getTime();
    if (!Number.isFinite(inactiveAt)) return 0;
    if (t.conns_active_at) {
      const activeAt = new Date(t.conns_active_at).getTime();
      if (Number.isFinite(activeAt) && activeAt > inactiveAt) return 0;
    }
    return Math.max(0, nowMs - inactiveAt);
  }
  if (t.status === "inactive" && t.created_at) {
    const createdAt = new Date(t.created_at).getTime();
    return Number.isFinite(createdAt) ? Math.max(0, nowMs - createdAt) : 0;
  }
  return 0;
}

function shouldCleanupIdleTunnel(t, nowMs, thresholdMs, prefix) {
  if (!t || !t.name || !t.name.startsWith(prefix)) return false;
  if (tunnelIsServing(t)) return false;
  // Cloudflare: conns_inactive_at === null means the tunnel is currently active.
  if (t.conns_active_at && !t.conns_inactive_at) return false;
  return idleSinceMs(t, nowMs) >= thresholdMs;
}

function dnsRecordNeedsRepair(records, tunnelId) {
  // A proxied CNAME to this tunnel is the only valid public record. Anything
  // else (missing, extra A/AAAA, unproxied, or pointing at another tunnel)
  // makes HTTPS fail at the edge — locally that often shows up as a TLS
  // handshake EOF because Clash/Surge fake-ip still synthesizes an address.
  const expected = cnameContent(tunnelId);
  const list = Array.isArray(records) ? records : [];
  if (list.length !== 1) return true;
  const rec = list[0];
  const content = String(rec.content || "").replace(/\.$/, "").toLowerCase();
  return rec.type !== "CNAME" || content !== expected.toLowerCase() || !rec.proxied;
}

async function findTunnelByName(env, name) {
  const tunnels = await cfFetch(
    env,
    `/accounts/${env.CF_ACCOUNT_ID}/cfd_tunnel?name=${encodeURIComponent(name)}&is_deleted=false`
  );
  return tunnels.length > 0 ? tunnels[0] : null;
}

async function deleteDnsRecord(env, hostname) {
  // Search ALL record types (A, AAAA, CNAME) — Cloudflare blocks CNAME
  // creation if any of these exist for the same hostname.
  const records = await cfFetch(
    env,
    `/zones/${env.CF_ZONE_ID}/dns_records?name=${encodeURIComponent(hostname)}`
  );
  for (const r of records) {
    await cfFetch(env, `/zones/${env.CF_ZONE_ID}/dns_records/${r.id}`, "DELETE");
  }
}

async function deleteTunnel(env, tunnelId) {
  try {
    await cfFetch(
      env,
      `/accounts/${env.CF_ACCOUNT_ID}/cfd_tunnel/${tunnelId}`,
      "DELETE",
      {}
    );
  } catch {
    // tunnel may have active connections; force-delete via cleanup endpoint
    try {
      await cfFetch(
        env,
        `/accounts/${env.CF_ACCOUNT_ID}/cfd_tunnel/${tunnelId}/connections`,
        "DELETE"
      );
      await cfFetch(
        env,
        `/accounts/${env.CF_ACCOUNT_ID}/cfd_tunnel/${tunnelId}`,
        "DELETE",
        {}
      );
    } catch {
      // best-effort
    }
  }
}

async function setIngress(env, tunnelId, hostname, port) {
  await cfFetch(
    env,
    `/accounts/${env.CF_ACCOUNT_ID}/cfd_tunnel/${tunnelId}/configurations`,
    "PUT",
    {
      config: {
        ingress: [
          { hostname, service: `http://localhost:${port}` },
          { service: "http_status:404" },
        ],
      },
    }
  );
}

async function getTunnelToken(env, tunnelId) {
  const result = await cfFetch(
    env,
    `/accounts/${env.CF_ACCOUNT_ID}/cfd_tunnel/${tunnelId}/token`,
  );
  if (typeof result === "string" && result) return result;
  if (result && typeof result.token === "string" && result.token) return result.token;
  throw new Error("tunnel token missing");
}

function cfBindings(env) {
  return {
    findTunnelByName: (name) => findTunnelByName(env, name),
    createTunnel: (name) =>
      cfFetch(env, `/accounts/${env.CF_ACCOUNT_ID}/cfd_tunnel`, "POST", {
        name,
        config_src: "cloudflare",
      }),
    deleteTunnel: (id) => deleteTunnel(env, id),
    deleteDnsRecord: (hostname) => deleteDnsRecord(env, hostname),
    setIngress: (id, hostname, port) => setIngress(env, id, hostname, port),
    ensureDnsRecord: (hostname, id) => ensureDnsRecord(env, hostname, id),
    getTunnelToken: (id) => getTunnelToken(env, id),
    lookupAuthoritative: (hostname) => lookupAuthoritativeCname(hostname),
  };
}

async function provision(env, username, port) {
  const { hostname, tunnelName } = tunnelMeta(env, username);
  return provisionTunnel({
    env,
    username,
    port,
    hostname,
    tunnelName,
    cf: cfBindings(env),
    locks: createD1LockStore(env.TELEMETRY_DB),
  });
}

async function getIngressPort(env, tunnelId) {
  // Returns the localhost port currently configured for this tunnel, or null
  // if it can't be determined (no config yet, unexpected shape).
  try {
    const cfg = await cfFetch(
      env,
      `/accounts/${env.CF_ACCOUNT_ID}/cfd_tunnel/${tunnelId}/configurations`
    );
    const ingress = cfg?.config?.ingress || [];
    for (const rule of ingress) {
      const m = /^https?:\/\/localhost:(\d+)$/.exec(rule.service || "");
      if (m) return parseInt(m[1], 10);
    }
  } catch {
    // fall through
  }
  return null;
}

async function updatePort(env, username, port) {
  const { hostname, tunnelName } = tunnelMeta(env, username);
  const tunnel = await findTunnelByName(env, tunnelName);
  if (!tunnel) throw new Error(`tunnel ${tunnelName} not found`);
  const current = await getIngressPort(env, tunnel.id);
  const changed = current !== port;
  if (changed) {
    await setIngress(env, tunnel.id, hostname, port);
  }
  // Port sync is a convenient heartbeat: heal a missing CNAME without forcing
  // the client through a full re-provision (which rotates the run token).
  try {
    await ensureDnsRecord(env, hostname, tunnel.id);
  } catch (err) {
    console.log(`[ensure-dns] ${hostname}: ${err.message}`);
  }
  // Echo back the port that is actually in effect so the client can persist
  // the truth (Worker may clamp invalid ports to the default).
  return { ok: true, changed, port };
}

async function ensureDnsRecord(env, hostname, tunnelId) {
  const records = await cfFetch(
    env,
    `/zones/${env.CF_ZONE_ID}/dns_records?name=${encodeURIComponent(hostname)}`
  );
  if (!dnsRecordNeedsRepair(records, tunnelId)) {
    return { repaired: false };
  }
  await deleteDnsRecord(env, hostname);
  await cfFetch(env, `/zones/${env.CF_ZONE_ID}/dns_records`, "POST", {
    type: "CNAME",
    name: hostname,
    content: cnameContent(tunnelId),
    proxied: true,
  });
  return { repaired: true };
}

async function handleEnsureDns(env, json, username) {
  const { hostname, tunnelName } = tunnelMeta(env, username);
  const tunnel = await findTunnelByName(env, tunnelName);
  if (!tunnel) return json({ error: "tunnel not found" }, 404);
  const result = await ensureDnsRecord(env, hostname, tunnel.id);
  let authoritative = null;
  try {
    authoritative = await lookupAuthoritativeCname(hostname);
  } catch {
    authoritative = null;
  }
  return json({
    ok: true,
    hostname,
    repaired: result.repaired,
    api_record: true,
    authoritative,
  });
}

// --- telemetry ---

// 恒定时间字符串比较：先比长度，再逐字符异或累加，全程不短路，避免计时侧信道。
// 用于 /telemetry 的 Bearer 密钥校验（设计文档第八节）。
function timingSafeEqual(a, b) {
  if (typeof a !== "string" || typeof b !== "string") return false;
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) {
    diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  }
  return diff === 0;
}

// 从 Authorization: Bearer <token> 头取出 token，缺失返回 null。
function extractBearer(request) {
  const h = request.headers.get("Authorization") || "";
  const m = /^Bearer\s+(.+)$/.exec(h);
  return m ? m[1] : null;
}

// usage_rollup 一行的字段顺序（与设计文档第八节 INSERT 的列顺序一一对应）。
const ROLLUP_FIELDS = [
  "bucket_start", "bucket_seconds", "username", "model", "app_version",
  "requests", "successes", "errors",
  "prompt_tokens_sum", "completion_tokens_sum", "total_tokens_sum",
  "request_bytes_sum", "response_bytes_sum",
  "ttft_ms_sum", "ttft_count",
  "generation_ms_sum", "generation_count", "generation_completion_tokens_sum",
  "estimated_credits", "credit_estimate_segments", "credit_estimate_missing_segments",
];

const ROLLUP_INSERT_SQL = `
INSERT INTO usage_rollup (bucket_start, bucket_seconds, username, model, app_version,
                          requests, successes, errors,
                          prompt_tokens_sum, completion_tokens_sum, total_tokens_sum,
                          request_bytes_sum, response_bytes_sum,
                          ttft_ms_sum, ttft_count,
                          generation_ms_sum, generation_count, generation_completion_tokens_sum,
                          estimated_credits, credit_estimate_segments, credit_estimate_missing_segments,
                          received_at)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(bucket_start, bucket_seconds, username, model, app_version)
DO UPDATE SET
  requests = excluded.requests,
  successes = excluded.successes,
  errors = excluded.errors,
  prompt_tokens_sum = excluded.prompt_tokens_sum,
  completion_tokens_sum = excluded.completion_tokens_sum,
  total_tokens_sum = excluded.total_tokens_sum,
  request_bytes_sum = excluded.request_bytes_sum,
  response_bytes_sum = excluded.response_bytes_sum,
  ttft_ms_sum = excluded.ttft_ms_sum,
  ttft_count = excluded.ttft_count,
  generation_ms_sum = excluded.generation_ms_sum,
  generation_count = excluded.generation_count,
  generation_completion_tokens_sum = excluded.generation_completion_tokens_sum,
  estimated_credits = excluded.estimated_credits,
  credit_estimate_segments = excluded.credit_estimate_segments,
  credit_estimate_missing_segments = excluded.credit_estimate_missing_segments,
  received_at = excluded.received_at`;

function toInt(v, def = 0) {
  const n = parseInt(v, 10);
  return Number.isFinite(n) ? n : def;
}

/** Optional non-negative float: missing/invalid → null; explicit 0 stays 0. */
function toOptionalNonNegFloat(v) {
  if (v === undefined || v === null || v === "") return null;
  const n = Number(v);
  if (!Number.isFinite(n) || n < 0) return null;
  return n;
}

/** Optional non-negative int: missing/invalid → null; explicit 0 stays 0. */
function toOptionalNonNegInt(v) {
  if (v === undefined || v === null || v === "") return null;
  const n = parseInt(v, 10);
  if (!Number.isFinite(n) || n < 0) return null;
  return n;
}

// 把一行上报数据归一成 INSERT 的参数数组。无效行（缺 username/model 等）返回 null。
function normalizeRollupRow(row, receivedAt) {
  if (!row || typeof row !== "object") return null;
  const username = row.username;
  if (typeof username !== "string" || !USERNAME_RE.test(username)) return null;
  const model = typeof row.model === "string" && row.model ? row.model : "unknown";
  const appVersion = typeof row.app_version === "string" && row.app_version ? row.app_version : "unknown";

  const bucketStart = toInt(row.bucket_start, -1);
  const bucketSeconds = toInt(row.bucket_seconds, -1);
  if (bucketStart < 0 || bucketSeconds <= 0) return null;

  // Idle Credit checkpoints can open a bucket with requests == 0. Those rows
  // inflate active-user counts and waste D1 storage; reject them at ingest
  // (client also filters, but older builds may still upload).
  const requests = toInt(row.requests);
  if (requests <= 0) return null;

  return [
    bucketStart,
    bucketSeconds,
    username,
    model,
    appVersion,
    requests,
    toInt(row.successes),
    toInt(row.errors),
    toInt(row.prompt_tokens_sum),
    toInt(row.completion_tokens_sum),
    toInt(row.total_tokens_sum),
    toInt(row.request_bytes_sum),
    toInt(row.response_bytes_sum),
    // Old clients omit latency fields → 0 (averages use NULLIF count).
    toInt(row.ttft_ms_sum),
    toInt(row.ttft_count),
    toInt(row.generation_ms_sum),
    toInt(row.generation_count),
    toInt(row.generation_completion_tokens_sum),
    // Old clients omit these → NULL (unknown). Explicit 0 means measured zero.
    toOptionalNonNegFloat(row.estimated_credits),
    toOptionalNonNegInt(row.credit_estimate_segments),
    toOptionalNonNegInt(row.credit_estimate_missing_segments),
    receivedAt,
  ];
}

async function handleTelemetry(request, env, json) {
  // 恒定时间比较校验预共享密钥；缺失或不匹配一律 401。
  const token = extractBearer(request);
  if (!env.TELEMETRY_SECRET || token == null || !timingSafeEqual(token, env.TELEMETRY_SECRET)) {
    return json({ error: "unauthorized" }, 401);
  }

  let body;
  try {
    body = await request.json();
  } catch {
    return json({ error: "invalid JSON" }, 400);
  }

  // 协议版本握手位：读出 body 顶层 schema_version 备用（当前无分支按版本分流，
  // 不再写入每行 rollup）。保留以便未来协议演进时按版本路由。
  const schemaVersion = toInt(body && body.schema_version, 1);
  void schemaVersion;
  const rows = body && Array.isArray(body.rows) ? body.rows : null;
  if (!rows) {
    return json({ error: "rows must be an array" }, 400);
  }

  const receivedAt = Math.floor(Date.now() / 1000);
  const statements = [];
  for (const row of rows) {
    const params = normalizeRollupRow(row, receivedAt);
    if (params) {
      statements.push(env.TELEMETRY_DB.prepare(ROLLUP_INSERT_SQL).bind(...params));
    }
  }

  if (statements.length === 0) {
    return json({ ok: true, accepted: 0 });
  }

  // 一次 batch 提交：D1 在单次 batch 内串行执行、整体作为一个事务。
  await env.TELEMETRY_DB.batch(statements);
  return json({ ok: true, accepted: statements.length });
}

// 刷新端点：客户端在 /telemetry 收到 401（本地密钥过期）后，用激活码 shared_secret
// 换取最新 TELEMETRY_SECRET（设计文档第八节"密钥分发与轮换"）。
// 用 shared_secret 鉴权（恒定时间比较），只读 env 返回密钥，绝不创建/删除/修改任何
// tunnel 或 DNS —— 与 /provision 的隧道重建逻辑彻底分离。
async function handleTelemetrySecret(request, env, json) {
  let body;
  try {
    body = await request.json();
  } catch {
    return json({ error: "invalid JSON" }, 400);
  }

  const { shared_secret, username } = body || {};
  if (!shared_secret || !env.SHARED_SECRET || !timingSafeEqual(shared_secret, env.SHARED_SECRET)) {
    return json({ error: "unauthorized" }, 401);
  }
  if (!username || !USERNAME_RE.test(username)) {
    return json({ error: "username must be lowercase alphanumeric/hyphen, 1-32 chars" }, 400);
  }
  if (!env.TELEMETRY_SECRET) {
    return json({ error: "telemetry not configured" }, 500);
  }
  return json({ telemetry_secret: env.TELEMETRY_SECRET });
}

// --- 只读查询（/q/*） ---
//
// 安全红线（设计文档第十二节）：
//   - 只开放下面写死的参数化固定查询，绝不透传任意 SQL。
//   - 代码层只允许 SELECT（所有模板都是 SELECT，且不接受外部 SQL）。
//   - 默认查 usage_daily，降低单次扫描行数与 D1 读额度。
// 注意：/q/* 自身不校验密钥，由 Cloudflare Access 在边缘挡住未授权请求。

function clampDays(v, def = 30, max = 365) {
  const n = toInt(v, def);
  if (n < 1) return 1;
  if (n > max) return max;
  return n;
}

// 固定查询表：name → (env, params) => { sql, binds }。全部为 SELECT，参数化绑定。
const QUERIES = {
  // 近 N 天，按 user × day 聚合（默认看板主查询）。
  "daily-by-user": (env, p) => {
    const days = clampDays(p.get("days"));
    const user = p.get("username");
    const binds = [`-${days} days`];
    let where = "day >= date('now', ?)";
    if (user && USERNAME_RE.test(user)) {
      where += " AND username = ?";
      binds.push(user);
    }
    return {
      sql: `SELECT day, username,
                   SUM(requests) AS requests,
                   SUM(successes) AS successes,
                   SUM(errors) AS errors,
                   SUM(prompt_tokens_sum) AS prompt_tokens,
                   SUM(completion_tokens_sum) AS completion_tokens,
                   SUM(total_tokens_sum) AS total_tokens,
                   SUM(request_bytes_sum) AS request_bytes,
                   SUM(response_bytes_sum) AS response_bytes
            FROM usage_daily
            WHERE ${where}
            GROUP BY day, username
            ORDER BY day, username`,
      binds,
    };
  },

  // 近 N 天，模型分布（按 model 聚合）。
  "model-distribution": (env, p) => {
    const days = clampDays(p.get("days"));
    return {
      sql: `SELECT model,
                   SUM(requests) AS requests,
                   SUM(total_tokens_sum) AS total_tokens
            FROM usage_daily
            WHERE day >= date('now', ?)
            GROUP BY model
            ORDER BY requests DESC`,
      binds: [`-${days} days`],
    };
  },

  // 近 N 天，每天的活跃（去重）用户数。
  "active-users": (env, p) => {
    const days = clampDays(p.get("days"));
    return {
      sql: `SELECT day, COUNT(DISTINCT username) AS active_users
            FROM usage_daily
            WHERE day >= date('now', ?)
            GROUP BY day
            ORDER BY day`,
      binds: [`-${days} days`],
    };
  },

  // 近 N 天，每个用户的总量汇总（按 token 倒序）。
  "user-totals": (env, p) => {
    const days = clampDays(p.get("days"));
    return {
      sql: `SELECT username,
                   SUM(requests) AS requests,
                   SUM(successes) AS successes,
                   SUM(errors) AS errors,
                   SUM(total_tokens_sum) AS total_tokens
            FROM usage_daily
            WHERE day >= date('now', ?)
            GROUP BY username
            ORDER BY total_tokens DESC`,
      binds: [`-${days} days`],
    };
  },
};

async function handleQuery(request, env, url, json) {
  // path: /q/<name>
  const name = url.pathname.slice("/q/".length);
  const builder = QUERIES[name];
  if (!builder) {
    return json({ error: "unknown query", available: Object.keys(QUERIES) }, 404);
  }

  // GET 用 query string，POST 接受 JSON body（统一转成 URLSearchParams 风格读取）。
  let params = url.searchParams;
  if (request.method === "POST") {
    try {
      const b = await request.json();
      params = new URLSearchParams();
      for (const [k, v] of Object.entries(b || {})) {
        if (v != null) params.set(k, String(v));
      }
    } catch {
      return json({ error: "invalid JSON" }, 400);
    }
  }

  // 结果缓存：用规范化后的 URL 作为 cache key，每个固定查询每 TTL 周期只真打 D1 一次。
  const cache = caches.default;
  const cacheKey = new Request(
    `https://q.cache/${name}?${params.toString()}`,
    { method: "GET" }
  );
  const cached = await cache.match(cacheKey);
  if (cached) return cached;

  const { sql, binds } = builder(env, params);
  // 代码层兜底：模板必须是 SELECT，杜绝任何写操作走到 D1。
  if (!/^\s*SELECT\b/i.test(sql)) {
    return json({ error: "only SELECT queries are allowed" }, 500);
  }

  const result = await env.TELEMETRY_DB.prepare(sql).bind(...binds).all();
  const resp = json({
    ok: true,
    query: name,
    results: result.results || [],
    rows_read: result.meta && result.meta.rows_read,
  });
  resp.headers.set("Cache-Control", `public, max-age=${QUERY_CACHE_TTL}`);
  // 异步写缓存，不阻塞响应。
  await cache.put(cacheKey, resp.clone());
  return resp;
}

// --- cron 卷动：usage_rollup → usage_daily ---
//
// 只卷已经结束的 UTC 自然日：昨天（刚闭合）+ 前天（补迟到上报）。
// 当天桶留在 usage_rollup，看板/观测若要看「正在进行的今天」直接查明细表。
// 天 × username × model 聚合 SUM，写入 usage_daily。
// 幂等可重入：用 INSERT ... ON CONFLICT(PK) DO UPDATE 覆盖，重复跑同一天结果一致。
//
// WHERE 必须是 bucket_start 的半开区间，不能包 date()：后者会让 idx_rollup_bucket
// 失效、全表扫描（见 D1 Query Insights）。
const DAILY_ROLLUP_SQL = `
INSERT INTO usage_daily (day, username, model,
                         requests, successes, errors,
                         prompt_tokens_sum, completion_tokens_sum, total_tokens_sum,
                         request_bytes_sum, response_bytes_sum,
                         ttft_ms_sum, ttft_count,
                         generation_ms_sum, generation_count, generation_completion_tokens_sum,
                         estimated_credits, credit_estimate_segments, credit_estimate_missing_segments)
SELECT date(bucket_start, 'unixepoch') AS day,
       username, model,
       SUM(requests), SUM(successes), SUM(errors),
       SUM(prompt_tokens_sum), SUM(completion_tokens_sum), SUM(total_tokens_sum),
       SUM(request_bytes_sum), SUM(response_bytes_sum),
       SUM(ttft_ms_sum), SUM(ttft_count),
       SUM(generation_ms_sum), SUM(generation_count), SUM(generation_completion_tokens_sum),
       SUM(estimated_credits), SUM(credit_estimate_segments), SUM(credit_estimate_missing_segments)
FROM usage_rollup
WHERE bucket_start >= ? AND bucket_start < ?
GROUP BY day, username, model
ON CONFLICT(day, username, model)
DO UPDATE SET
  requests = excluded.requests,
  successes = excluded.successes,
  errors = excluded.errors,
  prompt_tokens_sum = excluded.prompt_tokens_sum,
  completion_tokens_sum = excluded.completion_tokens_sum,
  total_tokens_sum = excluded.total_tokens_sum,
  request_bytes_sum = excluded.request_bytes_sum,
  response_bytes_sum = excluded.response_bytes_sum,
  ttft_ms_sum = excluded.ttft_ms_sum,
  ttft_count = excluded.ttft_count,
  generation_ms_sum = excluded.generation_ms_sum,
  generation_count = excluded.generation_count,
  generation_completion_tokens_sum = excluded.generation_completion_tokens_sum,
  estimated_credits = excluded.estimated_credits,
  credit_estimate_segments = excluded.credit_estimate_segments,
  credit_estimate_missing_segments = excluded.credit_estimate_missing_segments`;

/**
 * UTC calendar-day window as Unix-second half-open range [start, end).
 *
 * Cron must filter `usage_rollup` by raw `bucket_start` so SQLite can use
 * `idx_rollup_bucket`. Wrapping the column in `date(bucket_start, 'unixepoch')`
 * forces a full table scan (~24k rows today, growing without bound); a range
 * predicate only reads that day's few hundred buckets.
 *
 * Args:
 *   now: Instant whose UTC date is "today".
 *   daysAgo: 0 = that UTC day, 1 = the previous UTC day, etc.
 *
 * Returns:
 *   `{ start, end }` Unix seconds. `end` is exclusive.
 */
function utcDayWindow(now, daysAgo) {
  const utcMs = Date.UTC(
    now.getUTCFullYear(),
    now.getUTCMonth(),
    now.getUTCDate() - daysAgo,
  );
  const start = Math.floor(utcMs / 1000);
  return { start, end: start + 86400 };
}

// 昨天（刚闭合的完整 UTC 日）+ 前天（给迟到桶 24h 窗口）。不卷当天。
const DAILY_ROLLUP_DAYS_AGO = [1, 2];

/**
 * Daily rollup is gated to the 00:07 UTC cron tick.
 *
 * The Worker cron stays hourly for idle-tunnel cleanup / DNS repair.
 * usage_daily only needs one write after the UTC day closes; the in-progress
 * day is read from usage_rollup.
 *
 * Args:
 *   now: Instant of this cron invocation (typically `event.scheduledTime`).
 *
 * Returns:
 *   true iff this tick should run the usage_daily upsert.
 */
function shouldRollupDaily(now = new Date()) {
  return now.getUTCHours() === 0;
}

async function rollupToDaily(env, now = new Date()) {
  const statements = DAILY_ROLLUP_DAYS_AGO.map((daysAgo) => {
    const { start, end } = utcDayWindow(now, daysAgo);
    return env.TELEMETRY_DB.prepare(DAILY_ROLLUP_SQL).bind(start, end);
  });
  await env.TELEMETRY_DB.batch(statements);
}

// --- 闲置隧道定期清理 ---
//
// 仅在 IDLE_CLEANUP_DAYS 已配置且为正整数时执行；未配置则完全跳过（安全默认）。
// 只清理 name 以 HOSTNAME_PREFIX- 开头的隧道（本项目签发的），跳过正在活跃的。
// 单个隧道删除失败不影响其余；console.log 记录操作用于审计。

async function listProjectTunnels(env) {
  const prefix = (env.HOSTNAME_PREFIX || "kg") + "-";
  const out = [];
  let page = 1;
  while (true) {
    const tunnels = await cfFetch(
      env,
      `/accounts/${env.CF_ACCOUNT_ID}/cfd_tunnel?is_deleted=false&per_page=100&page=${page}`
    );
    if (!Array.isArray(tunnels) || tunnels.length === 0) break;
    for (const t of tunnels) {
      if (t.name && t.name.startsWith(prefix)) out.push(t);
    }
    if (tunnels.length < 100) break;
    page++;
  }
  return out;
}

async function cleanupIdleTunnels(env) {
  const maxDays = parseInt(env.IDLE_CLEANUP_DAYS, 10);
  if (!Number.isFinite(maxDays) || maxDays <= 0) return;

  const prefix = (env.HOSTNAME_PREFIX || "kg") + "-";
  const nowMs = Date.now();
  const thresholdMs = maxDays * 86400000;

  const tunnels = await listProjectTunnels(env);
  for (const t of tunnels) {
    if (!shouldCleanupIdleTunnel(t, nowMs, thresholdMs, prefix)) continue;

    const hostname = `${t.name}.${env.DOMAIN_SUFFIX}`;
    const idleDays = Math.floor(idleSinceMs(t, nowMs) / 86400000);
    try {
      // Tunnel first: if delete fails (reconnect raced in), leave DNS alone.
      await deleteTunnel(env, t.id);
      await deleteDnsRecord(env, hostname);
      console.log(`[cleanup] deleted idle tunnel "${t.name}" (idle ${idleDays}d)`);
    } catch (err) {
      console.log(`[cleanup] failed to delete "${t.name}": ${err.message}`);
    }
  }
}

async function listZoneCnameRecords(env) {
  const byName = new Map();
  let page = 1;
  while (true) {
    const records = await cfFetch(
      env,
      `/zones/${env.CF_ZONE_ID}/dns_records?type=CNAME&per_page=100&page=${page}`
    );
    if (!Array.isArray(records) || records.length === 0) break;
    for (const rec of records) {
      if (rec && rec.name) byName.set(String(rec.name).toLowerCase(), rec);
    }
    if (records.length < 100) break;
    page++;
  }
  return byName;
}

async function reconcileAllTunnelDns(env) {
  // One Worker invocation has a low subrequest cap (~50 on this account).
  // Scan with bulk lists, then repair a bounded number per call.
  const repairBudget = 12;
  const tunnels = await listProjectTunnels(env);
  const cnames = await listZoneCnameRecords(env);
  const report = { checked: 0, unchanged: 0, repaired: [], errors: [], pending: 0 };
  for (const t of tunnels) {
    report.checked += 1;
    const hostname = `${t.name}.${env.DOMAIN_SUFFIX}`;
    const existing = cnames.get(hostname.toLowerCase());
    const snapshot = existing ? [existing] : [];
    if (!dnsRecordNeedsRepair(snapshot, t.id)) {
      report.unchanged += 1;
      continue;
    }
    if (report.repaired.length + report.errors.length >= repairBudget) {
      report.pending += 1;
      continue;
    }
    try {
      const result = await ensureDnsRecord(env, hostname, t.id);
      if (result.repaired) report.repaired.push(hostname);
      else report.unchanged += 1;
    } catch (err) {
      report.errors.push({ hostname, error: String(err.message || err) });
      console.log(`[reconcile-dns] ${hostname}: ${err.message}`);
    }
  }
  return report;
}

async function handleReconcileDns(env, json) {
  const report = await reconcileAllTunnelDns(env);
  return json({
    ok: true,
    checked: report.checked,
    unchanged: report.unchanged,
    repaired: report.repaired.length,
    pending: report.pending,
    repaired_hostnames: report.repaired,
    errors: report.errors,
  });
}

// --- 隧道存在性只读查询 ---
//
// POST /tunnel-status: 供客户端确认隧道是否仍存在于云端。
// 严格只读——绝不创建/删除/修改任何 tunnel 或 DNS。
// 鉴权：body 中的 shared_secret，恒定时间比较。

async function handleTunnelStatus(request, env, json) {
  let body;
  try {
    body = await request.json();
  } catch {
    return json({ error: "invalid JSON" }, 400);
  }

  const { shared_secret, username } = body || {};
  if (!shared_secret || !env.SHARED_SECRET || !timingSafeEqual(shared_secret, env.SHARED_SECRET)) {
    return json({ error: "unauthorized" }, 401);
  }
  if (!username || !USERNAME_RE.test(username)) {
    return json({ error: "username must be lowercase alphanumeric/hyphen, 1-32 chars" }, 400);
  }

  const { tunnelName } = tunnelMeta(env, username);
  const tunnel = await findTunnelByName(env, tunnelName);
  return json({ exists: !!tunnel });
}

// --- 公告栏（/announcements） ---
//
// 鉴权与 /tunnel-status 一致：body 里的激活码，恒定时间比较。公告内容因此不对
// 公网裸奔，客户端也不用再存一份新密钥。
//
// D1 读取走边缘缓存：每 ANNOUNCEMENT_CACHE_TTL 秒才真查一次（enabled 且未过期的行），
// 之后版本/平台/用户名/灰度过滤都在内存里做。缓存内容与请求者无关，命中率接近 100%。
// 查询时已丢掉 ends_at 已过的历史行；缓存窗口内刚好到期的，仍由 JS 侧 ends_at 再挡一层。

async function loadAnnouncementRows(env) {
  // Cache key embeds a generation so demo/content flips don't wait out TTL.
  // Bump ANNOUNCEMENT_CACHE_GEN when a force-refresh of D1 rows is needed.
  const cache = caches.default;
  const cacheKey = new Request(
    `https://announcements.cache/rows-g${ANNOUNCEMENT_CACHE_GEN}`,
    { method: "GET" },
  );

  const cached = await cache.match(cacheKey);
  if (cached) {
    try {
      return await cached.json();
    } catch {
      // 缓存体损坏：退回真查一次，别让公告永久卡死在坏缓存上。
    }
  }

  let rows;
  try {
    const now = Math.floor(Date.now() / 1000);
    const result = await env.TELEMETRY_DB
      .prepare(ANNOUNCEMENTS_SELECT_SQL)
      .bind(now, ANNOUNCEMENT_ROW_LIMIT)
      .all();
    rows = result.results || [];
  } catch (err) {
    // 公告是非关键功能：D1 异常（最典型的是 Worker 已部署但迁移还没跑）时
    // 降级成"没有公告"，绝不把客户端的每小时轮询变成 500 风暴。不缓存失败结果，
    // 这样迁移一跑完下一次请求就能恢复。
    console.log(`[announcements] query failed: ${err.message}`);
    return [];
  }

  const resp = new Response(JSON.stringify(rows), {
    headers: {
      "Content-Type": "application/json",
      "Cache-Control": `public, max-age=${ANNOUNCEMENT_CACHE_TTL}`,
    },
  });
  await cache.put(cacheKey, resp.clone());
  return rows;
}

async function handleAnnouncements(request, env, json) {
  let body;
  try {
    body = await request.json();
  } catch {
    return json({ error: "invalid JSON" }, 400);
  }

  const { shared_secret, username } = body || {};
  if (!shared_secret || !env.SHARED_SECRET || !timingSafeEqual(shared_secret, env.SHARED_SECRET)) {
    return json({ error: "unauthorized" }, 401);
  }
  if (!username || !USERNAME_RE.test(username)) {
    return json({ error: "username must be lowercase alphanumeric/hyphen, 1-32 chars" }, 400);
  }

  const rows = await loadAnnouncementRows(env);
  const announcements = selectAnnouncements(
    rows,
    clientContextFromRequest({ headers: request.headers, body }),
  );
  return json({ ok: true, announcements });
}

// --- request handler ---

export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    const json = (data, status = 200) =>
      new Response(JSON.stringify(data), { status, headers: { "Content-Type": "application/json" } });

    // 遥测上报：自校验 TELEMETRY_SECRET（Bearer），写 usage_rollup。
    if (url.pathname === "/telemetry") {
      if (request.method !== "POST") {
        return new Response("not found", { status: 404 });
      }
      try {
        return await handleTelemetry(request, env, json);
      } catch (err) {
        return json({ error: err.message }, 500);
      }
    }

    // 遥测密钥刷新：用激活码 shared_secret 鉴权，只读 env 返回密钥，不碰隧道。
    if (url.pathname === "/telemetry-secret") {
      if (request.method !== "POST") {
        return new Response("not found", { status: 404 });
      }
      try {
        return await handleTelemetrySecret(request, env, json);
      } catch (err) {
        return json({ error: err.message }, 500);
      }
    }

    // 隧道存在性查询：只读，供客户端判断云端 tunnel 是否已被删除。
    if (url.pathname === "/tunnel-status") {
      if (request.method !== "POST") {
        return new Response("not found", { status: 404 });
      }
      try {
        return await handleTunnelStatus(request, env, json);
      } catch (err) {
        return json({ error: err.message }, 500);
      }
    }

    // 公告栏：用激活码鉴权，只读 announcements 表，绝不写库、不碰隧道。
    if (url.pathname === "/announcements") {
      if (request.method !== "POST") {
        return new Response("not found", { status: 404 });
      }
      try {
        return await handleAnnouncements(request, env, json);
      } catch (err) {
        return json({ error: err.message }, 500);
      }
    }

    // 只读查询：不自校验密钥（Cloudflare Access 在边缘挡），只读 usage_daily。
    if (url.pathname.startsWith("/q/")) {
      if (request.method !== "GET" && request.method !== "POST") {
        return new Response("not found", { status: 404 });
      }
      try {
        return await handleQuery(request, env, url, json);
      } catch (err) {
        return json({ error: err.message }, 500);
      }
    }

    // --- 以下是现有 provision 路由（shared_secret 在 body 内校验） ---

    if (request.method !== "POST") {
      return new Response("not found", { status: 404 });
    }

    let body;
    try {
      body = await request.json();
    } catch {
      return new Response(JSON.stringify({ error: "invalid JSON" }), { status: 400 });
    }

    const { shared_secret, username } = body || {};

    if (!shared_secret || shared_secret !== env.SHARED_SECRET) {
      return new Response(JSON.stringify({ error: "unauthorized" }), { status: 401 });
    }

    if (url.pathname === "/reconcile-dns") {
      try {
        return await handleReconcileDns(env, json);
      } catch (err) {
        return json({ error: err.message }, 500);
      }
    }

    if (!username || !USERNAME_RE.test(username)) {
      return new Response(
        JSON.stringify({ error: "username must be lowercase alphanumeric/hyphen, 1-32 chars" }),
        { status: 400 }
      );
    }

    if (url.pathname === "/provision") {
      try {
        const port = validatePort(body.port);
        const result = await provision(env, username, port);
        return json(result, 201);
      } catch (err) {
        if (err instanceof ProvisionConflictError || err.status === 409) {
          return json(
            { error: err.message, retry_after: err.retryAfter },
            409,
          );
        }
        return json({ error: err.message }, 500);
      }
    }

    if (url.pathname === "/update-port") {
      if (body.port == null) {
        return json({ error: "port is required" }, 400);
      }
      try {
        const port = validatePort(body.port);
        const result = await updatePort(env, username, port);
        return json(result);
      } catch (err) {
        return json({ error: err.message }, 500);
      }
    }

    if (url.pathname === "/ensure-dns") {
      try {
        return await handleEnsureDns(env, json, username);
      } catch (err) {
        return json({ error: err.message }, 500);
      }
    }

    return new Response("not found", { status: 404 });
  },

  // cron 每小时：清理闲置隧道 + 补 DNS。usage_daily 只在 UTC 0 点那一拍卷已结束的天。
  async scheduled(event, env, ctx) {
    const now = new Date(event.scheduledTime);
    if (shouldRollupDaily(now)) {
      ctx.waitUntil(rollupToDaily(env, now));
    }
    ctx.waitUntil((async () => {
      await cleanupIdleTunnels(env);
      await reconcileAllTunnelDns(env);
    })());
  },
};

// 供单元测试直接引用真实实现，避免测试里再抄一份逻辑然后悄悄跟源码走偏。
// Workers 运行时只消费 default export，额外的具名导出没有任何副作用。
export {
  normalizeRollupRow,
  utcDayWindow,
  shouldRollupDaily,
  DAILY_ROLLUP_DAYS_AGO,
  timingSafeEqual,
  cnameContent,
  dnsRecordNeedsRepair,
  tunnelIsServing,
  idleSinceMs,
  shouldCleanupIdleTunnel,
  provisionTunnel,
  createMemoryLockStore,
  createD1LockStore,
  ProvisionConflictError,
  StaleGenerationError,
};
