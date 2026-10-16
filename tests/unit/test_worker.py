"""The worker loop, failure handling, cleanup inside work_dir and backups."""

from __future__ import annotations

import signal
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from nas_subtitles.config import AppConfig
from nas_subtitles.domain import (
    HEARTBEAT_EVENT_CODE,
    ErrorCode,
    ExitCode,
    JobState,
    LockBusyError,
    MediaFingerprint,
    NasSubtitlesError,
    PipelineStage,
)
from nas_subtitles.pipeline import StageContext
from nas_subtitles.repository import open_repository
from nas_subtitles.worker import Worker, backup_state, cleanup_work_dir, state_lock


def test_cleanup_ignores_non_job_directories_and_media(config: AppConfig, media_root: Path) -> None:
    repo = open_repository(config)
    stray = config.work_dir / "not-a-uuid"
    stray.mkdir()
    (stray / "chunk.wav").write_bytes(b"x")
    media = media_root / "keep.mkv"
    media.write_bytes(b"media")
    summary = cleanup_work_dir(config, repo, older_than_days=0, dry_run=False)
    repo.close()
    assert (stray / "chunk.wav").is_file()
    assert media.is_file()
    assert summary.inspected_jobs == 0


def test_backup_creates_a_readable_copy(config: AppConfig, tmp_path: Path) -> None:
    repo = open_repository(config)
    repo.close()
    folder = backup_state(config, tmp_path / "backups")
    assert (folder / "jobs.sqlite3").is_file()


# --- The worker loop, with the pipeline replaced by a failing factory --------


def _fingerprint(name: str) -> MediaFingerprint:
    return MediaFingerprint(
        root_id="library-aaaa1111",
        relative_path=name,
        size_bytes=1024,
        mtime_ns=1_700_000_000_000_000_000,
        head_sha256="a" * 64,
        tail_sha256="b" * 64,
        audio_stream_index=1,
    )


def _raising(exc: BaseException) -> Callable[..., StageContext]:
    def factory(*_args: object) -> StageContext:
        raise exc

    return factory


def test_run_once_returns_false_on_an_empty_queue(config: AppConfig) -> None:
    repo = open_repository(config)
    worker = Worker(config, repo, owner="w")
    assert worker.run_once() is False
    repo.close()


def test_retryable_failure_waits_with_a_backoff(config: AppConfig) -> None:
    repo = open_repository(config)
    job = repo.enqueue(fingerprint=_fingerprint("io.mkv"), pipeline_config_hash="h")
    error = NasSubtitlesError("disk hiccup", code=ErrorCode.IO_ERROR)
    worker = Worker(config, repo, owner="w", context_factory=_raising(error))
    before = datetime.now(tz=UTC)

    assert worker.run_once() is True

    updated = repo.require_job(job.id)
    repo.close()
    assert updated.state is JobState.RETRY_WAIT
    assert updated.error_code is ErrorCode.IO_ERROR
    assert updated.next_attempt_at is not None
    first_delay = config.worker.retry_delays_seconds[0]
    assert updated.next_attempt_at >= before + timedelta(seconds=first_delay - 1)


def test_non_retryable_failure_fails_the_job(config: AppConfig) -> None:
    repo = open_repository(config)
    job = repo.enqueue(fingerprint=_fingerprint("bad.mkv"), pipeline_config_hash="h")
    error = NasSubtitlesError("media changed", code=ErrorCode.MEDIA_CHANGED)
    worker = Worker(config, repo, owner="w", context_factory=_raising(error))

    worker.run_once()

    updated = repo.require_job(job.id)
    repo.close()
    assert updated.state is JobState.FAILED
    assert updated.error_code is ErrorCode.MEDIA_CHANGED


def test_unexpected_exception_fails_the_job_instead_of_killing_the_worker(
    config: AppConfig,
) -> None:
    repo = open_repository(config)
    job = repo.enqueue(fingerprint=_fingerprint("boom.mkv"), pipeline_config_hash="h")
    worker = Worker(config, repo, owner="w", context_factory=_raising(RuntimeError("boom")))

    assert worker.run_once() is True

    updated = repo.require_job(job.id)
    repo.close()
    assert updated.state is JobState.FAILED
    assert updated.error_code is ErrorCode.INVALID_MEDIA


def test_retry_attempts_stop_at_max_attempts(config: AppConfig) -> None:
    repo = open_repository(config)
    job = repo.enqueue(fingerprint=_fingerprint("flaky.mkv"), pipeline_config_hash="h")
    error = NasSubtitlesError("disk hiccup", code=ErrorCode.IO_ERROR)
    worker = Worker(config, repo, owner="w", context_factory=_raising(error))
    states = []
    for _ in range(config.worker.max_attempts):
        if repo.require_job(job.id).state is JobState.RETRY_WAIT:
            # Skip the backoff instead of sleeping through it.
            repo.transition(job_id=job.id, state=JobState.QUEUED)
        worker.run_once()
        states.append(repo.require_job(job.id).state)
    repo.close()
    assert states[-1] is JobState.FAILED
    assert all(state is JobState.RETRY_WAIT for state in states[:-1])


