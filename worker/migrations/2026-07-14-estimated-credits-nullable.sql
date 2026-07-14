-- Make Credit columns nullable: NULL = unknown / not reported; 0 = measured zero.
-- The previous migration used NOT NULL DEFAULT 0, which conflated "missing" with zero.
-- SQLite/D1 cannot ALTER COLUMN nullability — drop and re-add (no real Credit data yet).
--
-- Deploy:
--   cd worker
--   wrangler d1 execute kiro-telemetry --remote --file=./migrations/2026-07-14-estimated-credits-nullable.sql

ALTER TABLE usage_rollup DROP COLUMN estimated_credits;
ALTER TABLE usage_rollup DROP COLUMN credit_estimate_segments;
ALTER TABLE usage_rollup DROP COLUMN credit_estimate_missing_segments;
ALTER TABLE usage_rollup ADD COLUMN estimated_credits REAL;
ALTER TABLE usage_rollup ADD COLUMN credit_estimate_segments INTEGER;
ALTER TABLE usage_rollup ADD COLUMN credit_estimate_missing_segments INTEGER;

ALTER TABLE usage_daily DROP COLUMN estimated_credits;
ALTER TABLE usage_daily DROP COLUMN credit_estimate_segments;
ALTER TABLE usage_daily DROP COLUMN credit_estimate_missing_segments;
ALTER TABLE usage_daily ADD COLUMN estimated_credits REAL;
ALTER TABLE usage_daily ADD COLUMN credit_estimate_segments INTEGER;
ALTER TABLE usage_daily ADD COLUMN credit_estimate_missing_segments INTEGER;
