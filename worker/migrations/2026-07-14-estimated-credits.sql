-- Add estimated Credit columns to existing D1 tables (nullable).
-- NULL = unknown / old client omitted the field; 0 = measured zero Credit.
-- Idempotent on fresh DBs that already have these columns via schema.sql:
-- re-running ALTER on SQLite/D1 will fail if the column exists; apply once per
-- environment. Safe to skip when bootstrapping from the updated schema.sql.
--
-- If you already applied an earlier NOT NULL DEFAULT 0 variant, use instead:
--   ./migrations/2026-07-14-estimated-credits-nullable.sql
--
-- Deploy:
--   cd worker
--   wrangler d1 execute kiro-telemetry --remote --file=./migrations/2026-07-14-estimated-credits.sql

ALTER TABLE usage_rollup ADD COLUMN estimated_credits REAL;
ALTER TABLE usage_rollup ADD COLUMN credit_estimate_segments INTEGER;
ALTER TABLE usage_rollup ADD COLUMN credit_estimate_missing_segments INTEGER;

ALTER TABLE usage_daily ADD COLUMN estimated_credits REAL;
ALTER TABLE usage_daily ADD COLUMN credit_estimate_segments INTEGER;
ALTER TABLE usage_daily ADD COLUMN credit_estimate_missing_segments INTEGER;
