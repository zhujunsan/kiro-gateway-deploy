-- D1 schema for usage telemetry (see docs/2026-06-25-telemetry-design.md §7)
-- 以及托盘公告栏。客户端本地不建库；下面几张表都是 Worker 侧（D1）的结构。

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
  -- Latency / throughput (old clients omit → stay 0).
  ttft_ms_sum         INTEGER NOT NULL DEFAULT 0,
  ttft_count          INTEGER NOT NULL DEFAULT 0,
  generation_ms_sum   INTEGER NOT NULL DEFAULT 0,
  generation_count    INTEGER NOT NULL DEFAULT 0,
  generation_completion_tokens_sum INTEGER NOT NULL DEFAULT 0,
  -- NULL = not reported (old client / no estimate); 0 = measured zero.
  estimated_credits   REAL,
  credit_estimate_segments INTEGER,
  credit_estimate_missing_segments INTEGER,
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
  ttft_ms_sum         INTEGER NOT NULL DEFAULT 0,
  ttft_count          INTEGER NOT NULL DEFAULT 0,
  generation_ms_sum   INTEGER NOT NULL DEFAULT 0,
  generation_count    INTEGER NOT NULL DEFAULT 0,
  generation_completion_tokens_sum INTEGER NOT NULL DEFAULT 0,
  estimated_credits   REAL,
  credit_estimate_segments INTEGER,
  credit_estimate_missing_segments INTEGER,
  PRIMARY KEY (day, username, model)
);

CREATE INDEX IF NOT EXISTS idx_daily_day ON usage_daily (day);

-- 公告栏：托盘菜单顶部展示的运营公告。字段含义与定向语义见
-- migrations/2026-08-03-announcements.sql（两份 DDL 保持一致），
-- 写公告的模板见 announcements.example.sql。
CREATE TABLE IF NOT EXISTS announcements (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,  -- 自增主键；INSERT 不写 id
  body              TEXT    NOT NULL,      -- 正文（单行菜单文字）
  tag               TEXT,                  -- 行尾灰色小字（可选）
  url               TEXT,                  -- 点击跳转；为空则点击无反应（不因此置灰）
  level             TEXT    NOT NULL DEFAULT 'info',  -- info | warning | critical
  priority          INTEGER NOT NULL DEFAULT 0,       -- 排序权重，大的在前
  dimmed            INTEGER NOT NULL DEFAULT 0,       -- 1 = 菜单行置灰；与有无 url 无关
  enabled           INTEGER NOT NULL DEFAULT 1,       -- 手动总开关
  starts_at         INTEGER,               -- Unix 秒，含；NULL = 立即生效
  ends_at           INTEGER,               -- Unix 秒，不含；NULL = 永不过期
  min_version       TEXT,                  -- 版本闭区间下界
  max_version       TEXT,                  -- 版本闭区间上界
  target_platforms  TEXT,                  -- macos,windows,linux；空 = 全平台
  created_at        INTEGER NOT NULL DEFAULT (CAST(strftime('%s', 'now') AS INTEGER)),
  updated_at        INTEGER NOT NULL DEFAULT (CAST(strftime('%s', 'now') AS INTEGER))
);

CREATE INDEX IF NOT EXISTS idx_announcements_enabled
  ON announcements (enabled, priority DESC);
