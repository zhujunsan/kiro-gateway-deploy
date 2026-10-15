// Pure helpers for POST /client-config.
//
// The D1 row `audit` holds JSON: {"mode":"off"|"best_effort","usernames":[],"rotate_seconds":600}.
// A non-empty usernames list restricts best_effort to those slugs (canary).
// Anything unreadable fails closed to off.

export const CLIENT_CONFIG_CACHE_TTL = 300;
export const CLIENT_CONFIG_CACHE_GEN = 2;
export const CLIENT_CONFIG_SQL = "SELECT key, value FROM client_config";

const DEFAULT_ROTATE = 600;

function off(rotate) {
  return { mode: "off", rotate_seconds: rotate };
}

/**
 * Decide the audit block for one username.
 *
 * @param {string|object|null} raw  the `audit` row's value, JSON text or object
 * @param {string} username
 * @returns {{mode: "off"|"best_effort", rotate_seconds: number}}
 */
export function auditConfigFor(raw, username) {
  let parsed = raw;
  if (typeof raw === "string") {
    try {
      parsed = JSON.parse(raw);
    } catch {
      return off(DEFAULT_ROTATE);
    }
  }
  if (!parsed || typeof parsed !== "object") return off(DEFAULT_ROTATE);
  const rotate = Number.isInteger(parsed.rotate_seconds) && parsed.rotate_seconds > 0
    ? parsed.rotate_seconds
    : DEFAULT_ROTATE;
  const requested = parsed.mode === "best_effort" ? "best_effort" : "off";
  const names = Array.isArray(parsed.usernames)
    ? parsed.usernames.filter((n) => typeof n === "string")
    : [];
  if (requested === "best_effort" && names.length > 0 && !names.includes(username)) {
    return off(rotate);
  }
  return { mode: requested, rotate_seconds: rotate };
}

// d-<10 hex>.<uuid>. The directory is this id so a bucket listing shows the
// person. The machine slug is still accepted when /usage did not return an id.
const KIRO_USER_ID = "d-[0-9a-f]{10}\\.[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}";

/** Object key a client is allowed to upload for itself. */
export function auditKeyPattern(username) {
  const slug = username.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  return new RegExp(
    "^audit/v1/\\d{2}-\\d{2}-\\d{2}/(?:" + slug + "|" + KIRO_USER_ID + ")" +
      "/[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}\\.(req|blob)\\.jsonl\\.zst$",
  );
}

/**
 * Validate an upload-url request's keys.
 *
 * @returns {string|null} an error message, or null when every key is allowed
 */
export function validateAuditKeys(username, keys) {
  if (!Array.isArray(keys) || keys.length === 0) return "keys required";
  if (keys.length > 10) return "too many keys";
  const pattern = auditKeyPattern(username);
  for (const key of keys) {
    if (typeof key !== "string" || key.includes("..") || !pattern.test(key)) {
      return "invalid key";
    }
  }
  return null;
}
