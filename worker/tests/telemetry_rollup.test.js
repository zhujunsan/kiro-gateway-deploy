/**
 * Tests for usage_rollup field normalization (credits + latency).
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
    toInt(row.ttft_ms_sum),
    toInt(row.ttft_count),
    toInt(row.generation_ms_sum),
    toInt(row.generation_count),
    toInt(row.generation_completion_tokens_sum),
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
    assert.equal(params.length, 22);
    assert.equal(params[13], 0); // ttft_ms_sum (omitted → 0)
    assert.equal(params[17], 0); // generation_completion_tokens_sum
    assert.equal(params[18], null); // estimated_credits
    assert.equal(params[19], null); // segments
    assert.equal(params[20], null); // missing
    assert.equal(params[21], 999);
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
    assert.equal(params[18], 0);
    assert.equal(params[19], 1);
    assert.equal(params[20], 0);
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
    assert.equal(params[18], 12.5);
    assert.equal(params[19], 2);
    assert.equal(params[20], 1);
  });

  it("invalid / negative credits become null", () => {
    assert.equal(normalizeRollupRow({
      bucket_start: 1, bucket_seconds: 600, username: "abc123def456",
      model: "m", app_version: "v", estimated_credits: -3,
    }, 1)[18], null);
    assert.equal(normalizeRollupRow({
      bucket_start: 1, bucket_seconds: 600, username: "abc123def456",
      model: "m", app_version: "v", estimated_credits: "NaN",
    }, 1)[18], null);
  });
});

describe("normalizeRollupRow latency fields", () => {
  it("accepts ttft / generation throughput sums", () => {
    const params = normalizeRollupRow({
      bucket_start: 1200,
      bucket_seconds: 600,
      username: "abc123def456",
      model: "m",
      app_version: "v",
      ttft_ms_sum: 2500,
      ttft_count: 5,
      generation_ms_sum: 10000,
      generation_count: 4,
      generation_completion_tokens_sum: 800,
    }, 1);
    assert.equal(params[13], 2500);
    assert.equal(params[14], 5);
    assert.equal(params[15], 10000);
    assert.equal(params[16], 4);
    assert.equal(params[17], 800);
  });
});

describe("index.js SQL includes credit and latency columns", () => {
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

  it("INSERT and daily rollup mention ttft / generation fields", () => {
    assert.match(src, /ttft_ms_sum/);
    assert.match(src, /ttft_count/);
    assert.match(src, /generation_ms_sum/);
    assert.match(src, /generation_count/);
    assert.match(src, /generation_completion_tokens_sum/);
    assert.match(src, /SUM\(ttft_ms_sum\)/);
    assert.match(src, /SUM\(generation_completion_tokens_sum\)/);
  });
});
