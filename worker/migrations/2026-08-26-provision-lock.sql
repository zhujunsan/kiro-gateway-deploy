-- per-username 签发租约：同一 username 的并发 /provision 互斥。
--
-- 已有库执行：
--   wrangler d1 execute kiro-telemetry --remote --file=./migrations/2026-08-26-provision-lock.sql
-- 新建库由 schema.sql 一并建出（DDL 与本文一致）。
--
-- 本文件即最终形态；不要再叠 ALTER 中间迁移。
--
-- 抢租约靠单条 INSERT ... ON CONFLICT DO UPDATE ... WHERE lease_until <= now
-- 的 meta.changes 判定（D1 单语句原子）。抢不到返回 409，不得删对方资源。

CREATE TABLE IF NOT EXISTS provision_lock (
  -- 隧道身份 slug（kg-<username>.example.com 里的 username）
  username    TEXT    PRIMARY KEY,
  -- Unix 秒。> now 表示租约有效；0 或已过期则可被接管。
  lease_until INTEGER NOT NULL,
  -- 每次成功抢到租约自增。删除 tunnel/DNS 前必须与持有者 generation 一致。
  generation  INTEGER NOT NULL DEFAULT 0,
  -- 当前持有者标识；释放时仅当 holder 匹配才清租约。
  holder      TEXT,
  -- 最近一次抢到/释放的 Unix 秒。
  updated_at  INTEGER NOT NULL
);
