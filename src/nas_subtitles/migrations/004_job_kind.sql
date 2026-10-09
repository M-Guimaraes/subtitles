-- Schema version 4. Distinguish subtitle jobs from dubbing jobs.
-- Existing rows keep their ids and become job_kind=subtitles.
-- Deduplication of full library jobs includes job_kind so a sidecar skip
-- cannot satisfy a dubbing request for the same file.

ALTER TABLE jobs ADD COLUMN job_kind TEXT NOT NULL DEFAULT 'subtitles';
ALTER TABLE jobs ADD COLUMN dubbing_profile TEXT;

ALTER TABLE artifacts ADD COLUMN segment_id TEXT;
ALTER TABLE artifacts ADD COLUMN revision INTEGER;

DROP INDEX IF EXISTS idx_jobs_full_execution_identity;
CREATE UNIQUE INDEX idx_jobs_full_execution_identity
    ON jobs (root_id, relative_path, fingerprint, pipeline_config_hash, job_kind)
    WHERE execution_scope = 'full';

CREATE TABLE dub_segments (
    id TEXT NOT NULL,
    job_id TEXT NOT NULL REFERENCES jobs (id),
    revision INTEGER NOT NULL,
    start_seconds REAL NOT NULL,
    end_seconds REAL NOT NULL,
    original_text TEXT NOT NULL DEFAULT '',
    translated_text TEXT NOT NULL DEFAULT '',
    adapted_text TEXT NOT NULL DEFAULT '',
    speaker_id TEXT,
    review_state TEXT NOT NULL DEFAULT 'pending',
    updated_at TEXT NOT NULL,
    PRIMARY KEY (job_id, id, revision)
);

CREATE INDEX idx_dub_segments_job ON dub_segments (job_id, revision);

CREATE TABLE voice_assignments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL REFERENCES jobs (id),
    speaker_id TEXT NOT NULL,
    voice_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    reference_sha256 TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (job_id, speaker_id)
);

CREATE TABLE synthesis_artifacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL REFERENCES jobs (id),
    segment_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    path TEXT NOT NULL,
    duration_seconds REAL NOT NULL,
    model_identity TEXT NOT NULL,
    seed INTEGER,
    sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX idx_synthesis_artifacts_job
    ON synthesis_artifacts (job_id, segment_id, revision);

INSERT INTO schema_migrations (version, applied_at) VALUES (4, datetime('now'));
