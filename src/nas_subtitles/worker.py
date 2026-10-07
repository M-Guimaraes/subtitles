"""The single worker process: scan, claim, run, heartbeat. Owned by stage 7.

There is exactly one daemon. Scanning is a periodic task inside it, not a
second process. A ``flock`` on ``state_dir`` keeps two workers from sharing a
queue, and the heartbeat runs on its own thread so a blocking inference still
looks alive.
"""

from __future__ import annotations

import re
import shutil
import signal
import threading
from collections.abc import Callable
from contextlib import AbstractContextManager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .config import AppConfig
from .discovery import scan
from .domain import (
    HEARTBEAT_EVENT_CODE,
    ErrorCode,
    EventLevel,
    ExitCode,
    JobEvent,
    JobRepository,
    JobState,
    NasSubtitlesError,
)
from .models import MODEL_MANIFEST_FILENAME
from .pipeline import StageContext, build_context, run_job
from .repository import StateDirLock, open_repository
from .states import retry_delay_seconds, should_retry

__all__ = [
    "CleanupSummary",
    "Worker",
    "backup_state",
    "cleanup_work_dir",
    "state_lock",
]

_JOB_ID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class CleanupSummary:
    """What ``cleanup`` removed, or would remove under ``--dry-run``."""

    inspected_jobs: int = 0
    removed_paths: tuple[str, ...] = ()
    reclaimed_bytes: int = 0
    dry_run: bool = True


class Worker:
    """Serial worker: one job at a time, SIGTERM-aware."""

    def __init__(
        self,
        config: AppConfig,
        repository: JobRepository,
        *,
        owner: str,
        context_factory: Callable[..., StageContext] | None = None,
    ) -> None:
        self.config = config
        self.repository = repository
        self.owner = owner
        self._stop = threading.Event()
        self._context_factory = context_factory
        self._heartbeat_thread: threading.Thread | None = None

    def run(self, *, once: bool = False) -> int:
        """Loop over scan and queue until stopped; returns a process exit code."""
        with state_lock(self.config):
            self._install_signal_handlers()
            self._start_heartbeat()
            try:
                while not self._stop.is_set():
                    scan(self.config, self.repository)
                    processed = self.run_once()
                    if once:
                        return int(ExitCode.SUCCESS)
                    if not processed:
                        self._stop.wait(self.config.scan_interval_seconds)
            except NasSubtitlesError as exc:
                if exc.code is ErrorCode.INTERRUPTED:
                    return int(ExitCode.SUCCESS)
                raise
            finally:
                self._stop.set()
        return int(ExitCode.SUCCESS)

    def run_once(self) -> bool:
        """Claim and process at most one job. ``False`` when the queue is empty."""
        claim = self.repository.claim_next_job(
            owner=self.owner, lease_seconds=self.config.worker.stale_lease_seconds
        )
        if claim is None:
            return False
        job = claim.job
        try:
            if self._context_factory is not None:
                context = self._context_factory(self.config, self.repository, job, self._stop)
            else:
                context = build_context(self.config, self.repository, job, stop_event=self._stop)
            result = run_job(context)
            return result.job_id == job.id
        except NasSubtitlesError as exc:
            self._handle_failure(job.id, exc)
            return True

    def request_stop(self) -> None:
        """Called from the SIGTERM handler; aborts the in-flight checkpoint."""
        self._stop.set()

    def heartbeat(self) -> None:
        """Record one ``worker_heartbeat`` event; runs on its own thread."""
        self.repository.append_event(
            JobEvent(
                level=EventLevel.INFO,
                code=HEARTBEAT_EVENT_CODE,
                payload={"owner": self.owner},
            )
        )
        with suppress(NasSubtitlesError):
            # Renew whatever we currently lease, if any.
            running = self.repository.list_jobs(state=JobState.RUNNING, limit=1)
            if running:
                self.repository.renew_lease(
                    job_id=running[0].id,
                    owner=self.owner,
                    lease_seconds=self.config.worker.stale_lease_seconds,
                )

    def _start_heartbeat(self) -> None:
        def loop() -> None:
            while not self._stop.wait(self.config.worker.heartbeat_seconds):
                try:
                    self.heartbeat()
                except Exception:
                    continue

        self.heartbeat()
        thread = threading.Thread(target=loop, name="nas-subs-heartbeat", daemon=True)
        self._heartbeat_thread = thread
        thread.start()

    def _install_signal_handlers(self) -> None:
        def handler(_signum: int, _frame: object | None) -> None:
            self.request_stop()

        signal.signal(signal.SIGTERM, handler)
        signal.signal(signal.SIGINT, handler)

    def _handle_failure(self, job_id: str, exc: NasSubtitlesError) -> None:
        job = self.repository.get_job(job_id)
        if job is None:
            return
        if exc.code is ErrorCode.INTERRUPTED:
            if job.error_code is ErrorCode.INTERRUPTED and job.current_stage is not None:
                self.repository.transition(
                    job_id=job_id,
                    state=JobState.NEEDS_REVIEW,
                    error_code=ErrorCode.INTERRUPTED,
                    error_detail="interrupted twice in the same stage",
                )
                return
            self.repository.transition(
                job_id=job_id,
                state=JobState.QUEUED,
                error_code=ErrorCode.INTERRUPTED,
                error_detail=exc.message,
            )
            return
        attempts = job.attempt_count
        if should_retry(
            error_code=exc.code,
            attempt_count=attempts,
            max_attempts=self.config.worker.max_attempts,
        ):
            delay = retry_delay_seconds(self.config.worker.retry_delays_seconds, attempts)
            self.repository.transition(
                job_id=job_id,
                state=JobState.RETRY_WAIT,
                error_code=exc.code,
                error_detail=exc.message,
                next_attempt_at=datetime.now(tz=UTC) + timedelta(seconds=delay),
            )
            return
        self.repository.transition(
            job_id=job_id,
            state=JobState.FAILED,
            error_code=exc.code,
            error_detail=exc.message,
        )


