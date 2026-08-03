// worker/src/announcements.js
// 公告栏定向过滤。
//
// 这里只放纯函数：没有 I/O、没有 Cloudflare 运行时依赖，输入一批 D1 行 + 一个
// 客户端上下文，输出该客户端应当看到的公告。鉴权、D1 读取和结果缓存留在
// index.js —— 这样"给谁看什么"这套规则可以脱离 Worker 环境直接单测。
//
// 匹配一条公告需要同时满足（任一不满足即不展示）：
//   1. enabled = 1
//   2. 当前时间落在 [starts_at, ends_at) 内（两侧均可为 NULL 表示不限）
//   3. 客户端版本落在 [min_version, max_version] 闭区间内
//   4. 客户端平台命中 target_platforms（为空则不限平台）
//
// 全部规则都是"fail-closed"：配置写错（比如 min_version 填了非法字符串）时
// 宁可不展示，也不要广播给错误的人群 —— 少一条公告是小事，发错人是事故。

/** 单次响应最多下发的公告条数（客户端菜单也按这个数预留槽位）。 */
export const ANNOUNCEMENT_MAX = 5;

/** 单次 D1 查询最多取回的行数，防止表被写爆时全表扫描。 */
export const ANNOUNCEMENT_ROW_LIMIT = 100;

/** D1 行的边缘缓存 TTL（秒）。读 D1 的次数因此与在线人数解耦。 */
export const ANNOUNCEMENT_CACHE_TTL = 300;

/** 缓存世代：改公告内容 / 表结构后想立刻生效时 +1，避免干等 TTL。 */
export const ANNOUNCEMENT_CACHE_GEN = 8;

const LEVELS = new Set(["info", "warning", "critical"]);
const DEFAULT_LEVEL = "info";

/**
 * 只取 enabled 且尚未过期的行（ends_at IS NULL 或 ends_at > ?）。
 * starts_at / 版本 / 平台定向仍放在 JS 里做：未来才生效的公告要能提前进缓存，
 * 这样缓存窗口内到点就能上架；已过期的历史行则直接在 D1 丢掉，少搬字节。
 * 绑定顺序：now（Unix 秒）, LIMIT。
 */
export const ANNOUNCEMENTS_SELECT_SQL = `
SELECT id, body, tag, url, level, priority, dimmed, enabled,
       starts_at, ends_at, min_version, max_version, target_platforms
FROM announcements
WHERE enabled = 1
  AND (ends_at IS NULL OR ends_at > ?)
ORDER BY priority DESC
LIMIT ?`;

function toInt(value, fallback = 0) {
  const n = parseInt(value, 10);
  return Number.isFinite(n) ? n : fallback;
}

/** 可空整数：NULL / 空串 / 非法值统一成 null，0 保持 0。 */
function toNullableInt(value) {
  if (value === undefined || value === null || value === "") return null;
  const n = parseInt(value, 10);
  return Number.isFinite(n) ? n : null;
}

/** 去空白后的非空字符串，否则 null。 */
function cleanText(value) {
  if (typeof value !== "string") return null;
  const s = value.trim();
  return s ? s : null;
}

/**
 * 只放行 http(s) 链接。客户端拿到 url 会直接丢给系统浏览器打开，所以
 * file:// / javascript: 之类的 scheme 必须在下发前就掐掉。
 */
function safeUrl(value) {
  const s = cleanText(value);
  if (!s) return null;
  return /^https?:\/\//i.test(s) ? s : null;
}

/**
 * 解析版本号为 [major, minor, patch]，无法识别时返回 null。
 * 与客户端 updates.py 的解析口径一致：从字符串里抓第一段 x.y[.z]，
 * 所以 "v0.4.22" / "0.4.22-beta" / "0.4" 都能认。
 */
export function parseVersion(value) {
  const m = /(\d+)\.(\d+)(?:\.(\d+))?/.exec(String(value ?? ""));
  if (!m) return null;
  return [toInt(m[1]), toInt(m[2]), toInt(m[3])];
}

/** 逐段比较两个 parseVersion 结果，返回 -1 / 0 / 1。 */
export function compareVersions(a, b) {
  for (let i = 0; i < 3; i++) {
    if (a[i] !== b[i]) return a[i] < b[i] ? -1 : 1;
  }
  return 0;
}

/**
 * 判断客户端版本是否落在闭区间 [minVersion, maxVersion] 内。
 * 两端都没设 → 恒为 true（不限版本）。
 * 设了区间但拿不到客户端版本，或者区间本身写得不合法 → false。
 *
 * 正常托盘客户端通过 User-Agent（KiroGatewayTray/x.y.z）上报版本；
 * 拿不到版本通常只发生在手工 curl / 非托盘调用方没带 UA 时。
 */
export function versionInRange(version, minVersion, maxVersion) {
  const min = cleanText(minVersion);
  const max = cleanText(maxVersion);
  if (!min && !max) return true;

  const current = parseVersion(version);
  if (!current) return false;

  if (min) {
    const lo = parseVersion(min);
    if (!lo || compareVersions(current, lo) < 0) return false;
  }
  if (max) {
    const hi = parseVersion(max);
    if (!hi || compareVersions(current, hi) > 0) return false;
  }
  return true;
}

