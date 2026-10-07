"""SQLite persistence for the queue. Owned by stage 2 of the plan.

Implements :class:`~nas_subtitles.domain.JobRepository`. Transactions are
short on purpose: the database lock is never held while a model is running.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from types import TracebackType

from .config import AppConfig
from .domain import (
    ArtifactRecord,
    ErrorCode,
    JobClaim,
    JobEvent,
    JobMetrics,
    JobRecord,
    JobState,
    MediaFingerprint,
    PipelineStage,
    ScanObservation,
    Seconds,
    TranslationCacheEntry,
)

__all__ = ["SqliteJobRepository", "open_repository"]


class SqliteJobRepository:
    """``JobRepository`` backed by ``state_dir/jobs.sqlite3``.

    Opens with WAL, ``foreign_keys=ON`` and ``busy_timeout=5000``. Claiming
    uses ``BEGIN IMMEDIATE`` so two processes can never lease the same job.
    """

    def __init__(self, database_path: Path, *, owner: str | None = None) -> None:
        self.database_path = database_path
        self.owner = owner

    def __enter__(self) -> SqliteJobRepository:
        raise NotImplementedError("the repository is implemented in stage 2 (state)")

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        raise NotImplementedError("the repository is implemented in stage 2 (state)")

    def initialise(self) -> None:
        """Create the directory, apply pending migrations and set the pragmas."""
        raise NotImplementedError("the repository is implemented in stage 2 (state)")

    def close(self) -> None:
        raise NotImplementedError("the repository is implemented in stage 2 (state)")

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
    ) -> JobRecord:
        raise NotImplementedError("the repository is implemented in stage 2 (state)")

    def get_job(self, job_id: str) -> JobRecord | None:
        raise NotImplementedError("the repository is implemented in stage 2 (state)")

    def list_jobs(
        self, *, state: JobState | None = None, limit: int = 100
    ) -> tuple[JobRecord, ...]:
        raise NotImplementedError("the repository is implemented in stage 2 (state)")

    def claim_next_job(self, *, owner: str, lease_seconds: int) -> JobClaim | None:
        raise NotImplementedError("the repository is implemented in stage 2 (state)")

    def renew_lease(self, *, job_id: str, owner: str, lease_seconds: int) -> bool:
        raise NotImplementedError("the repository is implemented in stage 2 (state)")

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
        raise NotImplementedError("the repository is implemented in stage 2 (state)")

    def approve_job(self, *, job_id: str) -> JobRecord:
        """Record the approval timestamp and move to ``ready_to_publish``."""
        raise NotImplementedError("the repository is implemented in stage 2 (state)")

    # -- artifacts, events and metrics ------------------------------------- #

    def record_artifact(self, artifact: ArtifactRecord) -> ArtifactRecord:
        raise NotImplementedError("the repository is implemented in stage 2 (state)")

    def list_artifacts(
        self, *, job_id: str, stage: PipelineStage | None = None
    ) -> tuple[ArtifactRecord, ...]:
        raise NotImplementedError("the repository is implemented in stage 2 (state)")

    def append_event(self, event: JobEvent) -> None:
        raise NotImplementedError("the repository is implemented in stage 2 (state)")

    def latest_event_at(self, *, code: str) -> datetime | None:
        raise NotImplementedError("the repository is implemented in stage 2 (state)")

    def record_metrics(self, metrics: JobMetrics) -> None:
        raise NotImplementedError("the repository is implemented in stage 2 (state)")

    # -- translation cache -------------------------------------------------- #

    def get_translation(self, cache_key: str) -> TranslationCacheEntry | None:
        raise NotImplementedError("the repository is implemented in stage 2 (state)")

    def put_translation(self, entry: TranslationCacheEntry) -> None:
        raise NotImplementedError("the repository is implemented in stage 2 (state)")

    # -- scanner bookkeeping ------------------------------------------------ #

    def get_scan_observation(self, *, root_id: str, relative_path: str) -> ScanObservation | None:
        raise NotImplementedError("the repository is implemented in stage 2 (state)")

    def upsert_scan_observation(self, observation: ScanObservation) -> ScanObservation:
        raise NotImplementedError("the repository is implemented in stage 2 (state)")

    # -- maintenance -------------------------------------------------------- #

    def backup_to(self, destination: Path) -> None:
        """Consistent copy via ``sqlite3.Connection.backup``, never a file copy."""
        raise NotImplementedError("the repository is implemented in stage 2 (state)")


def open_repository(config: AppConfig, *, owner: str | None = None) -> SqliteJobRepository:
    """Open (and migrate) the queue database described by ``config``."""
    raise NotImplementedError("the repository is implemented in stage 2 (state)")
