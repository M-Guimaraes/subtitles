"""SQLite persistence for the queue. Owned by stage 2 of the plan.

Implements :class:`~nas_subtitles.domain.JobRepository`. Transactions are
short on purpose: the database lock is never held while a model is running.
"""

from __future__ import annotations

import fcntl
import json
import sqlite3
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import TracebackType
from typing import IO

from .config import AppConfig
from .domain import (
    DB_SCHEMA_VERSION,
    ArtifactRecord,
    ErrorCode,
    EventLevel,
    JobClaim,
    JobEvent,
    JobExecutionScope,
    JobMetrics,
    JobRecord,
    JobState,
    LockBusyError,
    MediaFingerprint,
    NasSubtitlesError,
    PipelineStage,
    QualityFlag,
    QualityFlagCode,
    QualitySeverity,
    ScanObservation,
    Seconds,
    TranslationCacheEntry,
    canonical_json,
    infer_execution_scope,
)
from .logging_setup import event_payload
from .states import ensure_transition

__all__ = ["SqliteJobRepository", "StateDirLock", "open_repository"]

_MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"


class StateDirLock:
    """Exclusive ``flock`` on ``state_dir/worker.lock`` (exit code 6 when held)."""

    def __init__(self, lock_path: Path) -> None:
        self.lock_path = lock_path
        self._handle: IO[str] | None = None

    def __enter__(self) -> StateDirLock:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.lock_path.open("a", encoding="utf-8")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            handle.close()
            raise LockBusyError(
                f"another worker already holds {self.lock_path.name}",
                detail={"lock": self.lock_path.name},
            ) from exc
        self._handle = handle
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        handle = self._handle
        self._handle = None
        if handle is None:
            return
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


