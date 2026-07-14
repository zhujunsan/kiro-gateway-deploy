-- Add TTFT / generation throughput columns to existing D1 tables.
-- Old clients omit these fields; Worker stores 0 (avg queries use NULLIF count).
-- Idempotent on fresh DBs that already have these columns via schema.sql:
-- re-running ALTER on SQLite/D1 will fail if the column exists; apply once per
-- environment. Safe to skip when bootstrapping from the updated schema.sql.
--
-- Deploy:
--   cd worker
--   wrangler d1 execute kiro-telemetry --remote --file=./migrations/2026-07-14-ttft-generation.sql

ALTER TABLE usage_rollup ADD COLUMN ttft_ms_sum INTEGER NOT NULL DEFAULT 0;
ALTER TABLE usage_rollup ADD COLUMN ttft_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE usage_rollup ADD COLUMN generation_ms_sum INTEGER NOT NULL DEFAULT 0;
ALTER TABLE usage_rollup ADD COLUMN generation_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE usage_rollup ADD COLUMN generation_completion_tokens_sum INTEGER NOT NULL DEFAULT 0;

ALTER TABLE usage_daily ADD COLUMN ttft_ms_sum INTEGER NOT NULL DEFAULT 0;
ALTER TABLE usage_daily ADD COLUMN ttft_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE usage_daily ADD COLUMN generation_ms_sum INTEGER NOT NULL DEFAULT 0;
ALTER TABLE usage_daily ADD COLUMN generation_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE usage_daily ADD COLUMN generation_completion_tokens_sum INTEGER NOT NULL DEFAULT 0;
