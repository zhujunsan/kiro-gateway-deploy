/**
 * Tests for public-tunnel DNS repair predicates.
 * Run: node --test worker/tests/dns.test.js
 */
import { describe, it } from "node:test";
import assert from "node:assert/strict";

import { cnameContent, dnsRecordNeedsRepair, shouldCleanupIdleTunnel } from "../src/index.js";

const TUNNEL_ID = "0661cfaa-4f6f-4f88-a4a3-c00e6f0823b9";
const EXPECTED = `${TUNNEL_ID}.cfargotunnel.com`;

function rec(overrides = {}) {
  return {
    type: "CNAME",
    content: EXPECTED,
    proxied: true,
    ...overrides,
  };
}

describe("cnameContent", () => {
  it("points at the tunnel's cfargotunnel.com name", () => {
    assert.equal(cnameContent(TUNNEL_ID), EXPECTED);
  });
});

describe("dnsRecordNeedsRepair", () => {
  it("repairs when there are no records", () => {
    assert.equal(dnsRecordNeedsRepair([], TUNNEL_ID), true);
    assert.equal(dnsRecordNeedsRepair(null, TUNNEL_ID), true);
  });

  it("accepts the single proxied CNAME for this tunnel", () => {
    assert.equal(dnsRecordNeedsRepair([rec()], TUNNEL_ID), false);
  });

  it("treats a trailing-dot CNAME target as already correct", () => {
    assert.equal(
      dnsRecordNeedsRepair([rec({ content: EXPECTED + "." })], TUNNEL_ID),
      false,
    );
  });

  it("repairs an unproxied CNAME (HTTPS will not terminate at the edge)", () => {
    assert.equal(dnsRecordNeedsRepair([rec({ proxied: false })], TUNNEL_ID), true);
  });

  it("repairs a CNAME that points at a different tunnel", () => {
    assert.equal(
      dnsRecordNeedsRepair(
        [rec({ content: "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee.cfargotunnel.com" })],
        TUNNEL_ID,
      ),
      true,
    );
  });

  it("repairs leftover A/AAAA records that block CNAME creation", () => {
    assert.equal(
      dnsRecordNeedsRepair(
        [rec(), { type: "A", content: "1.2.3.4", proxied: true }],
        TUNNEL_ID,
      ),
      true,
    );
    assert.equal(
      dnsRecordNeedsRepair(
        [{ type: "A", content: "1.2.3.4", proxied: true }],
        TUNNEL_ID,
      ),
      true,
    );
  });
});

const DAY = 86400000;
const NOW = Date.parse("2026-08-25T00:00:00Z");
const THRESHOLD = 30 * DAY;
const PREFIX = "kg-";

function daysAgo(n) {
  return new Date(NOW - n * DAY).toISOString();
}

function tunnel(overrides = {}) {
  return {
    name: "kg-alice",
    status: "down",
    created_at: daysAgo(90),
    ...overrides,
  };
}

describe("shouldCleanupIdleTunnel", () => {
  it("never cleans a healthy or degraded tunnel (degraded still serves traffic)", () => {
    assert.equal(
      shouldCleanupIdleTunnel(tunnel({ status: "healthy", conns_inactive_at: daysAgo(40) }), NOW, THRESHOLD, PREFIX),
      false,
    );
    assert.equal(
      shouldCleanupIdleTunnel(tunnel({ status: "degraded", conns_inactive_at: daysAgo(40) }), NOW, THRESHOLD, PREFIX),
      false,
    );
  });

  it("cleans a down tunnel whose last inactive timestamp is past the threshold", () => {
    assert.equal(
      shouldCleanupIdleTunnel(tunnel({
        status: "down",
        conns_active_at: daysAgo(50),
        conns_inactive_at: daysAgo(40),
      }), NOW, THRESHOLD, PREFIX),
      true,
    );
  });

  it("does not treat created_at as idle for a down blip (live user false-positive)", () => {
    assert.equal(
      shouldCleanupIdleTunnel(tunnel({
        status: "down",
        created_at: daysAgo(90),
        conns_active_at: daysAgo(1),
        conns_inactive_at: null,
      }), NOW, THRESHOLD, PREFIX),
      false,
    );
    assert.equal(
      shouldCleanupIdleTunnel(tunnel({
        status: "down",
        created_at: daysAgo(90),
      }), NOW, THRESHOLD, PREFIX),
      false,
    );
  });

  it("uses created_at only for never-run inactive tunnels", () => {
    assert.equal(
      shouldCleanupIdleTunnel(tunnel({ status: "inactive", created_at: daysAgo(40) }), NOW, THRESHOLD, PREFIX),
      true,
    );
    assert.equal(
      shouldCleanupIdleTunnel(tunnel({ status: "inactive", created_at: daysAgo(5) }), NOW, THRESHOLD, PREFIX),
      false,
    );
  });

  it("does not clean if the tunnel reconnected after the last inactive timestamp", () => {
    assert.equal(
      shouldCleanupIdleTunnel(tunnel({
        status: "down",
        conns_inactive_at: daysAgo(40),
        conns_active_at: daysAgo(1),
      }), NOW, THRESHOLD, PREFIX),
      false,
    );
  });

  it("ignores tunnels outside the project prefix", () => {
    assert.equal(
      shouldCleanupIdleTunnel(tunnel({ name: "other-alice", status: "inactive", created_at: daysAgo(40) }), NOW, THRESHOLD, PREFIX),
      false,
    );
  });
});
