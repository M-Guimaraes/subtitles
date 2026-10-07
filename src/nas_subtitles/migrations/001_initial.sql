-- Schema version 1. Applied by SqliteJobRepository.initialise.
-- WAL, foreign_keys and busy_timeout are set on the connection, not here.

CREATE TABLE schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE jobs (
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
    UNIQUE (root_id, relative_path, fingerprint, pipeline_config_hash)
);

CREATE INDEX idx_jobs_claim ON jobs (state, priority DESC, created_at);

CREATE TABLE artifacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL REFERENCES jobs (id),
    stage TEXT NOT NULL,
    chunk_index INTEGER,
    path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    schema_version INTEGER NOT NULL,
    stage_config_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT REFERENCES jobs (id),
    level TEXT NOT NULL,
    code TEXT NOT NULL,
    payload_json TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX idx_events_code_created ON events (code, created_at);

CREATE TABLE translation_cache (
    cache_key TEXT PRIMARY KEY,
    translated_text TEXT NOT NULL,
    engine_identity TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE metrics (
    job_id TEXT PRIMARY KEY REFERENCES jobs (id),
    media_seconds REAL NOT NULL DEFAULT 0,
    extraction_seconds REAL NOT NULL DEFAULT 0,
    asr_seconds REAL NOT NULL DEFAULT 0,
    translation_seconds REAL NOT NULL DEFAULT 0,
    total_seconds REAL NOT NULL DEFAULT 0,
    peak_rss_bytes INTEGER,
    output_cues INTEGER NOT NULL DEFAULT 0,
    quality_flags_json TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE scan_observations (
    root_id TEXT NOT NULL,
    relative_path TEXT NOT NULL,
    size INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    first_stable_seen_at TEXT,
    last_seen_at TEXT,
    PRIMARY KEY (root_id, relative_path)
);

INSERT INTO schema_migrations (version, applied_at) VALUES (1, datetime('now'));
