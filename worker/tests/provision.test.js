/**
 * Concurrent-safe /provision: reuse existing tunnels, serialize per username.
 * Run: node --test worker/tests/provision.test.js
 */
import { describe, it } from "node:test";
import assert from "node:assert/strict";

import {
  PROVISION_LEASE_MS,
  ProvisionConflictError,
  StaleGenerationError,
  createMemoryLockStore,
  provisionTunnel,
} from "../src/provision.js";

const USER = "alice";
const HOST = "kg-alice.example.com";
const NAME = "kg-alice";
const PORT = 64005;
const ENV = { TELEMETRY_SECRET: "tel-secret", HOSTNAME_PREFIX: "kg", DOMAIN_SUFFIX: "example.com" };

function createFakeCf({ existing } = {}) {
  const tunnels = new Map();
  const tokens = new Map();
  const dns = new Map();
  const ops = [];
  let dnsAbsent = false;
  let dnsGap = 0;
  let tokenId = 0;

  if (existing) {
    tunnels.set(existing.name, { id: existing.id, name: existing.name, token: existing.token });
    if (existing.token) tokens.set(existing.id, existing.token);
    if (existing.hostname) dns.set(existing.hostname, existing.id);
  }

  const cf = {
    ops,
    tunnels,
    dns,
    get dnsGap() {
      return dnsGap;
    },
    async findTunnelByName(name) {
      const t = tunnels.get(name);
      return t ? { ...t } : null;
    },
    async createTunnel(name) {
      tokenId += 1;
      const id = `tun-${tokenId}`;
      const token = `token-${id}`;
      const t = { id, name, token };
      tunnels.set(name, t);
      tokens.set(id, token);
      ops.push({ op: "createTunnel", name, id });
      return { ...t };
    },
    async deleteTunnel(id) {
      ops.push({ op: "deleteTunnel", id });
      for (const [name, t] of [...tunnels.entries()]) {
        if (t.id === id) tunnels.delete(name);
      }
      tokens.delete(id);
    },
    async deleteDnsRecord(hostname) {
      ops.push({ op: "deleteDns", hostname });
      dns.delete(hostname);
      dnsAbsent = true;
    },
    async setIngress(id, hostname, port) {
      ops.push({ op: "setIngress", id, hostname, port });
    },
    async ensureDnsRecord(hostname, tunnelId) {
      ops.push({ op: "ensureDns", hostname, tunnelId });
      if (dnsAbsent) dnsGap += 1;
      dns.set(hostname, tunnelId);
      dnsAbsent = false;
      return { repaired: true };
    },
    async getTunnelToken(id) {
      if (!tokens.has(id)) throw new Error("no token");
      return tokens.get(id);
    },
  };
  return cf;
}

function args(overrides = {}) {
  return {
    env: ENV,
    username: USER,
    port: PORT,
    hostname: HOST,
    tunnelName: NAME,
    nowMs: 1_000_000,
    ...overrides,
  };
}

describe("provisionTunnel reuse", () => {
  it("reuses an existing tunnel without deleting tunnel or DNS", async () => {
    const cf = createFakeCf({
      existing: { id: "tun-old", name: NAME, token: "token-old", hostname: HOST },
    });
    const result = await provisionTunnel(args({ cf, locks: createMemoryLockStore() }));
    assert.equal(result.reused, true);
    assert.equal(result.run_token, "token-old");
    assert.equal(result.hostname, HOST);
    assert.equal(result.telemetry_secret, "tel-secret");
    assert.equal(cf.tunnels.size, 1);
    assert.equal(cf.tunnels.get(NAME).id, "tun-old");
    assert.equal(cf.ops.some((o) => o.op === "deleteTunnel"), false);
    assert.equal(cf.ops.some((o) => o.op === "deleteDns"), false);
    assert.equal(cf.ops.some((o) => o.op === "createTunnel"), false);
    assert.equal(cf.ops.some((o) => o.op === "setIngress"), true);
    assert.equal(cf.ops.some((o) => o.op === "ensureDns"), true);
    assert.equal(cf.dnsGap, 0);
  });

  it("creates a tunnel when none exists", async () => {
    const cf = createFakeCf();
    const result = await provisionTunnel(args({ cf, locks: createMemoryLockStore() }));
    assert.equal(result.reused, false);
    assert.ok(result.run_token.startsWith("token-"));
    assert.equal(cf.tunnels.size, 1);
    assert.equal(cf.dns.get(HOST), cf.tunnels.get(NAME).id);
  });

  it("rotates only when token fetch fails, under the same lease", async () => {
    const cf = createFakeCf({
      existing: { id: "tun-old", name: NAME, token: null, hostname: HOST },
    });
    cf.getTunnelToken = async () => {
      throw new Error("no token");
    };
    const result = await provisionTunnel(args({ cf, locks: createMemoryLockStore() }));
    assert.equal(result.rotated, true);
    assert.equal(result.reused, false);
    assert.equal(cf.ops.filter((o) => o.op === "deleteTunnel").length, 1);
    assert.equal(cf.tunnels.size, 1);
    assert.notEqual(cf.tunnels.get(NAME).id, "tun-old");
  });
});

