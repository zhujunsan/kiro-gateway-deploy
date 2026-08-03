/**
 * Tests for announcement targeting (worker/src/announcements.js).
 * Run: node --test   (from worker/)
 *
 * These cover the "who sees what" rules, which is where a mistake is expensive:
 * a wrong match broadcasts an internal notice to everyone, a wrong miss silently
 * loses a notice nobody can see.
 */
import { describe, it } from "node:test";
import assert from "node:assert/strict";

import {
  ANNOUNCEMENT_MAX,
  ANNOUNCEMENTS_SELECT_SQL,
  clientContextFromRequest,
  compareVersions,
  parseCsvList,
  parseTrayUserAgent,
  parseVersion,
  selectAnnouncements,
  versionInRange,
} from "../src/announcements.js";

const NOW = 1_800_000_000;

/** A row that matches everything, so each test only states what it changes. */
function row(overrides = {}) {
  return {
    id: 1,
    body: "维护通知",
    tag: null,
    url: null,
    level: "info",
    priority: 0,
    enabled: 1,
    starts_at: null,
    ends_at: null,
    min_version: null,
    max_version: null,
    target_platforms: null,
    ...overrides,
  };
}

function pick(rows, ctx = {}) {
  return selectAnnouncements(rows, {
    appVersion: "0.4.22",
    platform: "macos",
    now: NOW,
    ...ctx,
  });
}

function ids(rows, ctx = {}) {
  return pick(rows, ctx).map((a) => a.id);
}

describe("parseVersion / compareVersions", () => {
  it("accepts the shapes the app actually reports", () => {
    assert.deepEqual(parseVersion("0.4.22"), [0, 4, 22]);
    assert.deepEqual(parseVersion("v0.4.22"), [0, 4, 22]);
    assert.deepEqual(parseVersion("0.4.22-beta.1"), [0, 4, 22]);
    assert.deepEqual(parseVersion("0.4"), [0, 4, 0]);
  });

  it("returns null for anything that is not a version", () => {
    assert.equal(parseVersion("unknown"), null);
    assert.equal(parseVersion(""), null);
    assert.equal(parseVersion(null), null);
    assert.equal(parseVersion(undefined), null);
    assert.equal(parseVersion("7"), null); // 单个数字不算版本
  });

  it("compares numerically, not lexicographically", () => {
    assert.equal(compareVersions([0, 4, 9], [0, 4, 10]), -1);
    assert.equal(compareVersions([0, 10, 0], [0, 9, 99]), 1);
    assert.equal(compareVersions([1, 0, 0], [1, 0, 0]), 0);
  });
});

describe("parseTrayUserAgent / clientContextFromRequest", () => {
  it("parses the canonical tray UA", () => {
    assert.deepEqual(parseTrayUserAgent("KiroGatewayTray/0.4.22 (macos)"), {
      appVersion: "0.4.22",
      platform: "macos",
    });
  });

  it("accepts UA without a platform suffix", () => {
    assert.deepEqual(parseTrayUserAgent("KiroGatewayTray/0.4.22"), {
      appVersion: "0.4.22",
      platform: "",
    });
  });

  it("ignores unrelated user agents", () => {
    assert.deepEqual(parseTrayUserAgent("curl/8.0"), {
      appVersion: "",
      platform: "",
    });
    assert.deepEqual(parseTrayUserAgent(""), {
      appVersion: "",
      platform: "",
    });
  });

  it("prefers User-Agent over body fields", () => {
    const ctx = clientContextFromRequest({
      headers: { "User-Agent": "KiroGatewayTray/0.4.22 (macos)" },
      body: { app_version: "0.1.0", platform: "windows" },
      now: NOW,
    });
    assert.equal(ctx.appVersion, "0.4.22");
    assert.equal(ctx.platform, "macos");
    assert.equal(ctx.now, NOW);
  });

  it("falls back to body when UA is missing (curl debugging)", () => {
    const ctx = clientContextFromRequest({
      headers: {},
      body: { app_version: "0.4.10", platform: "linux" },
      now: NOW,
    });
    assert.equal(ctx.appVersion, "0.4.10");
    assert.equal(ctx.platform, "linux");
  });

  it("reads Headers.get when present", () => {
    const headers = new Map([["user-agent", "KiroGatewayTray/1.2.3 (windows)"]]);
    headers.get = Map.prototype.get;
    const ctx = clientContextFromRequest({ headers, body: {}, now: NOW });
    assert.equal(ctx.appVersion, "1.2.3");
    assert.equal(ctx.platform, "windows");
  });
});

