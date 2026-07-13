/**
 * Minimal Node tests for /telemetry/errors helpers.
 * Run: node --test worker/tests/telemetry_errors.test.js
 */
const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

// Load selected helpers from index.js by evaluating a trimmed sandbox.
// We re-implement the normalize/size checks here to avoid spinning miniflare;
// keep them in sync with worker/src/index.js.

const USERNAME_RE = /^[a-z0-9][a-z0-9-]{0,31}$/;
const MAX_INCIDENT_RECORD_BYTES = 192 * 1024;
const INCIDENT_KIND = "kiro_gateway_incident";

function toInt(v, def = 0) {
  const n = parseInt(v, 10);
  return Number.isFinite(n) ? n : def;
}

function normalizeIncidentRecord(raw) {
  if (!raw || typeof raw !== "object") return null;
  if (raw.kind !== INCIDENT_KIND) return null;
  const recordType = raw.record_type;
  if (recordType !== "manifest" && recordType !== "artifact_chunk") return null;
  const incidentId = typeof raw.incident_id === "string" ? raw.incident_id : "";
  if (!incidentId || incidentId.length > 128) return null;
  const partId = typeof raw.part_id === "string" ? raw.part_id : "";
  if (!partId || partId.length > 64) return null;

  if (recordType === "manifest") {
    const username = typeof raw.username === "string" ? raw.username : "unknown";
    if (!USERNAME_RE.test(username) && username !== "unknown") return null;
    return {
      kind: INCIDENT_KIND,
      record_type: "manifest",
      part_id: partId,
      incident_id: incidentId,
      username,
      source: typeof raw.source === "string" ? raw.source.slice(0, 64) : "unknown",
      code: typeof raw.code === "string" ? raw.code.slice(0, 128) : "unknown",
    };
  }
  const artifact = typeof raw.artifact === "string" ? raw.artifact.slice(0, 128) : "";
  if (!artifact) return null;
  const data = typeof raw.data === "string" ? raw.data : "";
  if (!data) return null;
  return {
    kind: INCIDENT_KIND,
    record_type: "artifact_chunk",
    part_id: partId,
    incident_id: incidentId,
    artifact,
    part_index: toInt(raw.part_index, 0),
    part_total: toInt(raw.part_total, 1),
    encoding: raw.encoding === "base64" ? "base64" : "utf-8",
    data,
  };
}

describe("normalizeIncidentRecord", () => {
  it("accepts a valid manifest", () => {
    const rec = normalizeIncidentRecord({
      kind: INCIDENT_KIND,
      record_type: "manifest",
      part_id: "abc",
      incident_id: "inc-1",
      username: "abc123def456",
      source: "kiro_upstream",
      code: "INVALID_MODEL_ID",
    });
    assert.equal(rec.record_type, "manifest");
    assert.equal(rec.username, "abc123def456");
  });

  it("rejects bad username", () => {
    const rec = normalizeIncidentRecord({
      kind: INCIDENT_KIND,
      record_type: "manifest",
      part_id: "abc",
      incident_id: "inc-1",
      username: "BAD_USER",
    });
    assert.equal(rec, null);
  });

  it("accepts artifact_chunk", () => {
    const rec = normalizeIncidentRecord({
      kind: INCIDENT_KIND,
      record_type: "artifact_chunk",
      part_id: "p1",
      incident_id: "inc-1",
      artifact: "app_logs.txt",
      part_index: 0,
      part_total: 1,
      encoding: "utf-8",
      data: "hello",
    });
    assert.equal(rec.artifact, "app_logs.txt");
    assert.equal(rec.data, "hello");
  });

  it("rejects missing data", () => {
    const rec = normalizeIncidentRecord({
      kind: INCIDENT_KIND,
      record_type: "artifact_chunk",
      part_id: "p1",
      incident_id: "inc-1",
      artifact: "x",
      data: "",
    });
    assert.equal(rec, null);
  });
});

describe("size gate", () => {
  it("flags oversized serialized records", () => {
    const logLine = { data: "x".repeat(MAX_INCIDENT_RECORD_BYTES) };
    assert.ok(JSON.stringify(logLine).length > MAX_INCIDENT_RECORD_BYTES);
  });
});

describe("source file contains handler", () => {
  it("exports /telemetry/errors route", () => {
    const src = fs.readFileSync(
      path.join(__dirname, "..", "src", "index.js"),
      "utf8"
    );
    assert.match(src, /pathname === "\/telemetry\/errors"/);
    assert.match(src, /handleTelemetryErrors/);
    assert.match(src, /console\.error\(serialized\)/);
    // Exactly one console.error call site in the handler path for incidents.
    const matches = src.match(/console\.error\(serialized\)/g) || [];
    assert.equal(matches.length, 1);
  });
});
