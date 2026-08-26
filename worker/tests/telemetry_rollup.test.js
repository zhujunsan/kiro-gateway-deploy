/**
 * Tests for usage_rollup field normalization (credits + latency).
 * Run: node --test worker/tests/
 *
 * normalizeRollupRow is imported from the Worker source rather than copied, so
 * these assertions can never silently drift from the code that actually runs.
 */
import { describe, it } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import {
  normalizeRollupRow,
  utcDayWindow,
  shouldRollupDaily,
  DAILY_ROLLUP_DAYS_AGO,
} from "../src/index.js";

const __dirname = path.dirname(fileURLToPath(import.meta.url));

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
      requests: 1,
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
      requests: 1,
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
      model: "m", app_version: "v", requests: 1, estimated_credits: -3,
    }, 1)[18], null);
    assert.equal(normalizeRollupRow({
      bucket_start: 1, bucket_seconds: 600, username: "abc123def456",
      model: "m", app_version: "v", requests: 1, estimated_credits: "NaN",
    }, 1)[18], null);
  });

  it("rejects idle zero-request buckets", () => {
    assert.equal(normalizeRollupRow({
      bucket_start: 1200,
      bucket_seconds: 600,
      username: "abc123def456",
      model: "m",
      app_version: "v",
      requests: 0,
      estimated_credits: 12.5,
    }, 1), null);
    assert.equal(normalizeRollupRow({
      bucket_start: 1200,
      bucket_seconds: 600,
      username: "abc123def456",
      model: "m",
      app_version: "v",
      // omitted requests → 0
      estimated_credits: 1,
    }, 1), null);
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
      requests: 1,
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

  it("daily rollup filters bucket_start by range so the bucket index can be used", () => {
    assert.match(src, /WHERE\s+bucket_start >= \? AND bucket_start < \?/i);
    assert.doesNotMatch(
      src,
      /WHERE\s+date\(bucket_start,\s*'unixepoch'\)\s*=/,
    );
  });
});

describe("utcDayWindow", () => {
  it("returns a 86400-second UTC day range for today and yesterday", () => {
    const now = new Date("2026-08-25T08:50:00Z");
    const today = utcDayWindow(now, 0);
    assert.equal(today.start, Date.UTC(2026, 7, 25) / 1000);
    assert.equal(today.end, today.start + 86400);
    const yesterday = utcDayWindow(now, 1);
    assert.equal(yesterday.start, Date.UTC(2026, 7, 24) / 1000);
    assert.equal(yesterday.end, today.start);
  });

  it("crosses month boundaries in UTC", () => {
    const now = new Date("2026-08-01T00:30:00Z");
    const yesterday = utcDayWindow(now, 1);
    assert.equal(yesterday.start, Date.UTC(2026, 6, 31) / 1000);
    assert.equal(yesterday.end, Date.UTC(2026, 7, 1) / 1000);
  });
});

describe("shouldRollupDaily", () => {
  it("runs only on the 00:xx UTC cron tick", () => {
    assert.equal(shouldRollupDaily(new Date("2026-08-26T00:07:00Z")), true);
    assert.equal(shouldRollupDaily(new Date("2026-08-26T00:59:00Z")), true);
    assert.equal(shouldRollupDaily(new Date("2026-08-26T01:07:00Z")), false);
    assert.equal(shouldRollupDaily(new Date("2026-08-26T08:07:00Z")), false);
    assert.equal(shouldRollupDaily(new Date("2026-08-25T23:07:00Z")), false);
  });
});

describe("DAILY_ROLLUP_DAYS_AGO", () => {
  it("rolls completed UTC days only, not the in-progress day", () => {
    assert.deepEqual(DAILY_ROLLUP_DAYS_AGO, [1, 2]);
  });
});
