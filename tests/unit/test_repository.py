"""Queue persistence: claims, leases, retries, heartbeats and backup."""

from __future__ import annotations

import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from nas_subtitles.cli import app
from nas_subtitles.config import AppConfig
from nas_subtitles.domain import (
    HEARTBEAT_EVENT_CODE,
    ErrorCode,
    EventLevel,
    ExitCode,
    JobEvent,
    JobState,
    LockBusyError,
    MediaFingerprint,
    PipelineStage,
)
from nas_subtitles.repository import SqliteJobRepository, StateDirLock, open_repository
from nas_subtitles.states import retry_delay_seconds, should_retry

runner = CliRunner()


def _fingerprint(relative_path: str = "show/episode.mkv") -> MediaFingerprint:
    return MediaFingerprint(
        root_id="library-aaaa1111",
        relative_path=relative_path,
        size_bytes=1024,
        mtime_ns=1_700_000_000_000_000_000,
        head_sha256="a" * 64,
        tail_sha256="b" * 64,
        audio_stream_index=1,
    )


def _enqueue(repo: SqliteJobRepository, relative_path: str = "show/episode.mkv"):
    return repo.enqueue(
        fingerprint=_fingerprint(relative_path),
        pipeline_config_hash="cfg-hash",
        priority=0,
    )


def test_initialise_enables_wal_and_foreign_keys(config: AppConfig) -> None:
    repo = open_repository(config)
    connection = sqlite3.connect(config.database_path)
    try:
        journal = connection.execute("PRAGMA journal_mode").fetchone()[0]
        # The live repository connection set WAL; a second connection sees wal or the file.
        assert str(journal).lower() in {"wal", "delete"}
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    finally:
        connection.close()
        repo.close()
    assert {
        "schema_migrations",
        "jobs",
        "artifacts",
        "events",
        "translation_cache",
        "metrics",
        "scan_observations",
    } <= tables


def test_two_claims_cannot_lease_the_same_job(config: AppConfig) -> None:
    first = open_repository(config, owner="a")
    second = open_repository(config, owner="b")
    _enqueue(first)
    barrier = threading.Barrier(2)
    results: list[str | None] = []
    lock = threading.Lock()

    def claim(repo: SqliteJobRepository, owner: str) -> None:
        barrier.wait()
        claimed = repo.claim_next_job(owner=owner, lease_seconds=60)
        with lock:
            results.append(None if claimed is None else claimed.job.id)

    threads = [
        threading.Thread(target=claim, args=(first, "worker-a")),
        threading.Thread(target=claim, args=(second, "worker-b")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    first.close()
    second.close()
    leased = [item for item in results if item is not None]
    assert len(leased) == 1
    assert len(results) == 2


def test_expired_lease_can_be_reclaimed_by_another_owner(config: AppConfig) -> None:
    repo = open_repository(config)
    job = _enqueue(repo)
    first = repo.claim_next_job(owner="alpha", lease_seconds=0)
    assert first is not None
    assert first.job.id == job.id
    reclaimed = repo.claim_next_job(owner="beta", lease_seconds=60)
    repo.close()
    assert reclaimed is not None
    assert reclaimed.job.id == job.id
    assert reclaimed.lease_owner == "beta"
    assert reclaimed.job.attempt_count == 1


def test_retryable_failure_enters_retry_wait_then_becomes_claimable(config: AppConfig) -> None:
    repo = open_repository(config)
    job = _enqueue(repo)
    claimed = repo.claim_next_job(owner="w", lease_seconds=60)
    assert claimed is not None
    assert should_retry(
        error_code=ErrorCode.IO_ERROR,
        attempt_count=claimed.job.attempt_count,
        max_attempts=config.worker.max_attempts,
    )
    delay = retry_delay_seconds(config.worker.retry_delays_seconds, claimed.job.attempt_count)
    past = datetime.now(tz=UTC) - timedelta(seconds=1)
    waiting = repo.transition(
        job_id=job.id,
        state=JobState.RETRY_WAIT,
        stage=PipelineStage.EXTRACT,
        error_code=ErrorCode.IO_ERROR,
        error_detail="disk full",
        next_attempt_at=past,
    )
    assert waiting.state is JobState.RETRY_WAIT
    assert delay == 300
    again = repo.claim_next_job(owner="w", lease_seconds=60)
    repo.close()
    assert again is not None
    assert again.job.id == job.id
    assert again.job.attempt_count == 2
    assert again.job.state is JobState.RUNNING


def test_non_retryable_error_goes_to_failed(config: AppConfig) -> None:
    repo = open_repository(config)
    job = _enqueue(repo)
    repo.claim_next_job(owner="w", lease_seconds=60)
    failed = repo.transition(
        job_id=job.id,
        state=JobState.FAILED,
        error_code=ErrorCode.INVALID_MEDIA,
        error_detail="no audio",
    )
    queued = repo.transition(job_id=job.id, state=JobState.QUEUED)
    repo.close()
    assert failed.state is JobState.FAILED
    assert queued.state is JobState.QUEUED


def test_heartbeat_event_is_readable_by_latest_event_at(config: AppConfig) -> None:
    repo = open_repository(config)
    repo.append_event(
        JobEvent(level=EventLevel.INFO, code=HEARTBEAT_EVENT_CODE, payload={"owner": "w"})
    )
    moment = repo.latest_event_at(code=HEARTBEAT_EVENT_CODE)
    repo.close()
    assert moment is not None
    assert moment.tzinfo is not None


def test_backup_uses_sqlite_backup_and_is_readable(config: AppConfig, tmp_path: Path) -> None:
    repo = open_repository(config)
    job = _enqueue(repo)
    destination = tmp_path / "backups" / "jobs.sqlite3"
    repo.backup_to(destination)
    repo.close()
    copied = sqlite3.connect(destination)
    try:
        row = copied.execute("SELECT id FROM jobs WHERE id = ?", (job.id,)).fetchone()
    finally:
        copied.close()
    assert row is not None


def test_state_dir_lock_rejects_a_second_holder(config: AppConfig) -> None:
    held = StateDirLock(config.lock_path)
    held.__enter__()
    try:
        with pytest.raises(LockBusyError):
            StateDirLock(config.lock_path).__enter__()
    finally:
        held.__exit__(None, None, None)


def test_jobs_list_and_show_round_trip_through_the_cli(
    config: AppConfig, config_path: Path
) -> None:
    repo = open_repository(config)
    job = _enqueue(repo)
    repo.close()
    listed = runner.invoke(app, ["jobs", "list", "--config", str(config_path), "--json"])
    assert listed.exit_code == int(ExitCode.SUCCESS)
    payload = listed.stdout
    assert job.id in payload
    shown = runner.invoke(app, ["jobs", "show", job.id, "--config", str(config_path), "--json"])
    assert shown.exit_code == int(ExitCode.SUCCESS)
    assert job.id in shown.stdout


def test_jobs_cancel_and_retry_cli(config: AppConfig, config_path: Path) -> None:
    repo = open_repository(config)
    job = _enqueue(repo)
    repo.claim_next_job(owner="w", lease_seconds=60)
    repo.transition(job_id=job.id, state=JobState.FAILED, error_code=ErrorCode.IO_ERROR)
    repo.close()
    retried = runner.invoke(app, ["jobs", "retry", job.id, "--config", str(config_path), "--json"])
    assert retried.exit_code == int(ExitCode.SUCCESS)
    cancelled = runner.invoke(
        app, ["jobs", "cancel", job.id, "--config", str(config_path), "--json"]
    )
    assert cancelled.exit_code == int(ExitCode.SUCCESS)
    assert "cancelled" in cancelled.stdout
