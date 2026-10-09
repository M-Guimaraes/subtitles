-- Schema version 3. Persist the public target language on each job.
-- Different targets of the same file are distinct jobs (hash + this column).
-- Existing rows default to the historical public identifier pt-BR.

ALTER TABLE jobs ADD COLUMN target_language TEXT;

UPDATE jobs SET target_language = 'pt-BR' WHERE target_language IS NULL;

INSERT INTO schema_migrations (version, applied_at) VALUES (3, datetime('now'));