class SqliteJobRepository:
    """``JobRepository`` backed by ``state_dir/jobs.sqlite3``.

    Opens with WAL, ``foreign_keys=ON`` and ``busy_timeout=5000``. Claiming
    uses ``BEGIN IMMEDIATE`` so two processes can never lease the same job.
    """

    def __init__(self, database_path: Path, *, owner: str | None = None) -> None:
        self.database_path = database_path
        self.owner = owner
        self._connection: sqlite3.Connection | None = None

    def __enter__(self) -> SqliteJobRepository:
        self.initialise()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def initialise(self) -> None:
        """Create the directory, apply pending migrations and set the pragmas."""
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = self._connect()
        current = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'schema_migrations'"
        ).fetchone()
        applied: set[int] = set()
        if current is not None:
            applied = {
                int(row["version"])
                for row in connection.execute("SELECT version FROM schema_migrations")
            }
        for migration in _pending_migrations(applied):
            # executescript issues its own COMMIT; do not wrap it in BEGIN IMMEDIATE.
            connection.executescript(migration.read_text(encoding="utf-8"))
        self._assert_schema_version(connection)

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    # -- queue ------------------------------------------------------------- #

    def enqueue(
        self,
        *,
        fingerprint: MediaFingerprint,
        pipeline_config_hash: str,
        priority: int = 0,
        source_language_override: str | None = None,
        audio_stream_index_override: int | None = None,
        preview_seconds: Seconds | None = None,
        preview_offset_seconds: Seconds | None = None,
        target_language: str | None = None,
    ) -> JobRecord:
        now = datetime.now(tz=UTC)
        fingerprint_json = _fingerprint_json(fingerprint)
        job_id = str(uuid.uuid4())
        scope = infer_execution_scope(preview_seconds=preview_seconds)
        with self._transaction(immediate=True):
            if scope is JobExecutionScope.FULL:
                existing = (
                    self._connection_or_raise()
                    .execute(
                        """
                    SELECT * FROM jobs
                    WHERE root_id = ? AND relative_path = ? AND fingerprint = ?
                          AND pipeline_config_hash = ? AND execution_scope = ?
                    """,
                        (
                            fingerprint.root_id,
                            fingerprint.relative_path,
                            fingerprint_json,
                            pipeline_config_hash,
                            str(scope),
                        ),
                    )
                    .fetchone()
                )
                if existing is not None:
                    return _job_from_row(existing)
            self._connection_or_raise().execute(
                """
                INSERT INTO jobs (
                    id, root_id, relative_path, fingerprint, pipeline_config_hash,
                    state, current_stage, priority, attempt_count, created_at, updated_at,
                    source_language_override, audio_stream_index_override,
                    preview_seconds, preview_offset_seconds, execution_scope,
                    target_language
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    fingerprint.root_id,
                    fingerprint.relative_path,
                    fingerprint_json,
                    pipeline_config_hash,
                    str(JobState.QUEUED),
                    None,
                    priority,
                    _iso(now),
                    _iso(now),
                    source_language_override,
                    audio_stream_index_override,
                    preview_seconds,
                    preview_offset_seconds,
                    str(scope),
                    target_language,
                ),
            )
        job = self.get_job(job_id)
        if job is None:
            raise NasSubtitlesError("enqueued job could not be read back", code=ErrorCode.IO_ERROR)
        return job

    def get_job(self, job_id: str) -> JobRecord | None:
        row = (
            self._connection_or_raise()
            .execute("SELECT * FROM jobs WHERE id = ?", (job_id,))
            .fetchone()
        )
        return _job_from_row(row) if row is not None else None

    def list_jobs(
        self, *, state: JobState | None = None, limit: int = 100
    ) -> tuple[JobRecord, ...]:
        connection = self._connection_or_raise()
        if state is None:
            rows = connection.execute(
                "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        else:
            rows = connection.execute(
                "SELECT * FROM jobs WHERE state = ? ORDER BY created_at DESC LIMIT ?",
                (str(state), limit),
            ).fetchall()
        return tuple(_job_from_row(row) for row in rows)

    def claim_next_job(self, *, owner: str, lease_seconds: int) -> JobClaim | None:
        now = datetime.now(tz=UTC)
        expires = now + timedelta(seconds=lease_seconds)
        with self._transaction(immediate=True):
            row = (
                self._connection_or_raise()
                .execute(
                    """
                SELECT * FROM jobs
                WHERE state = ?
                   OR (state = ? AND (next_attempt_at IS NULL OR next_attempt_at <= ?))
                   OR (state = ? AND lease_expires_at IS NOT NULL AND lease_expires_at <= ?)
                ORDER BY priority DESC, created_at ASC
                LIMIT 1
                """,
                    (
                        str(JobState.QUEUED),
                        str(JobState.RETRY_WAIT),
                        _iso(now),
                        str(JobState.RUNNING),
                        _iso(now),
                    ),
                )
                .fetchone()
            )
            if row is None:
                return None
            job = _job_from_row(row)
            ensure_transition(job.state, JobState.RUNNING)
            increment = job.state is not JobState.RUNNING
            attempt_count = job.attempt_count + 1 if increment else job.attempt_count
            self._connection_or_raise().execute(
                """
                UPDATE jobs
                SET state = ?, lease_owner = ?, lease_expires_at = ?,
                    attempt_count = ?, next_attempt_at = NULL, updated_at = ?,
                    error_code = CASE WHEN ? THEN NULL ELSE error_code END,
                    error_detail = CASE WHEN ? THEN NULL ELSE error_detail END
                WHERE id = ?
                """,
                (
                    str(JobState.RUNNING),
                    owner,
                    _iso(expires),
                    attempt_count,
                    _iso(now),
                    increment,
                    increment,
                    job.id,
                ),
            )
        claimed = self.get_job(job.id)
        if claimed is None:
            return None
        return JobClaim(job=claimed, lease_owner=owner, lease_expires_at=expires)

    def renew_lease(self, *, job_id: str, owner: str, lease_seconds: int) -> bool:
        now = datetime.now(tz=UTC)
        expires = now + timedelta(seconds=lease_seconds)
        with self._transaction(immediate=True):
            cursor = self._connection_or_raise().execute(
                """
                UPDATE jobs
                SET lease_expires_at = ?, updated_at = ?
                WHERE id = ? AND lease_owner = ? AND state = ?
                """,
                (_iso(expires), _iso(now), job_id, owner, str(JobState.RUNNING)),
            )
            return cursor.rowcount == 1

    def transition(
        self,
        *,
        job_id: str,
        state: JobState,
        stage: PipelineStage | None = None,
        error_code: ErrorCode | None = None,
        error_detail: str | None = None,
        next_attempt_at: datetime | None = None,
        output_path: Path | None = None,
    ) -> JobRecord:
        job = self.require_job(job_id)
        ensure_transition(job.state, state)
        now = datetime.now(tz=UTC)
        clear_lease = state is not JobState.RUNNING
        with self._transaction(immediate=True):
            self._connection_or_raise().execute(
                """
                UPDATE jobs
                SET state = ?,
                    current_stage = COALESCE(?, current_stage),
                    error_code = ?,
                    error_detail = ?,
                    next_attempt_at = ?,
                    output_path = COALESCE(?, output_path),
                    lease_owner = CASE WHEN ? THEN NULL ELSE lease_owner END,
                    lease_expires_at = CASE WHEN ? THEN NULL ELSE lease_expires_at END,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    str(state),
                    str(stage) if stage is not None else None,
                    str(error_code) if error_code is not None else None,
                    error_detail,
                    _iso(next_attempt_at) if next_attempt_at is not None else None,
                    str(output_path) if output_path is not None else None,
                    clear_lease,
                    clear_lease,
                    _iso(now),
                    job_id,
                ),
            )
        updated = self.require_job(job_id)
        return updated

    def approve_job(self, *, job_id: str) -> JobRecord:
        """Record the approval timestamp and move to ``ready_to_publish``."""
        job = self.require_job(job_id)
        ensure_transition(job.state, JobState.READY_TO_PUBLISH)
        now = datetime.now(tz=UTC)
        with self._transaction(immediate=True):
            self._connection_or_raise().execute(
                """
                UPDATE jobs
                SET state = ?, approved_at = ?, updated_at = ?,
                    lease_owner = NULL, lease_expires_at = NULL
                WHERE id = ?
                """,
                (str(JobState.READY_TO_PUBLISH), _iso(now), _iso(now), job_id),
            )
        return self.require_job(job_id)

    def require_job(self, job_id: str) -> JobRecord:
        job = self.get_job(job_id)
        if job is None:
            raise NasSubtitlesError(
                f"job {job_id} was not found",
                code=ErrorCode.JOB_NOT_FOUND,
                detail={"job_id": job_id},
            )
        return job

    # -- artifacts, events and metrics ------------------------------------- #

    def record_artifact(self, artifact: ArtifactRecord) -> ArtifactRecord:
        now = artifact.created_at or datetime.now(tz=UTC)
        with self._transaction(immediate=True):
            cursor = self._connection_or_raise().execute(
                """
                INSERT INTO artifacts (
                    job_id, stage, chunk_index, path, sha256,
                    schema_version, stage_config_hash, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    artifact.job_id,
                    str(artifact.stage),
                    artifact.chunk_index,
                    str(artifact.path),
                    artifact.sha256,
                    artifact.schema_version,
                    artifact.stage_config_hash,
                    _iso(now),
                ),
            )
            artifact_id = int(cursor.lastrowid or 0)
        return ArtifactRecord(
            job_id=artifact.job_id,
            stage=artifact.stage,
            path=artifact.path,
            sha256=artifact.sha256,
            schema_version=artifact.schema_version,
            stage_config_hash=artifact.stage_config_hash,
            chunk_index=artifact.chunk_index,
            created_at=now,
            id=artifact_id,
        )

    def list_artifacts(
        self, *, job_id: str, stage: PipelineStage | None = None
    ) -> tuple[ArtifactRecord, ...]:
        connection = self._connection_or_raise()
        if stage is None:
            rows = connection.execute(
                "SELECT * FROM artifacts WHERE job_id = ? ORDER BY id", (job_id,)
            ).fetchall()
        else:
            rows = connection.execute(
                "SELECT * FROM artifacts WHERE job_id = ? AND stage = ? ORDER BY id",
                (job_id, str(stage)),
            ).fetchall()
        return tuple(_artifact_from_row(row) for row in rows)

    def append_event(self, event: JobEvent) -> None:
        now = event.created_at or datetime.now(tz=UTC)
        payload = canonical_json(event_payload(event.payload))
        with self._transaction(immediate=True):
            self._connection_or_raise().execute(
                """
                INSERT INTO events (job_id, level, code, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    event.job_id,
                    str(event.level),
                    event.code,
                    payload,
                    _iso(now),
                ),
            )

    def latest_event_at(self, *, code: str) -> datetime | None:
        row = (
            self._connection_or_raise()
            .execute(
                "SELECT max(created_at) AS created_at FROM events WHERE code = ?",
                (code,),
            )
            .fetchone()
        )
        if row is None or row["created_at"] is None:
            return None
        return _parse_datetime(str(row["created_at"]))

    def record_metrics(self, metrics: JobMetrics) -> None:
        flags = canonical_json(
            [
                {
                    "code": str(flag.code),
                    "message": flag.message,
                    "severity": str(flag.severity),
                    "cue_index": flag.cue_index,
                    "unit_id": flag.unit_id,
                    "observed": flag.observed,
                    "threshold": flag.threshold,
                }
                for flag in metrics.quality_flags
            ]
        )
        with self._transaction(immediate=True):
            self._connection_or_raise().execute(
                """
                INSERT INTO metrics (
                    job_id, media_seconds, extraction_seconds, asr_seconds,
                    translation_seconds, total_seconds, peak_rss_bytes,
                    output_cues, quality_flags_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(job_id) DO UPDATE SET
                    media_seconds = excluded.media_seconds,
                    extraction_seconds = excluded.extraction_seconds,
                    asr_seconds = excluded.asr_seconds,
                    translation_seconds = excluded.translation_seconds,
                    total_seconds = excluded.total_seconds,
                    peak_rss_bytes = excluded.peak_rss_bytes,
                    output_cues = excluded.output_cues,
                    quality_flags_json = excluded.quality_flags_json
                """,
                (
                    metrics.job_id,
                    metrics.media_seconds,
                    metrics.extraction_seconds,
                    metrics.asr_seconds,
                    metrics.translation_seconds,
                    metrics.total_seconds,
                    metrics.peak_rss_bytes,
                    metrics.output_cues,
                    flags,
                ),
            )

    def get_metrics(self, job_id: str) -> JobMetrics | None:
        row = (
            self._connection_or_raise()
            .execute("SELECT * FROM metrics WHERE job_id = ?", (job_id,))
            .fetchone()
        )
        if row is None:
            return None
        raw_flags = json.loads(row["quality_flags_json"] or "[]")
        flags = tuple(
            QualityFlag(
                code=QualityFlagCode(item["code"]),
                message=str(item["message"]),
                severity=QualitySeverity(item["severity"]),
                cue_index=item.get("cue_index"),
                unit_id=item.get("unit_id"),
                observed=item.get("observed"),
                threshold=item.get("threshold"),
            )
            for item in raw_flags
        )
        return JobMetrics(
            job_id=row["job_id"],
            media_seconds=row["media_seconds"],
            extraction_seconds=row["extraction_seconds"],
            asr_seconds=row["asr_seconds"],
            translation_seconds=row["translation_seconds"],
            total_seconds=row["total_seconds"],
            peak_rss_bytes=row["peak_rss_bytes"],
            output_cues=row["output_cues"],
            quality_flags=flags,
        )

    # -- translation cache -------------------------------------------------- #

    def get_translation(self, cache_key: str) -> TranslationCacheEntry | None:
        row = (
            self._connection_or_raise()
            .execute("SELECT * FROM translation_cache WHERE cache_key = ?", (cache_key,))
            .fetchone()
        )
        if row is None:
            return None
        return TranslationCacheEntry(
            cache_key=row["cache_key"],
            translated_text=row["translated_text"],
            engine_identity=row["engine_identity"],
            created_at=_parse_datetime(row["created_at"]),
        )

    def put_translation(self, entry: TranslationCacheEntry) -> None:
        now = entry.created_at or datetime.now(tz=UTC)
        with self._transaction(immediate=True):
            self._connection_or_raise().execute(
                """
                INSERT INTO translation_cache (
                    cache_key, translated_text, engine_identity, created_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(cache_key) DO UPDATE SET
                    translated_text = excluded.translated_text,
                    engine_identity = excluded.engine_identity
                """,
                (entry.cache_key, entry.translated_text, entry.engine_identity, _iso(now)),
            )

    # -- scanner bookkeeping ------------------------------------------------ #

    def get_scan_observation(self, *, root_id: str, relative_path: str) -> ScanObservation | None:
        row = (
            self._connection_or_raise()
            .execute(
                """
            SELECT * FROM scan_observations
            WHERE root_id = ? AND relative_path = ?
            """,
                (root_id, relative_path),
            )
            .fetchone()
        )
        if row is None:
            return None
        return ScanObservation(
            root_id=row["root_id"],
            relative_path=row["relative_path"],
            size_bytes=row["size"],
            mtime_ns=row["mtime_ns"],
            first_stable_seen_at=_optional_datetime(row["first_stable_seen_at"]),
            last_seen_at=_optional_datetime(row["last_seen_at"]),
        )

    def upsert_scan_observation(self, observation: ScanObservation) -> ScanObservation:
        with self._transaction(immediate=True):
            self._connection_or_raise().execute(
                """
                INSERT INTO scan_observations (
                    root_id, relative_path, size, mtime_ns,
                    first_stable_seen_at, last_seen_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(root_id, relative_path) DO UPDATE SET
                    size = excluded.size,
                    mtime_ns = excluded.mtime_ns,
                    first_stable_seen_at = excluded.first_stable_seen_at,
                    last_seen_at = excluded.last_seen_at
                """,
                (
                    observation.root_id,
                    observation.relative_path,
                    observation.size_bytes,
                    observation.mtime_ns,
                    _iso(observation.first_stable_seen_at)
                    if observation.first_stable_seen_at
                    else None,
                    _iso(observation.last_seen_at) if observation.last_seen_at else None,
                ),
            )
        stored = self.get_scan_observation(
            root_id=observation.root_id, relative_path=observation.relative_path
        )
        if stored is None:
            raise NasSubtitlesError(
                "scan observation could not be read back",
                code=ErrorCode.IO_ERROR,
            )
        return stored

    # -- maintenance -------------------------------------------------------- #

    def backup_to(self, destination: Path) -> None:
        """Consistent copy via ``sqlite3.Connection.backup``, never a file copy."""
        destination.parent.mkdir(parents=True, exist_ok=True)
        source = self._connection_or_raise()
        with sqlite3.connect(destination) as target:
            source.backup(target)

    def list_events(self, *, job_id: str | None = None, limit: int = 100) -> tuple[JobEvent, ...]:
        connection = self._connection_or_raise()
        if job_id is None:
            rows = connection.execute(
                "SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        else:
            rows = connection.execute(
                "SELECT * FROM events WHERE job_id = ? ORDER BY id DESC LIMIT ?",
                (job_id, limit),
            ).fetchall()
        return tuple(_event_from_row(row) for row in rows)

    # -- connection helpers ------------------------------------------------- #

    def _connect(self) -> sqlite3.Connection:
        if self._connection is not None:
            return self._connection
        connection = sqlite3.connect(
            self.database_path,
            timeout=5.0,
            isolation_level=None,
            check_same_thread=False,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        self._connection = connection
        return connection

    def _connection_or_raise(self) -> sqlite3.Connection:
        if self._connection is None:
            return self._connect()
        return self._connection

    @contextmanager
    def _transaction(self, *, immediate: bool) -> Iterator[sqlite3.Connection]:
        connection = self._connection_or_raise()
        connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        try:
            yield connection
        except Exception:
            connection.execute("ROLLBACK")
            raise
        else:
            connection.execute("COMMIT")

    def _assert_schema_version(self, connection: sqlite3.Connection) -> None:
        row = connection.execute("SELECT max(version) AS version FROM schema_migrations").fetchone()
        version = int(row["version"]) if row is not None and row["version"] is not None else 0
        if version != DB_SCHEMA_VERSION:
            raise NasSubtitlesError(
                f"database schema version {version} does not match {DB_SCHEMA_VERSION}",
                code=ErrorCode.IO_ERROR,
            )


def open_repository(config: AppConfig, *, owner: str | None = None) -> SqliteJobRepository:
    """Open (and migrate) the queue database described by ``config``."""
    repository = SqliteJobRepository(config.database_path, owner=owner)
    repository.initialise()
    return repository


def _pending_migrations(applied: set[int]) -> tuple[Path, ...]:
    if not _MIGRATIONS_DIR.is_dir():
        raise NasSubtitlesError(
            f"migrations directory is missing at {_MIGRATIONS_DIR}",
            code=ErrorCode.IO_ERROR,
        )
    pending: list[Path] = []
    for path in sorted(_MIGRATIONS_DIR.glob("*.sql")):
        version = int(path.stem.split("_", 1)[0])
        if version not in applied:
            pending.append(path)
    return tuple(pending)


def _fingerprint_json(fingerprint: MediaFingerprint) -> str:
    return canonical_json(asdict(fingerprint))


def _parse_fingerprint(raw: str) -> MediaFingerprint:
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise NasSubtitlesError("stored fingerprint is not an object", code=ErrorCode.IO_ERROR)
    stream = payload.get("audio_stream_index")
    return MediaFingerprint(
        root_id=str(payload["root_id"]),
        relative_path=str(payload["relative_path"]),
        size_bytes=_as_int(payload["size_bytes"]),
        mtime_ns=_as_int(payload["mtime_ns"]),
        head_sha256=str(payload["head_sha256"]),
        tail_sha256=str(payload["tail_sha256"]),
        audio_stream_index=None if stream is None else _as_int(stream),
    )


def _as_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise NasSubtitlesError(
            "stored fingerprint field is not an integer",
            code=ErrorCode.IO_ERROR,
        )
    return value


def _job_from_row(row: sqlite3.Row) -> JobRecord:
    return JobRecord(
        id=row["id"],
        root_id=row["root_id"],
        relative_path=row["relative_path"],
        fingerprint=_parse_fingerprint(row["fingerprint"]),
        pipeline_config_hash=row["pipeline_config_hash"],
        state=JobState(row["state"]),
        created_at=_parse_datetime(row["created_at"]),
        updated_at=_parse_datetime(row["updated_at"]),
        current_stage=PipelineStage(row["current_stage"]) if row["current_stage"] else None,
        priority=row["priority"],
        attempt_count=row["attempt_count"],
        next_attempt_at=_optional_datetime(row["next_attempt_at"]),
        lease_owner=row["lease_owner"],
        lease_expires_at=_optional_datetime(row["lease_expires_at"]),
        error_code=ErrorCode(row["error_code"]) if row["error_code"] else None,
        error_detail=row["error_detail"],
        output_path=Path(row["output_path"]) if row["output_path"] else None,
        source_language_override=row["source_language_override"],
        audio_stream_index_override=row["audio_stream_index_override"],
        preview_seconds=row["preview_seconds"],
        preview_offset_seconds=row["preview_offset_seconds"],
        approved_at=_optional_datetime(row["approved_at"]),
        execution_scope=_execution_scope_from_row(row),
        target_language=_optional_target_language(row),
    )


def _optional_target_language(row: sqlite3.Row) -> str | None:
    try:
        stored = row["target_language"]
    except IndexError:
        return None
    if stored is None:
        return None
    text = str(stored).strip()
    return text or None


def _execution_scope_from_row(row: sqlite3.Row) -> JobExecutionScope:
    stored: str | None
    try:
        stored = row["execution_scope"]
    except IndexError:
        stored = None
    return infer_execution_scope(stored=stored, preview_seconds=row["preview_seconds"])


def _artifact_from_row(row: sqlite3.Row) -> ArtifactRecord:
    return ArtifactRecord(
        job_id=row["job_id"],
        stage=PipelineStage(row["stage"]),
        path=Path(row["path"]),
        sha256=row["sha256"],
        schema_version=row["schema_version"],
        stage_config_hash=row["stage_config_hash"],
        chunk_index=row["chunk_index"],
        created_at=_parse_datetime(row["created_at"]),
        id=row["id"],
    )


def _event_from_row(row: sqlite3.Row) -> JobEvent:
    payload_raw = row["payload_json"]
    payload: Mapping[str, object] = json.loads(payload_raw) if payload_raw else {}
    return JobEvent(
        level=EventLevel(row["level"]),
        code=row["code"],
        job_id=row["job_id"],
        payload=payload,
        created_at=_parse_datetime(row["created_at"]),
        id=row["id"],
    )


def _iso(moment: datetime) -> str:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).isoformat()


def _parse_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _optional_datetime(value: str | None) -> datetime | None:
    if value is None:
        return None
    return _parse_datetime(value)
