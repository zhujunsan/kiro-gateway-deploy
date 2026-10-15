import { describe, it } from "node:test";
import assert from "node:assert/strict";

import { auditConfigFor, validateAuditKeys } from "../src/client_config.js";

const KEY = "audit/v1/26-09-30/alice/20260930T120000Z-abcd1234.req.jsonl.zst";

describe("auditConfigFor", () => {
  it("fails closed when the row is missing or garbage", () => {
    assert.deepEqual(auditConfigFor(null, "alice"), { mode: "off", rotate_seconds: 600 });
    assert.deepEqual(auditConfigFor("{", "alice"), { mode: "off", rotate_seconds: 600 });
    assert.deepEqual(auditConfigFor({}, "alice").mode, "off");
  });

  it("returns best_effort for everyone when the allow-list is empty", () => {
    const raw = JSON.stringify({ mode: "best_effort", usernames: [], rotate_seconds: 300 });
    assert.deepEqual(auditConfigFor(raw, "alice"), { mode: "best_effort", rotate_seconds: 300 });
    assert.equal(auditConfigFor(raw, "bob").mode, "best_effort");
  });

  it("restricts best_effort to the allow-list", () => {
    const raw = JSON.stringify({ mode: "best_effort", usernames: ["alice"] });
    assert.equal(auditConfigFor(raw, "alice").mode, "best_effort");
    assert.equal(auditConfigFor(raw, "bob").mode, "off");
  });
});

describe("validateAuditKeys", () => {
  it("accepts the caller's own req and blob keys", () => {
    const blob = KEY.replace(".req.", ".blob.");
    assert.equal(validateAuditKeys("alice", [KEY, blob]), null);
  });

  it("accepts an Identity Center user id directory", () => {
    const key = "audit/v1/26-10-08/d-9567aedac7.a7141a78-2011-70e3-c737-1500af9bdb40/20261008T120000Z-abcd1234.req.jsonl.zst";
    assert.equal(validateAuditKeys("alice", [key]), null);
    assert.equal(validateAuditKeys("alice", ["audit/v1/26-10-08/d-1.abc/20261008T120000Z-abcd1234.req.jsonl.zst"]), "invalid key");
  });

  it("rejects another user, traversal, a bad extension, and too many keys", () => {
    assert.equal(validateAuditKeys("bob", [KEY]), "invalid key");
    assert.match(validateAuditKeys("alice", [KEY.replace("/alice/", "/alice/../../etc/")]), /invalid/);
    assert.equal(validateAuditKeys("alice", ["audit/v1/26-09-30/alice/notes.txt"]), "invalid key");
    assert.equal(validateAuditKeys("alice", []), "keys required");
    assert.equal(validateAuditKeys("alice", Array(11).fill(KEY)), "too many keys");
  });
});
