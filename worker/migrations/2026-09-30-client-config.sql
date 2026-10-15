-- Remote client switches. `audit` is the first key; later flags are more rows.
-- value is JSON. audit: {"mode":"off"|"best_effort","usernames":[],"rotate_seconds":600}
-- A non-empty usernames list is a canary: only those slugs get best_effort.

CREATE TABLE IF NOT EXISTS client_config (
  key        TEXT PRIMARY KEY,
  value      TEXT NOT NULL,
  updated_at INTEGER NOT NULL DEFAULT (CAST(strftime('%s', 'now') AS INTEGER))
);

INSERT INTO client_config (key, value)
VALUES ('audit', '{"mode":"off","usernames":[],"rotate_seconds":600}')
ON CONFLICT(key) DO NOTHING;