describe("versionInRange", () => {
  it("no bounds means every version matches", () => {
    assert.equal(versionInRange("0.1.0", null, null), true);
    assert.equal(versionInRange("unknown", null, ""), true);
  });

  it("treats both bounds as inclusive", () => {
    assert.equal(versionInRange("0.4.20", "0.4.20", "0.4.22"), true);
    assert.equal(versionInRange("0.4.22", "0.4.20", "0.4.22"), true);
    assert.equal(versionInRange("0.4.19", "0.4.20", "0.4.22"), false);
    assert.equal(versionInRange("0.4.23", "0.4.20", "0.4.22"), false);
  });

  it("supports open-ended ranges", () => {
    assert.equal(versionInRange("9.9.9", "0.4.20", null), true);
    assert.equal(versionInRange("0.1.0", "0.4.20", null), false);
    assert.equal(versionInRange("0.1.0", null, "0.4.20"), true);
    assert.equal(versionInRange("9.9.9", null, "0.4.20"), false);
  });

  it("hides the notice when a bound exists but the client version is unknown", () => {
    assert.equal(versionInRange("", "0.4.20", null), false);
    assert.equal(versionInRange("unknown", null, "0.4.20"), false);
  });

  it("fails closed on a malformed bound instead of ignoring it", () => {
    assert.equal(versionInRange("0.4.22", "latest", null), false);
    assert.equal(versionInRange("0.4.22", null, "???"), false);
  });
});

describe("parseCsvList", () => {
  it("trims, lowercases and drops empties", () => {
    assert.deepEqual(parseCsvList(" a, B ,,c "), ["a", "b", "c"]);
    assert.deepEqual(parseCsvList(""), []);
    assert.deepEqual(parseCsvList(null), []);
    assert.deepEqual(parseCsvList(",, ,"), []);
  });
});

describe("selectAnnouncements — enable / time window", () => {
  it("shows a fully open announcement", () => {
    assert.deepEqual(ids([row()]), [1]);
  });

  it("respects the manual kill switch", () => {
    assert.deepEqual(ids([row({ enabled: 0 })]), []);
  });

  it("starts_at is inclusive, ends_at is exclusive", () => {
    assert.deepEqual(ids([row({ starts_at: NOW })]), [1]);
    assert.deepEqual(ids([row({ starts_at: NOW + 1 })]), []);
    assert.deepEqual(ids([row({ ends_at: NOW + 1 })]), [1]);
    assert.deepEqual(ids([row({ ends_at: NOW })]), []);
  });

  it("handles a half-open window on either side", () => {
    assert.deepEqual(ids([row({ starts_at: NOW - 10, ends_at: null })]), [1]);
    assert.deepEqual(ids([row({ starts_at: null, ends_at: NOW + 10 })]), [1]);
    assert.deepEqual(ids([row({ starts_at: NOW - 20, ends_at: NOW - 10 })]), []);
  });
});

describe("selectAnnouncements — targeting", () => {
  it("filters by version range", () => {
    assert.deepEqual(ids([row({ min_version: "0.4.22" })]), [1]);
    assert.deepEqual(ids([row({ min_version: "0.5.0" })]), []);
    assert.deepEqual(ids([row({ max_version: "0.4.0" })]), []);
  });

  it("hides version-scoped notices from clients that report no version", () => {
    assert.deepEqual(ids([row({ min_version: "0.4.0" })], { appVersion: "" }), []);
    // 无版本区间的公告仍然照发；缺版本通常只发生在没带 UA 的手工调用。
    assert.deepEqual(ids([row()], { appVersion: "" }), [1]);
  });

  it("filters by platform", () => {
    assert.deepEqual(ids([row({ target_platforms: "macos,linux" })]), [1]);
    assert.deepEqual(ids([row({ target_platforms: "windows" })]), []);
    assert.deepEqual(
      ids([row({ target_platforms: "windows" })], { platform: "windows" }),
      [1],
    );
  });

  it("hides platform-scoped notices from clients that report no platform", () => {
    assert.deepEqual(ids([row({ target_platforms: "macos" })], { platform: "" }), []);
    assert.deepEqual(ids([row()], { platform: "" }), [1]);
  });
});

describe("selectAnnouncements — ordering and capping", () => {
  it("sorts by priority desc, then by newest start, then by id", () => {
    const rows = [
      row({ id: 10, priority: 1 }),
      row({ id: 20, priority: 9 }),
      row({ id: 30, priority: 5, starts_at: NOW - 100 }),
      row({ id: 40, priority: 5, starts_at: NOW - 10 }),
    ];
    assert.deepEqual(ids(rows), [20, 40, 30, 10]);
  });

  it("breaks a full tie deterministically by numeric id", () => {
    const rows = [row({ id: 3 }), row({ id: 1 }), row({ id: 2 })];
    assert.deepEqual(ids(rows), [1, 2, 3]);
  });

  it("never returns more than ANNOUNCEMENT_MAX, keeping the highest priorities", () => {
    const rows = Array.from({ length: 12 }, (_, i) =>
      row({ id: i + 1, priority: i }),
    );
    const got = ids(rows);
    assert.equal(got.length, ANNOUNCEMENT_MAX);
    assert.deepEqual(got, [12, 11, 10, 9, 8]);
  });
});

