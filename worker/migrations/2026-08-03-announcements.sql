-- 公告栏（announcements）：托盘菜单顶部、"新版提醒"下方展示的运营公告。
--
-- 已有库（尚无此表）执行：
--   wrangler d1 execute kiro-telemetry --remote --file=./migrations/2026-08-03-announcements.sql
-- 新建库由 schema.sql 一并建出（DDL 与本文一致）。
--
-- 本文件即最终形态；不要再叠 ALTER 中间迁移。写公告见 announcements.example.sql。
--
-- 线上表若仍是旧的 key TEXT 主键，先 DROP 再按本文重建（当前生产为空表，可直接重建）：
--   wrangler d1 execute kiro-telemetry --remote --command "DROP TABLE IF EXISTS announcements;"
--   wrangler d1 execute kiro-telemetry --remote --file=./migrations/2026-08-03-announcements.sql

CREATE TABLE IF NOT EXISTS announcements (
  -- 自增整数主键。INSERT 不写 id；改/下架用 WHERE id = ?。
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  -- 正文：直接作为菜单行文字，控制在一行内（客户端会把换行折成空格并截断）。
  -- 需要长内容请放到 url 里。
  body              TEXT    NOT NULL,
  -- 可选的行尾灰色小字（macOS 右对齐），如 '限时' / '08-10 截止'。
  tag               TEXT,
  -- 可选跳转链接。为空时点击无反应；是否置灰由 dimmed 决定，与有无 url 无关。
  url               TEXT,
  -- info | warning | critical —— 只影响菜单行 emoji（📢 / ⚠️ / 🚨）。
  level             TEXT    NOT NULL DEFAULT 'info',
  -- 排序权重，大的排前面；同权重时新上线的（starts_at 大）在前。
  priority          INTEGER NOT NULL DEFAULT 0,
  -- 1 = 菜单行置灰（enabled=False）。与有无 url 无关，由运维显式配置。
  dimmed            INTEGER NOT NULL DEFAULT 0,
  -- 手动总开关。置 0 等于立刻下架，不用改时间。
  enabled           INTEGER NOT NULL DEFAULT 1,
  -- 生效起止（Unix 秒，UTC）。starts_at 含、ends_at 不含；NULL 表示该侧不限。
  starts_at         INTEGER,
  ends_at           INTEGER,
  -- 版本区间（闭区间，形如 '0.4.20'）。设了区间但客户端没上报版本时不展示。
  min_version       TEXT,
  max_version       TEXT,
  -- 平台定向：逗号分隔，取值 macos / windows / linux。为空 = 全平台。
  target_platforms  TEXT,
  created_at        INTEGER NOT NULL DEFAULT (CAST(strftime('%s', 'now') AS INTEGER)),
  updated_at        INTEGER NOT NULL DEFAULT (CAST(strftime('%s', 'now') AS INTEGER))
);

-- Worker 查 enabled = 1 且未过期（ends_at IS NULL OR ends_at > now）的行，按 priority 取前 N；
-- 版本/平台定向在 JS 侧做。
CREATE INDEX IF NOT EXISTS idx_announcements_enabled
  ON announcements (enabled, priority DESC);
