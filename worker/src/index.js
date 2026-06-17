// worker/src/index.js
// Cloudflare Worker: provision a per-user cloudflared tunnel.
//
// POST /provision
//   body: { shared_secret: string, username: string }
//   → 201 { hostname, run_token }  (first call)
//   → 200 { hostname, run_token: null, message: "already provisioned" }  (repeat)
//
// Required Worker Secrets (set via wrangler secret put):
//   SHARED_SECRET   — the secret distributed to users out-of-band
//   CF_API_TOKEN    — scoped: Tunnel:Edit + DNS:Edit (botsonny.top only)
//   CF_ACCOUNT_ID
//   CF_ZONE_ID
//   DOMAIN_SUFFIX   — e.g. "botsonny.top"
//   HOSTNAME_PREFIX — e.g. "kg"  → final hostname = kg-<username>.<DOMAIN_SUFFIX>

const CF_API = "https://api.cloudflare.com/client/v4";

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

async function provision(env, username) {
  const prefix = env.HOSTNAME_PREFIX || "kg";
  const hostname = `${prefix}-${username}.${env.DOMAIN_SUFFIX}`;
  const tunnelName = `${prefix}-${username}`;

  // 1. Create tunnel
  const tunnel = await cfFetch(
    env,
    `/accounts/${env.CF_ACCOUNT_ID}/cfd_tunnel`,
    "POST",
    { name: tunnelName, config_src: "cloudflare" }
  );
  const tunnelId = tunnel.id;
  const runToken = tunnel.token;

  // 2. Set ingress (does NOT auto-create DNS record)
  await cfFetch(
    env,
    `/accounts/${env.CF_ACCOUNT_ID}/cfd_tunnel/${tunnelId}/configurations`,
    "PUT",
    {
      config: {
        ingress: [
          { hostname, service: "http://localhost:18000" },
          { service: "http_status:404" },
        ],
      },
    }
  );

  // 3. Create proxied CNAME DNS record (must be proxied=true for HTTPS to work)
  await cfFetch(
    env,
    `/zones/${env.CF_ZONE_ID}/dns_records`,
    "POST",
    {
      type: "CNAME",
      name: hostname,
      content: `${tunnelId}.cfargotunnel.com`,
      proxied: true,
    }
  );

  // 4. Persist in KV
  await env.PROVISION_KV.put(
    `user:${username}`,
    JSON.stringify({ tunnel_id: tunnelId, hostname, created_at: new Date().toISOString() })
  );

  return { hostname, run_token: runToken };
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    if (request.method === "POST" && url.pathname === "/provision") {
      let body;
      try {
        body = await request.json();
      } catch {
        return new Response(JSON.stringify({ error: "invalid JSON" }), { status: 400 });
      }

      const { shared_secret, username } = body || {};

      // Validate shared secret
      if (!shared_secret || shared_secret !== env.SHARED_SECRET) {
        return new Response(JSON.stringify({ error: "unauthorized" }), { status: 401 });
      }

      // Validate username: lowercase alphanumeric + hyphen, 1-32 chars
      if (!username || !/^[a-z0-9][a-z0-9-]{0,31}$/.test(username)) {
        return new Response(
          JSON.stringify({ error: "username must be lowercase alphanumeric/hyphen, 1-32 chars" }),
          { status: 400 }
        );
      }

      // Idempotency: already provisioned?
      const existing = await env.PROVISION_KV.get(`user:${username}`);
      if (existing) {
        const data = JSON.parse(existing);
        // Return hostname but NOT run_token (it was only returned once at creation)
        return new Response(
          JSON.stringify({ hostname: data.hostname, run_token: null, message: "already provisioned" }),
          { status: 200, headers: { "Content-Type": "application/json" } }
        );
      }

      try {
        const result = await provision(env, username);
        return new Response(JSON.stringify(result), {
          status: 201,
          headers: { "Content-Type": "application/json" },
        });
      } catch (err) {
        return new Response(JSON.stringify({ error: err.message }), { status: 500 });
      }
    }

    return new Response("not found", { status: 404 });
  },
};