def state_lock(config: AppConfig) -> AbstractContextManager[StateDirLock]:
    """``flock`` on ``state_dir``; raises ``LockBusyError`` (exit 6) if taken."""
    return StateDirLock(config.lock_path)


def cleanup_work_dir(
    config: AppConfig,
    repository: JobRepository,
    *,
    older_than_days: int = 30,
    dry_run: bool = True,
) -> CleanupSummary:
    """Delete only inside ``work_dir``, only for validated job IDs, never by glob.

    A successful job loses its WAV files immediately; transcripts, translations
    and manifests survive for ``older_than_days``. Failed and under-review jobs
    keep their checkpoints.
    """
    removed: list[str] = []
    reclaimed = 0
    inspected = 0
    cutoff = datetime.now(tz=UTC) - timedelta(days=older_than_days)
    if not config.work_dir.is_dir():
        return CleanupSummary(dry_run=dry_run)
    for child in config.work_dir.iterdir():
        if not child.is_dir() or not _JOB_ID_RE.match(child.name):
            continue
        job = repository.get_job(child.name)
        if job is None:
            continue
        inspected += 1
        if job.state is JobState.COMPLETED:
            for wav in child.rglob("*.wav"):
                reclaimed += _unlink(wav, dry_run=dry_run, removed=removed)
        if job.state in {JobState.FAILED, JobState.NEEDS_REVIEW, JobState.CANCELLED}:
            continue
        if job.state is JobState.COMPLETED and job.updated_at < cutoff:
            reclaimed += _rmtree(child, dry_run=dry_run, removed=removed)
    return CleanupSummary(
        inspected_jobs=inspected,
        removed_paths=tuple(removed),
        reclaimed_bytes=reclaimed,
        dry_run=dry_run,
    )


def backup_state(config: AppConfig, destination: Path) -> Path:
    """Back up config, lock, model manifest and the SQLite database."""
    destination.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%SZ")
    folder = destination / f"nas-subtitles-{stamp}"
    folder.mkdir(parents=True, exist_ok=True)
    repo = open_repository(config)
    try:
        repo.backup_to(folder / "jobs.sqlite3")
    finally:
        repo.close()
    manifest = config.models_dir / MODEL_MANIFEST_FILENAME
    if manifest.is_file():
        shutil.copy2(manifest, folder / MODEL_MANIFEST_FILENAME)
    return folder


def _unlink(path: Path, *, dry_run: bool, removed: list[str]) -> int:
    size = path.stat().st_size if path.is_file() else 0
    removed.append(str(path.name))
    if not dry_run:
        path.unlink()
    return size


def _rmtree(path: Path, *, dry_run: bool, removed: list[str]) -> int:
    total = 0
    for child in path.rglob("*"):
        if child.is_file():
            total += child.stat().st_size
            removed.append(child.name)
    removed.append(path.name)
    if not dry_run:
        shutil.rmtree(path)
    return total
