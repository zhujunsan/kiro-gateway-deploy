// Per-username provision: reuse existing tunnels, serialize via a lease lock.
// Cloudflare API calls are injected (cf) so node --test can drive concurrency
// without miniflare or the live Cloudflare API.

export const PROVISION_LEASE_MS = 60_000;

export class ProvisionConflictError extends Error {
  constructor(retryAfter) {
    super("provision in progress");
    this.status = 409;
    this.retryAfter = retryAfter;
  }
}

export class StaleGenerationError extends Error {
  constructor() {
    super("stale provision generation");
    this.status = 409;
  }
}

function telemetryFields(env) {
  return env?.TELEMETRY_SECRET ? { telemetry_secret: env.TELEMETRY_SECRET } : {};
}

export function createMemoryLockStore() {
  const rows = new Map();
  return {
    async acquire(username, holder, nowMs) {
      const row = rows.get(username);
      if (!row || row.leaseUntil <= nowMs) {
        const generation = (row?.generation || 0) + 1;
        rows.set(username, {
          leaseUntil: nowMs + PROVISION_LEASE_MS,
          generation,
          holder,
        });
        return { acquired: true, generation };
      }
      return {
        acquired: false,
        retryAfter: Math.max(1, Math.ceil((row.leaseUntil - nowMs) / 1000)),
      };
    },
    async release(username, holder) {
      const row = rows.get(username);
      if (row && row.holder === holder) {
        row.leaseUntil = 0;
        row.holder = null;
      }
    },
    async currentGeneration(username) {
      return rows.get(username)?.generation || 0;
    },
  };
}

const SQL_ACQUIRE = `
INSERT INTO provision_lock (username, lease_until, generation, holder, updated_at)
VALUES (?1, ?2, 1, ?3, ?4)
ON CONFLICT(username) DO UPDATE SET
  lease_until = excluded.lease_until,
  generation = provision_lock.generation + 1,
  holder = excluded.holder,
  updated_at = excluded.updated_at
WHERE provision_lock.lease_until <= excluded.updated_at
`;

const SQL_RELEASE = `
UPDATE provision_lock
SET lease_until = 0, holder = NULL, updated_at = ?3
WHERE username = ?1 AND holder = ?2
`;

export function createD1LockStore(db) {
  if (!db) {
    throw new Error("TELEMETRY_DB is required for provision locking");
  }
  return {
    async acquire(username, holder, nowMs) {
      const nowSec = Math.floor(nowMs / 1000);
      const leaseUntil = nowSec + Math.floor(PROVISION_LEASE_MS / 1000);
      const result = await db.prepare(SQL_ACQUIRE)
        .bind(username, leaseUntil, holder, nowSec)
        .run();
      if (result?.meta?.changes === 1) {
        const row = await db.prepare(
          "SELECT generation FROM provision_lock WHERE username = ?1",
        ).bind(username).first();
        return { acquired: true, generation: row?.generation ?? 1 };
      }
      const row = await db.prepare(
        "SELECT lease_until FROM provision_lock WHERE username = ?1",
      ).bind(username).first();
      const retryAfter = row
        ? Math.max(1, Number(row.lease_until) - nowSec)
        : 1;
      return { acquired: false, retryAfter };
    },
    async release(username, holder, nowMs = Date.now()) {
      const nowSec = Math.floor(nowMs / 1000);
      await db.prepare(SQL_RELEASE).bind(username, holder, nowSec).run();
    },
    async currentGeneration(username) {
      const row = await db.prepare(
        "SELECT generation FROM provision_lock WHERE username = ?1",
      ).bind(username).first();
      return row?.generation || 0;
    },
  };
}

async function assertCurrentGeneration(locks, username, generation) {
  const current = await locks.currentGeneration(username);
  if (current !== generation) {
    throw new StaleGenerationError();
  }
}

async function rotateTunnel(cf, locks, username, generation, existing, hostname, tunnelName, port) {
  await assertCurrentGeneration(locks, username, generation);
  await cf.deleteDnsRecord(hostname);
  await assertCurrentGeneration(locks, username, generation);
  await cf.deleteTunnel(existing.id);
  const tunnel = await cf.createTunnel(tunnelName);
  await cf.setIngress(tunnel.id, hostname, port);
  await cf.ensureDnsRecord(hostname, tunnel.id);
  return tunnel;
}

async function tokenFor(cf, tunnel) {
  if (tunnel?.token) return tunnel.token;
  return cf.getTunnelToken(tunnel.id);
}

export async function provisionTunnel({
  env,
  username,
  port,
  hostname,
  tunnelName,
  cf,
  locks,
  nowMs = Date.now(),
  holder = `p-${nowMs}-${Math.random().toString(36).slice(2, 10)}`,
}) {
  const lease = await locks.acquire(username, holder, nowMs);
  if (!lease.acquired) {
    throw new ProvisionConflictError(lease.retryAfter);
  }
  try {
    const existing = await cf.findTunnelByName(tunnelName);
    if (existing) {
      await cf.setIngress(existing.id, hostname, port);
      await cf.ensureDnsRecord(hostname, existing.id);
      let token;
      try {
        token = await tokenFor(cf, existing);
      } catch (err) {
        console.log(
          `[provision] token fetch failed for ${existing.id}, rotating: ${err.message}`,
        );
        const rotated = await rotateTunnel(
          cf, locks, username, lease.generation,
          existing, hostname, tunnelName, port,
        );
        return {
          hostname,
          run_token: rotated.token,
          reused: false,
          rotated: true,
          ...telemetryFields(env),
        };
      }
      return {
        hostname,
        run_token: token,
        reused: true,
        rotated: false,
        ...telemetryFields(env),
      };
    }

    const tunnel = await cf.createTunnel(tunnelName);
    await cf.setIngress(tunnel.id, hostname, port);
    await cf.ensureDnsRecord(hostname, tunnel.id);
    return {
      hostname,
      run_token: tunnel.token,
      reused: false,
      rotated: false,
      ...telemetryFields(env),
    };
  } finally {
    await locks.release(username, holder, nowMs);
  }
}

export async function lookupAuthoritativeCname(hostname) {
  try {
    const res = await fetch(
      `https://cloudflare-dns.com/dns-query?name=${encodeURIComponent(hostname)}&type=CNAME`,
      { headers: { accept: "application/dns-json" } },
    );
    if (!res.ok) return null;
    const data = await res.json();
    const answers = data.Answer || [];
    return answers.some((a) => Number(a.type) === 5);
  } catch {
    return null;
  }
}
