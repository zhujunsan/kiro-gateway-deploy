// worker/src/index.js
// Cloudflare Worker: provision a per-user cloudflared tunnel.
// Stateless — no KV storage. Cloudflare API is the source of truth.
//
// POST /provision
//   body: { shared_secret, username, port? }
//   → 201 { hostname, run_token }
//   Idempotent: if tunnel already exists, deletes and recreates it.
//
// POST /update-port
//   body: { shared_secret, username, port }
//   → 200 { ok: true, changed, port }   (port = value actually in effect)
//
// Required Worker Secrets (set via wrangler secret put):
//   SHARED_SECRET   — the secret distributed to users out-of-band
//   CF_API_TOKEN    — scoped: Tunnel:Edit + DNS:Edit (example.com only)
//   CF_ACCOUNT_ID
//   CF_ZONE_ID
//   DOMAIN_SUFFIX   — e.g. "example.com"
//   HOSTNAME_PREFIX — e.g. "kg"  → final hostname = kg-<username>.<DOMAIN_SUFFIX>

const CF_API = "https://api.cloudflare.com/client/v4";
const DEFAULT_PORT = 64005;

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

  return { hostname, run_token: tunnel.token };
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

// --- request handler ---

export default {
  async fetch(request, env) {
    const url = new URL(request.url);

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

    if (!username || !/^[a-z0-9][a-z0-9-]{0,31}$/.test(username)) {
      return new Response(
        JSON.stringify({ error: "username must be lowercase alphanumeric/hyphen, 1-32 chars" }),
        { status: 400 }
      );
    }

    const json = (data, status = 200) =>
      new Response(JSON.stringify(data), { status, headers: { "Content-Type": "application/json" } });

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
};