/**
 * 从 User-Agent 解析托盘身份。
 * 期望形如：``KiroGatewayTray/0.4.22 (macos)``（平台括号可选）。
 *
 * @param {string|null|undefined} ua
 * @returns {{appVersion: string, platform: string}}
 */
export function parseTrayUserAgent(ua) {
  const s = typeof ua === "string" ? ua : "";
  const m = /KiroGatewayTray\/([^\s;/]+)(?:\s*\(([^)]*)\))?/i.exec(s);
  if (!m) return { appVersion: "", platform: "" };
  const appVersion = cleanText(m[1]) || "";
  // 括号里可能还有其它片段；只认 macos/windows/linux 其中一个 token。
  const platformToken = String(m[2] || "")
    .split(/[\s,;]+/)
    .map((t) => t.trim().toLowerCase())
    .find((t) => t === "macos" || t === "windows" || t === "linux");
  return { appVersion, platform: platformToken || "" };
}

/**
 * 合并请求头 UA 与 body 兜底字段，得到定向用的客户端上下文。
 * UA 优先；body.app_version / body.platform 仅作 curl 调试兜底。
 *
 * @param {{headers?: Headers|Map|object, body?: object, now?: number}} input
 */
export function clientContextFromRequest(input) {
  const headers = (input && input.headers) || {};
  const body = (input && input.body) || {};
  const ua =
    typeof headers.get === "function"
      ? headers.get("User-Agent") || headers.get("user-agent") || ""
      : headers["User-Agent"] || headers["user-agent"] || "";
  const fromUa = parseTrayUserAgent(ua);
  const bodyVersion = typeof body.app_version === "string" ? body.app_version : "";
  const bodyPlatform = typeof body.platform === "string" ? body.platform : "";
  return {
    appVersion: fromUa.appVersion || bodyVersion || "",
    platform: fromUa.platform || bodyPlatform || "",
    now: toInt(input && input.now, Math.floor(Date.now() / 1000)),
  };
}

/** 把 "a, b ,C" 这类逗号分隔配置解析成去空、小写的数组。 */
export function parseCsvList(value) {
  if (typeof value !== "string") return [];
  return value
    .split(",")
    .map((s) => s.trim().toLowerCase())
    .filter(Boolean);
}

function inTimeWindow(row, now) {
  const start = toNullableInt(row.starts_at);
  const end = toNullableInt(row.ends_at);
  if (start !== null && now < start) return false;
  if (end !== null && now >= end) return false;
  return true;
}

/** 正整数主键：缺失、0、负数、非数字一律视为脏行。 */
function toPositiveId(value) {
  const n = toInt(value, 0);
  return n > 0 ? n : 0;
}

function matches(row, ctx) {
  if (!row || typeof row !== "object") return false;

  // 脏行防御：没有合法 id 或正文的行渲染出来只会是一条空菜单项。
  const id = toPositiveId(row.id);
  const body = cleanText(row.body);
  if (!id || !body) return false;

  if (toInt(row.enabled, 0) !== 1) return false;
  if (!inTimeWindow(row, ctx.now)) return false;
  if (!versionInRange(ctx.appVersion, row.min_version, row.max_version)) return false;

  // 平台定向：设了名单却拿不到客户端平台时同样不展示（fail-closed）。
  const platforms = parseCsvList(row.target_platforms);
  if (platforms.length && (!ctx.platform || !platforms.includes(ctx.platform))) {
    return false;
  }

  return true;
}

/** priority 大的在前；同权重时新上线的在前；再同则按 id 数值稳定排序。 */
function compareForDisplay(a, b) {
  const byPriority = toInt(b.priority, 0) - toInt(a.priority, 0);
  if (byPriority !== 0) return byPriority;
  const byStart = toInt(b.starts_at, 0) - toInt(a.starts_at, 0);
  if (byStart !== 0) return byStart;
  return toPositiveId(a.id) - toPositiveId(b.id);
}

/** 只下发客户端渲染真正需要的字段（ends_at 给客户端本地剔除过期缓存用）。 */
function project(row) {
  return {
    id: toPositiveId(row.id),
    body: cleanText(row.body),
    tag: cleanText(row.tag),
    url: safeUrl(row.url),
    level: LEVELS.has(row.level) ? row.level : DEFAULT_LEVEL,
    priority: toInt(row.priority, 0),
    // 置灰由云端显式配置，不跟有无 url 绑定：无链接也可以是正常色，有链接也可以灰。
    dimmed: toInt(row.dimmed, 0) === 1,
    ends_at: toNullableInt(row.ends_at),
  };
}

/**
 * 从 D1 行里挑出该客户端应当看到的公告，按展示顺序排好并截断到 ANNOUNCEMENT_MAX。
 *
 * @param {Array<object>} rows D1 查回的原始行
 * @param {{appVersion?: string, platform?: string, now?: number}} ctx
 *        客户端上下文；now 是 Unix 秒
 * @returns {Array<object>} 可直接 JSON 序列化下发的公告数组
 */
export function selectAnnouncements(rows, ctx) {
  const context = {
    appVersion: (ctx && ctx.appVersion) || "",
    platform: String((ctx && ctx.platform) || "").toLowerCase(),
    now: toInt(ctx && ctx.now, 0),
  };
  return (Array.isArray(rows) ? rows : [])
    .filter((row) => matches(row, context))
    .sort(compareForDisplay)
    .slice(0, ANNOUNCEMENT_MAX)
    .map(project);
}