describe("selectAnnouncements — payload hygiene", () => {
  it("ships only the fields the client renders", () => {
    const endsAt = NOW + 3600;
    const [a] = pick([
      row({ tag: " 限时 ", url: "https://example.com/n", level: "warning", ends_at: endsAt }),
    ]);
    assert.deepEqual(a, {
      id: 1,
      body: "维护通知",
      tag: "限时",
      url: "https://example.com/n",
      level: "warning",
      priority: 0,
      dimmed: false,
      ends_at: endsAt,
    });
    // 定向字段绝不能回给客户端。
    assert.equal("target_platforms" in a, false);
    assert.equal("min_version" in a, false);
  });

  it("projects dimmed from the cloud flag, defaulting to false", () => {
    assert.equal(pick([row()])[0].dimmed, false);
    assert.equal(pick([row({ dimmed: 1 })])[0].dimmed, true);
    assert.equal(pick([row({ dimmed: 0, url: null })])[0].dimmed, false);
    // Missing column (older rows before the migration) must not gray by accident.
    const without = { ...row() };
    delete without.dimmed;
    assert.equal(pick([without])[0].dimmed, false);
  });

  it("drops non-http(s) urls so the client never opens them", () => {
    for (const bad of ["javascript:alert(1)", "file:///etc/passwd", "ftp://x/y", "example.com"]) {
      assert.equal(pick([row({ url: bad })])[0].url, null, bad);
    }
    assert.equal(pick([row({ url: "HTTPS://Example.com/a" })])[0].url, "HTTPS://Example.com/a");
  });

  it("falls back to the info level for unknown or missing levels", () => {
    assert.equal(pick([row({ level: "URGENT" })])[0].level, "info");
    assert.equal(pick([row({ level: null })])[0].level, "info");
    assert.equal(pick([row({ level: "critical" })])[0].level, "critical");
  });

  it("normalizes empty optional fields to null", () => {
    const [a] = pick([row({ tag: "   ", url: "", ends_at: "" })]);
    assert.equal(a.tag, null);
    assert.equal(a.url, null);
    assert.equal(a.ends_at, null);
  });

  it("skips rows with missing/invalid id or no body instead of rendering a blank line", () => {
    assert.deepEqual(ids([row({ id: 0 }), row({ id: 2 })]), [2]);
    assert.deepEqual(ids([row({ id: -1 })]), []);
    assert.deepEqual(ids([row({ id: null })]), []);
    assert.deepEqual(ids([row({ id: "nope" })]), []);
    assert.deepEqual(ids([row({ body: "   " })]), []);
    assert.deepEqual(ids([row({ body: null })]), []);
  });

  it("survives malformed input without throwing", () => {
    assert.deepEqual(selectAnnouncements(null, { now: NOW }), []);
    assert.deepEqual(selectAnnouncements(undefined, { now: NOW }), []);
    assert.deepEqual(ids([null, undefined, "nope", 42, row({ id: 9 })]), [9]);
    // 完全没有上下文时不抛异常：没有时间窗的公告仍然算命中，有时间窗的按 now=0 判。
    assert.deepEqual(selectAnnouncements([row()], undefined).map((a) => a.id), [1]);
    assert.deepEqual(selectAnnouncements([row({ starts_at: NOW })], undefined), []);
  });
});

describe("ANNOUNCEMENTS_SELECT_SQL", () => {
  it("drops expired rows at the D1 layer (enabled + ends_at)", () => {
    assert.match(ANNOUNCEMENTS_SELECT_SQL, /enabled\s*=\s*1/);
    assert.match(ANNOUNCEMENTS_SELECT_SQL, /ends_at\s+IS\s+NULL\s+OR\s+ends_at\s*>\s*\?/i);
    // starts_at stays in JS so not-yet-live notices can enter the 5-minute cache.
    assert.doesNotMatch(ANNOUNCEMENTS_SELECT_SQL, /starts_at\s*(IS\s+NULL|<=)/i);
    // bind order: now, LIMIT
    assert.equal((ANNOUNCEMENTS_SELECT_SQL.match(/\?/g) || []).length, 2);
    assert.match(ANNOUNCEMENTS_SELECT_SQL, /\bSELECT\s+id\b/i);
  });
});
