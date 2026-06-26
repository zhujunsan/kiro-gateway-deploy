// worker/src/index.js
// Cloudflare Worker: provision a per-user cloudflared tunnel + usage telemetry.
// Provision side is stateless — no KV storage. Cloudflare API is the source of truth.
// Telemetry side persists to D1 (binding TELEMETRY_DB), see docs/2026-06-25-telemetry-design.md.
//
// POST /provision
//   body: { shared_secret, username, port? }
//   → 201 { hostname, run_token, telemetry_secret? }
//   Idempotent: if tunnel already exists, deletes and recreates it.
//   telemetry_secret 仅当 TELEMETRY_SECRET 已配置时附带（首次下发）。
//
// POST /update-port
//   body: { shared_secret, username, port }
//   → 200 { ok: true, changed, port }   (port = value actually in effect)
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
// GET|POST /q/*
//   只读查询端点，仅开放参数化的固定查询，默认查 usage_daily。
//   注意：/q/* 不自校验密钥 —— 由 Cloudflare Access 在边缘挡（见设计文档第十二/十三节）。
//   Worker 侧只做：白名单查询 + 仅 SELECT + 结果缓存（TTL 60 分钟）。
//
// scheduled(): cron 触发，把 usage_rollup 卷成 usage_daily 日聚合（幂等可重入）。
//
// Required Worker Secrets (set via wrangler secret put):
//   SHARED_SECRET   — the secret distributed to users out-of-band
//   CF_API_TOKEN    — scoped: Tunnel:Edit + DNS:Edit (example.com only)
//   CF_ACCOUNT_ID
//   CF_ZONE_ID
//   DOMAIN_SUFFIX   — e.g. "example.com"
//   HOSTNAME_PREFIX — e.g. "kg"  → final hostname = kg-<username>.<DOMAIN_SUFFIX>
//   TELEMETRY_SECRET — 客户端上报 /telemetry 用的预共享密钥（独立于 SHARED_SECRET）
// Required bindings (wrangler.toml):
//   TELEMETRY_DB    — D1 database (kiro-telemetry)

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

async function provision(env, username, port) {
  const { hostname, tunnelName } = tunnelMeta(env, username);

  // Clean up any existing tunnel + DNS (idempotent recreate)
  const existing = await findTunnelByName(env, tunnelName);
  if (existing) {
    await deleteDnsRecord(env, hostname);
    await deleteTunnel(env, existing.id);
  }

  // Create fresh tunnel
  const tunnel = await cfFetch(
    env,
    `/accounts/${env.CF_ACCOUNT_ID}/cfd_tunnel`,
    "POST",
    { name: tunnelName, config_src: "cloudflare" }
  );

  await setIngress(env, tunnel.id, hostname, port);

  const cnameBody = {
    type: "CNAME",
    name: hostname,
    content: `${tunnel.id}.cfargotunnel.com`,
    proxied: true,
  };
  try {
    await cfFetch(env, `/zones/${env.CF_ZONE_ID}/dns_records`, "POST", cnameBody);
  } catch {
    // Stale record may still exist; force-clean and retry once.
    await deleteDnsRecord(env, hostname);
    await cfFetch(env, `/zones/${env.CF_ZONE_ID}/dns_records`, "POST", cnameBody);
  }

  const result = { hostname, run_token: tunnel.token };
  // 首次下发遥测密钥：provision 成功时附带当前 TELEMETRY_SECRET（设计文档第八节
  // "密钥分发与轮换"）。未配置该 secret 时省略该字段、不报错。
  if (env.TELEMETRY_SECRET) {
    result.telemetry_secret = env.TELEMETRY_SECRET;
  }
  return result;
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
  // Echo back the port that is actually in effect so the client can persist
  // the truth (Worker may clamp invalid ports to the default).
  return { ok: true, changed, port };
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
];

const ROLLUP_INSERT_SQL = `
INSERT INTO usage_rollup (bucket_start, bucket_seconds, username, model, app_version,
                          requests, successes, errors,
                          prompt_tokens_sum, completion_tokens_sum, total_tokens_sum,
                          request_bytes_sum, response_bytes_sum,
                          received_at)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
  received_at = excluded.received_at`;

function toInt(v, def = 0) {
  const n = parseInt(v, 10);
  return Number.isFinite(n) ? n : def;
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

  return [
    bucketStart,
    bucketSeconds,
    username,
    model,
    appVersion,
    toInt(row.requests),
    toInt(row.successes),
    toInt(row.errors),
    toInt(row.prompt_tokens_sum),
    toInt(row.completion_tokens_sum),
    toInt(row.total_tokens_sum),
    toInt(row.request_bytes_sum),
    toInt(row.response_bytes_sum),
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
// 把指定天（默认前一天 + 当天，覆盖跨小时/跨天的边界）的 rollup 桶按
// 天 × username × model 聚合 SUM，写入 usage_daily。
// 幂等可重入：用 INSERT ... ON CONFLICT(PK) DO UPDATE 覆盖，重复跑同一天结果一致。
const DAILY_ROLLUP_SQL = `
INSERT INTO usage_daily (day, username, model,
                         requests, successes, errors,
                         prompt_tokens_sum, completion_tokens_sum, total_tokens_sum,
                         request_bytes_sum, response_bytes_sum)
SELECT date(bucket_start, 'unixepoch') AS day,
       username, model,
       SUM(requests), SUM(successes), SUM(errors),
       SUM(prompt_tokens_sum), SUM(completion_tokens_sum), SUM(total_tokens_sum),
       SUM(request_bytes_sum), SUM(response_bytes_sum)
FROM usage_rollup
WHERE date(bucket_start, 'unixepoch') = ?
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
  response_bytes_sum = excluded.response_bytes_sum`;

async function rollupToDaily(env) {
  // 覆盖当天与前一天：cron 在 UTC 边界附近触发时，前一天可能还有迟到的桶进来。
  const now = new Date();
  const days = [];
  for (let i = 0; i <= 1; i++) {
    const d = new Date(now.getTime() - i * 86400000);
    days.push(d.toISOString().slice(0, 10)); // YYYY-MM-DD (UTC)
  }
  const statements = days.map((day) =>
    env.TELEMETRY_DB.prepare(DAILY_ROLLUP_SQL).bind(day)
  );
  await env.TELEMETRY_DB.batch(statements);
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

    return new Response("not found", { status: 404 });
  },

  // cron 触发：把 usage_rollup 卷成 usage_daily（幂等可重入）。
  async scheduled(event, env, ctx) {
    ctx.waitUntil(rollupToDaily(env));
  },
};