describe("provisionTunnel concurrency", () => {
  it("same username: one winner, waiters get 409 and delete nothing", async () => {
    const cf = createFakeCf();
    const locks = createMemoryLockStore();
    let unblock;
    const gate = new Promise((resolve) => {
      unblock = resolve;
    });
    let held;
    const heldP = new Promise((resolve) => {
      held = resolve;
    });
    const originalCreate = cf.createTunnel.bind(cf);
    cf.createTunnel = async (name) => {
      held();
      await gate;
      return originalCreate(name);
    };

    const first = provisionTunnel(args({ cf, locks, holder: "a" }));
    await heldP;
    const waiters = await Promise.allSettled([
      provisionTunnel(args({ cf, locks, holder: "b" })),
      provisionTunnel(args({ cf, locks, holder: "c" })),
    ]);
    unblock();
    const won = await first;
    assert.equal(won.reused, false);
    assert.equal(cf.tunnels.size, 1);
    assert.equal(cf.ops.filter((o) => o.op === "createTunnel").length, 1);
    assert.equal(cf.ops.filter((o) => o.op === "deleteTunnel").length, 0);
    assert.equal(cf.dnsGap, 0);
    for (const w of waiters) {
      assert.equal(w.status, "rejected");
      assert.equal(w.reason instanceof ProvisionConflictError, true);
      assert.equal(w.reason.status, 409);
    }
  });

  it("expired lease can be taken over", async () => {
    const locks = createMemoryLockStore();
    const first = await locks.acquire(USER, "a", 1000);
    assert.equal(first.acquired, true);
    assert.equal(first.generation, 1);
    const blocked = await locks.acquire(USER, "b", 1000 + 10);
    assert.equal(blocked.acquired, false);
    const later = await locks.acquire(USER, "b", 1000 + PROVISION_LEASE_MS + 1);
    assert.equal(later.acquired, true);
    assert.equal(later.generation, 2);
  });

  it("stale generation must not delete a newer tunnel or DNS", async () => {
    const cf = createFakeCf({
      existing: { id: "tun-old", name: NAME, token: null, hostname: HOST },
    });
    const locks = createMemoryLockStore();
    const now = 1_000_000;
    cf.getTunnelToken = async () => {
      const takeover = await locks.acquire(USER, "b", now + PROVISION_LEASE_MS + 1);
      assert.equal(takeover.acquired, true);
      await cf.deleteTunnel("tun-old");
      const fresh = await cf.createTunnel(NAME);
      await cf.ensureDnsRecord(HOST, fresh.id);
      throw new Error("no token");
    };
    await assert.rejects(
      () => provisionTunnel(args({ cf, locks, nowMs: now, holder: "a" })),
      (err) => err instanceof StaleGenerationError,
    );
    assert.equal(cf.tunnels.size, 1);
    const live = cf.tunnels.get(NAME);
    assert.ok(live);
    assert.notEqual(live.id, "tun-old");
    assert.equal(cf.dns.get(HOST), live.id);
    // A's rotate must not have deleted B's tunnel.
    const deletes = cf.ops.filter((o) => o.op === "deleteTunnel").map((o) => o.id);
    assert.equal(deletes.includes(live.id), false);
  });
});
