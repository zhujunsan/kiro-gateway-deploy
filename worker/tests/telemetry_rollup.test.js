/**
 * Tests for usage_rollup credit field normalization.
 * Run: node --test worker/tests/telemetry_rollup.test.js
 */
const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const USERNAME_RE = /^[a-z0-9][a-z0-9-]{0,31}$/;

function toInt(v, def = 0) {
  const n = parseInt(v, 10);
  return Number.isFinite(n) ? n : def;
}

function toOptionalNonNegFloat(v) {
  if (v === undefined || v === null || v === "") return null;
  const n = Number(v);
  if (!Number.isFinite(n) || n < 0) return null;
  return n;
}

function toOptionalNonNegInt(v) {
  if (v === undefined || v === null || v === "") return null;
  const n = parseInt(v, 10);
  if (!Number.isFinite(n) || n < 0) return null;
  return n;
}

// Keep in sync with worker/src/index.js normalizeRollupRow.
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
    toOptionalNonNegFloat(row.estimated_credits),
    toOptionalNonNegInt(row.credit_estimate_segments),
    toOptionalNonNegInt(row.credit_estimate_missing_segments),
    receivedAt,
  ];
}

describe("normalizeRollupRow credit fields", () => {
  it("omitted credit fields become null (not 0)", () => {
    const params = normalizeRollupRow({
      bucket_start: 1200,
      bucket_seconds: 600,
      username: "abc123def456",
      model: "kiro-o-4.8",
      app_version: "0.3.13",
      requests: 2,
      successes: 2,
      errors: 0,
      prompt_tokens_sum: 10,
      completion_tokens_sum: 5,
      total_tokens_sum: 15,
      request_bytes_sum: 100,
      response_bytes_sum: 200,
    }, 999);
    assert.equal(params.length, 17);
    assert.equal(params[13], null); // estimated_credits
    assert.equal(params[14], null); // segments
    assert.equal(params[15], null); // missing
    assert.equal(params[16], 999);
  });

  it("explicit zero credits stay zero", () => {
    const params = normalizeRollupRow({
      bucket_start: 1200,
      bucket_seconds: 600,
      username: "abc123def456",
      model: "m",
      app_version: "v",
      estimated_credits: 0,
      credit_estimate_segments: 1,
      credit_estimate_missing_segments: 0,
    }, 1);
    assert.equal(params[13], 0);
    assert.equal(params[14], 1);
    assert.equal(params[15], 0);
  });

  it("accepts valid estimated_credits", () => {
    const params = normalizeRollupRow({
      bucket_start: 1200,
      bucket_seconds: 600,
      username: "abc123def456",
      model: "m",
      app_version: "v",
      estimated_credits: 12.5,
      credit_estimate_segments: 2,
      credit_estimate_missing_segments: 1,
    }, 1);
    assert.equal(params[13], 12.5);
    assert.equal(params[14], 2);
    assert.equal(params[15], 1);
  });

  it("invalid / negative credits become null", () => {
    assert.equal(normalizeRollupRow({
      bucket_start: 1, bucket_seconds: 600, username: "abc123def456",
      model: "m", app_version: "v", estimated_credits: -3,
    }, 1)[13], null);
    assert.equal(normalizeRollupRow({
      bucket_start: 1, bucket_seconds: 600, username: "abc123def456",
      model: "m", app_version: "v", estimated_credits: "NaN",
    }, 1)[13], null);
  });
});

describe("index.js SQL includes credit columns", () => {
  const src = fs.readFileSync(
    path.join(__dirname, "..", "src", "index.js"),
    "utf8",
  );

  it("INSERT and daily rollup mention estimated_credits", () => {
    assert.match(src, /estimated_credits/);
    assert.match(src, /credit_estimate_segments/);
    assert.match(src, /credit_estimate_missing_segments/);
    assert.match(src, /SUM\(estimated_credits\)/);
    assert.match(src, /toOptionalNonNegFloat/);
  });
});
