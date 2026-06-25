-- D1 schema for usage telemetry (see docs/2026-06-25-telemetry-design.md §7).
-- 客户端本地不建库；下面两张表是 Worker 侧（D1）的结构。

CREATE TABLE IF NOT EXISTS usage_rollup (
  id                  INTEGER PRIMARY KEY AUTOINCREMENT,
  bucket_start        INTEGER NOT NULL,   -- Unix 秒，对齐到 bucket_seconds
  bucket_seconds      INTEGER NOT NULL,   -- 桶宽，默认 600
  username            TEXT    NOT NULL,   -- 匿名哈希
  model               TEXT    NOT NULL,
  app_version         TEXT    NOT NULL,
  requests            INTEGER NOT NULL DEFAULT 0,
  successes           INTEGER NOT NULL DEFAULT 0,
  errors              INTEGER NOT NULL DEFAULT 0,
  prompt_tokens_sum   INTEGER NOT NULL DEFAULT 0,
  completion_tokens_sum INTEGER NOT NULL DEFAULT 0,
  total_tokens_sum    INTEGER NOT NULL DEFAULT 0,
  request_bytes_sum   INTEGER NOT NULL DEFAULT 0,
  response_bytes_sum  INTEGER NOT NULL DEFAULT 0,
  credits_used_sum    REAL,               -- nullable，随缘
  schema_version      INTEGER NOT NULL DEFAULT 1,
  received_at         INTEGER NOT NULL,   -- Worker 落库时间
  UNIQUE (bucket_start, bucket_seconds, username, model, app_version)
);

CREATE INDEX IF NOT EXISTS idx_rollup_bucket   ON usage_rollup (bucket_start);
CREATE INDEX IF NOT EXISTS idx_rollup_user     ON usage_rollup (username, bucket_start);

-- 日聚合表：Worker 定时把 usage_rollup 卷成"天 × user × model"，供看板默认查询，
-- 把单次扫描行数从"窗口内全部 10 分钟桶"压到"窗口天数 × 用户 × 模型"级（见第十二节）。
CREATE TABLE IF NOT EXISTS usage_daily (
  day                 TEXT    NOT NULL,   -- YYYY-MM-DD（UTC）
  username            TEXT    NOT NULL,
  model               TEXT    NOT NULL,
  requests            INTEGER NOT NULL DEFAULT 0,
  successes           INTEGER NOT NULL DEFAULT 0,
  errors              INTEGER NOT NULL DEFAULT 0,
  prompt_tokens_sum   INTEGER NOT NULL DEFAULT 0,
  completion_tokens_sum INTEGER NOT NULL DEFAULT 0,
  total_tokens_sum    INTEGER NOT NULL DEFAULT 0,
  request_bytes_sum   INTEGER NOT NULL DEFAULT 0,
  response_bytes_sum  INTEGER NOT NULL DEFAULT 0,
  credits_used_sum    REAL,
  PRIMARY KEY (day, username, model)
);

CREATE INDEX IF NOT EXISTS idx_daily_day ON usage_daily (day);