def _interrupt_while_transcribing(repo: object, worker: Worker, job_id: str) -> None:
    repo.claim_next_job(owner="w", lease_seconds=60)  # type: ignore[attr-defined]
    repo.transition(  # type: ignore[attr-defined]
        job_id=job_id, state=JobState.RUNNING, stage=PipelineStage.TRANSCRIBE
    )
    worker._handle_failure(job_id, NasSubtitlesError("stopping", code=ErrorCode.INTERRUPTED))


def test_an_interruption_requeues_the_job(config: AppConfig) -> None:
    repo = open_repository(config)
    job = repo.enqueue(fingerprint=_fingerprint("stop.mkv"), pipeline_config_hash="h")
    worker = Worker(config, repo, owner="w")

    _interrupt_while_transcribing(repo, worker, job.id)

    first = repo.require_job(job.id)
    repo.close()
    assert first.state is JobState.QUEUED
    assert first.error_code is ErrorCode.INTERRUPTED


@pytest.mark.xfail(
    strict=True,
    reason=(
        "known bug: claim_next_job and transition clear error_code, so "
        "_handle_failure never sees the earlier interruption"
    ),
)
def test_a_second_interruption_in_the_same_stage_asks_for_review(config: AppConfig) -> None:
    repo = open_repository(config)
    job = repo.enqueue(fingerprint=_fingerprint("stop-twice.mkv"), pipeline_config_hash="h")
    worker = Worker(config, repo, owner="w")
    _interrupt_while_transcribing(repo, worker, job.id)

    _interrupt_while_transcribing(repo, worker, job.id)

    second = repo.require_job(job.id)
    repo.close()
    assert second.state is JobState.NEEDS_REVIEW


def test_recover_interrupted_jobs_requeues_running_jobs(config: AppConfig) -> None:
    repo = open_repository(config)
    job = repo.enqueue(fingerprint=_fingerprint("crash.mkv"), pipeline_config_hash="h")
    repo.claim_next_job(owner="dead-worker", lease_seconds=60)
    worker = Worker(config, repo, owner="w")

    assert worker.recover_interrupted_jobs() == 1

    updated = repo.require_job(job.id)
    repo.close()
    assert updated.state is JobState.QUEUED
    assert updated.error_code is ErrorCode.INTERRUPTED


def test_heartbeat_records_an_event_and_renews_the_running_lease(config: AppConfig) -> None:
    repo = open_repository(config)
    job = repo.enqueue(fingerprint=_fingerprint("long.mkv"), pipeline_config_hash="h")
    claimed = repo.claim_next_job(owner="w", lease_seconds=1)
    assert claimed is not None
    worker = Worker(config, repo, owner="w")

    worker.heartbeat()

    events = [event for event in repo.list_events() if event.code == HEARTBEAT_EVENT_CODE]
    renewed = repo.require_job(job.id)
    repo.close()
    assert events and events[0].payload == {"owner": "w"}
    assert renewed.lease_expires_at is not None
    assert renewed.lease_expires_at > claimed.lease_expires_at


def test_a_second_worker_cannot_take_the_state_lock(config: AppConfig) -> None:
    with state_lock(config), pytest.raises(LockBusyError), state_lock(config):
        pass


def test_run_once_mode_processes_and_exits(config: AppConfig) -> None:
    repo = open_repository(config)
    job = repo.enqueue(fingerprint=_fingerprint("once.mkv"), pipeline_config_hash="h")
    error = NasSubtitlesError("media changed", code=ErrorCode.MEDIA_CHANGED)
    worker = Worker(config, repo, owner="w", context_factory=_raising(error))
    previous = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}
    try:
        assert worker.run(once=True) == int(ExitCode.SUCCESS)
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    state = repo.require_job(job.id).state
    repo.close()
    assert state is JobState.FAILED
    # The lock is released and the heartbeat thread told to stop.
    with state_lock(config):
        pass


def test_cleanup_drops_wavs_of_completed_jobs_and_keeps_failed_ones(config: AppConfig) -> None:
    repo = open_repository(config)
    done = repo.enqueue(fingerprint=_fingerprint("done.mkv"), pipeline_config_hash="h")
    repo.claim_next_job(owner="w", lease_seconds=60)
    repo.transition(job_id=done.id, state=JobState.COMPLETED)
    failed = repo.enqueue(fingerprint=_fingerprint("failed.mkv"), pipeline_config_hash="h")
    repo.claim_next_job(owner="w", lease_seconds=60)
    repo.transition(job_id=failed.id, state=JobState.FAILED, error_code=ErrorCode.IO_ERROR)
    for job_id in (done.id, failed.id):
        folder = config.work_dir / job_id
        folder.mkdir(parents=True)
        (folder / "chunk-0000.wav").write_bytes(b"w" * 10)
        (folder / "transcript.json").write_text("{}", encoding="utf-8")

    dry = cleanup_work_dir(config, repo, older_than_days=30, dry_run=True)
    assert (config.work_dir / done.id / "chunk-0000.wav").exists()
    assert dry.reclaimed_bytes == 10

    summary = cleanup_work_dir(config, repo, older_than_days=30, dry_run=False)
    repo.close()
    assert summary.inspected_jobs == 2
    assert not (config.work_dir / done.id / "chunk-0000.wav").exists()
    assert (config.work_dir / done.id / "transcript.json").exists()
    assert (config.work_dir / failed.id / "chunk-0000.wav").exists()
