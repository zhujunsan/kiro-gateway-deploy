-- 公告栏运维模板：复制需要的段落，改好后执行
--   wrangler d1 execute kiro-telemetry --remote --command "<SQL>"
-- 或者把改好的语句存成文件后
--   wrangler d1 execute kiro-telemetry --remote --file=./my-announcement.sql
--
-- 本文件只是模板，不需要（也不应该）整份执行。
--
-- 几条要记住的规则：
--   * id 是自增主键：INSERT 不写 id；改/下架用 WHERE id = N。
--   * 发新公告后用 SELECT 回看拿到 id，再改文案或下架。
--   * body 是菜单行文字，客户端会把换行折成空格并截断到 120 字。长内容放 url。
--   * dimmed=1 菜单行置灰；与有无 url 无关。无链接默认仍是正常色。
--   * 时间一律 Unix 秒（UTC）：starts_at 含、ends_at 不含。
--   * 改完最多 5 分钟生效（Worker 边缘缓存 TTL）。要立刻验证就等一会儿再试，
--     或 bump worker/src/announcements.js 里的 ANNOUNCEMENT_CACHE_GEN 后 redeploy。
--   * 客户端每小时才拉一次，所以用户实际看到的延迟最多约 1 小时 + 5 分钟。

-- ---------------------------------------------------------------------------
-- 1. 最简单的一条：所有人可见、长期有效（默认不置灰；无 url 时点击无反应）
-- ---------------------------------------------------------------------------
INSERT INTO announcements (body)
VALUES ('欢迎使用 Kiro Gateway，有问题请联系管理员');

-- ---------------------------------------------------------------------------
-- 1b. 置灰只读提示（无链接 + dimmed=1）
-- ---------------------------------------------------------------------------
INSERT INTO announcements (body, tag, dimmed)
VALUES ('本行仅展示、不可点击', '只读', 1);

-- ---------------------------------------------------------------------------
-- 1c. 有链接但仍置灰（例如想强调「先别点」、或过期预告）
-- ---------------------------------------------------------------------------
INSERT INTO announcements (body, tag, url, dimmed, level)
VALUES (
  '维护已结束，详情可点（示例：链接与置灰可并存）',
  '已结束',
  'https://example.com/notice',
  1,
  'info'
);

-- ---------------------------------------------------------------------------
-- 2. 带链接和右侧灰色小字，自动上下架
-- ---------------------------------------------------------------------------
INSERT INTO announcements (body, tag, url, level, priority, starts_at, ends_at)
VALUES (
  '8/10 02:00-04:00 计划维护，期间可能短暂断连',
  '点击查看',
  'https://example.com/notice/2026-08-maintenance',
  'warning',
  100,
  CAST(strftime('%s', '2026-08-03 00:00:00') AS INTEGER),
  CAST(strftime('%s', '2026-08-10 04:00:00') AS INTEGER)
);

-- ---------------------------------------------------------------------------
-- 3. 只给某个版本区间看（闭区间）——典型场景：催促老版本升级
--    托盘客户端用 User-Agent（KiroGatewayTray/x.y.z）上报版本；
--    设了版本区间后，请求里解析不到版本号的调用方（比如没带 UA 的 curl）看不到。
-- ---------------------------------------------------------------------------
INSERT INTO announcements (body, url, level, max_version)
VALUES (
  '你的版本已停止维护，请尽快升级',
  'https://github.com/zhujunsan/kiro-gateway-deploy/releases/latest',
  'critical',
  '0.4.21'
);

-- ---------------------------------------------------------------------------
-- 4. 只给某个平台看
-- ---------------------------------------------------------------------------
INSERT INTO announcements (body, target_platforms)
VALUES ('Windows 用户请把安装目录加入杀软白名单', 'windows');

-- ---------------------------------------------------------------------------
-- 5. 日常操作（把 N 换成 SELECT 回看拿到的 id）
-- ---------------------------------------------------------------------------

-- 改文案
UPDATE announcements
   SET body = '新的文案', updated_at = CAST(strftime('%s', 'now') AS INTEGER)
 WHERE id = N;

-- 立刻下架（比改 ends_at 更直接）
UPDATE announcements
   SET enabled = 0, updated_at = CAST(strftime('%s', 'now') AS INTEGER)
 WHERE id = N;

-- 重新上架
UPDATE announcements
   SET enabled = 1, updated_at = CAST(strftime('%s', 'now') AS INTEGER)
 WHERE id = N;

-- 彻底删除
DELETE FROM announcements WHERE id = N;

-- 看当前哪些公告真的在生效窗口内（不含版本/平台定向，那部分在 Worker 里算）
SELECT id, enabled, priority, dimmed,
       datetime(starts_at, 'unixepoch') AS starts,
       datetime(ends_at,   'unixepoch') AS ends,
       body
  FROM announcements
 WHERE enabled = 1
   AND (starts_at IS NULL OR starts_at <= strftime('%s', 'now'))
   AND (ends_at   IS NULL OR ends_at   >  strftime('%s', 'now'))
 ORDER BY priority DESC, id;

-- 查所有公告（含已下架的）；发新公告后用这个拿 id
SELECT id, enabled, priority, body FROM announcements ORDER BY id DESC LIMIT 10;
