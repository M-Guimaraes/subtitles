-- Schema version 2. Distinguishes preview/manual jobs from full library jobs.
-- Existing rows keep their ids; preview_seconds IS NOT NULL becomes preview.
-- The old unique identity mixed those scopes and blocked automatic enqueue.

PRAGMA foreign_keys=OFF;

CREATE TABLE jobs_new (
    id TEXT PRIMARY KEY,
    root_id TEXT NOT NULL,
    relative_path TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    pipeline_config_hash TEXT NOT NULL,
    state TEXT NOT NULL,
    current_stage TEXT,
    priority INTEGER NOT NULL DEFAULT 0,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT,
    lease_owner TEXT,
    lease_expires_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    error_code TEXT,
    error_detail TEXT,
    output_path TEXT,
    source_language_override TEXT,
    audio_stream_index_override INTEGER,
    preview_seconds REAL,
    preview_offset_seconds REAL,
    approved_at TEXT,
    execution_scope TEXT NOT NULL
);

INSERT INTO jobs_new (
    id, root_id, relative_path, fingerprint, pipeline_config_hash,
    state, current_stage, priority, attempt_count, next_attempt_at,
    lease_owner, lease_expires_at, created_at, updated_at, error_code,
    error_detail, output_path, source_language_override,
    audio_stream_index_override, preview_seconds, preview_offset_seconds,
    approved_at, execution_scope
)
SELECT
    id, root_id, relative_path, fingerprint, pipeline_config_hash,
    state, current_stage, priority, attempt_count, next_attempt_at,
    lease_owner, lease_expires_at, created_at, updated_at, error_code,
    error_detail, output_path, source_language_override,
    audio_stream_index_override, preview_seconds, preview_offset_seconds,
    approved_at,
    CASE WHEN preview_seconds IS NOT NULL THEN 'preview' ELSE 'full' END
FROM jobs;

DROP TABLE jobs;
ALTER TABLE jobs_new RENAME TO jobs;

CREATE INDEX idx_jobs_claim ON jobs (state, priority DESC, created_at);
CREATE UNIQUE INDEX idx_jobs_full_execution_identity
    ON jobs (root_id, relative_path, fingerprint, pipeline_config_hash)
    WHERE execution_scope = 'full';

PRAGMA foreign_key_check;
PRAGMA foreign_keys=ON;

INSERT INTO schema_migrations (version, applied_at) VALUES (2, datetime('now'));
