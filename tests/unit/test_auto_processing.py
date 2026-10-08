"""Automatic processing: reconciliation, stability, skip policy, recovery, sidecar."""

from __future__ import annotations

import errno
import logging
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from nas_subtitles.cli import app
from nas_subtitles.config import AppConfig
from nas_subtitles.discovery import (
    canonical_target_sidecar,
    enqueue_path,
    find_existing_subtitles,
    has_portuguese_subtitle,
    has_target_sidecar,
    is_candidate_name,
    is_stable,
    is_temporary_name,
    observe,
    scan,
)
from nas_subtitles.domain import (
    AudioStreamInfo,
    ErrorCode,
    JobState,
    MediaFingerprint,
    NasSubtitlesError,
    PipelineStage,
    ProbeResult,
    PublishOutcome,
    QualityReport,
)
from nas_subtitles.output import publish_exclusive, sidecar_path_for
from nas_subtitles.pipeline import PipelineResult
from nas_subtitles.repository import open_repository
from nas_subtitles.worker import Worker


class _FakeProbe:
    def probe(self, path: Path) -> ProbeResult:
        size = path.stat().st_size if path.is_file() else 0
        return ProbeResult(
            path=path,
            duration_seconds=2.0,
            size_bytes=size,
            audio_streams=(AudioStreamInfo(index=1, codec_name="aac", language="en"),),
        )


def _fingerprint(relative_path: str) -> MediaFingerprint:
    return MediaFingerprint(
        root_id="library-aaaa1111",
        relative_path=relative_path,
        size_bytes=1024,
        mtime_ns=1_700_000_000_000_000_000,
        head_sha256="a" * 64,
        tail_sha256="b" * 64,
        audio_stream_index=1,
    )


def _fast_config(config: AppConfig) -> AppConfig:
    return config.model_copy(update={"stability_window_seconds": 60, "minimum_file_age_seconds": 0})


def _stabilize(config: AppConfig, repo: object, path: Path, *, now: datetime) -> None:
    root = config.roots[0]
    age = config.stability_window_seconds + 5
    stamp = now.timestamp() - age
    os.utime(path, (stamp, stamp))
    first = now - timedelta(seconds=config.stability_window_seconds + 1)
    observe(repo, root=root, path=path, now=first)  # type: ignore[arg-type]
    observe(repo, root=root, path=path, now=now)  # type: ignore[arg-type]


def test_initial_scan_discovers_eligible_media(config: AppConfig, media_root: Path) -> None:
    config = _fast_config(config)
    video = media_root / "episode.mkv"
    video.write_bytes(b"media-bytes")
    repo = open_repository(config)
    now = datetime.now(tz=UTC)
    _stabilize(config, repo, video, now=now)
    summary = scan(config, repo, now=now, probe=_FakeProbe())
    repo.close()
    assert summary.examined == 1
    assert summary.enqueued == 1
    assert summary.enqueued_paths == ("episode.mkv",)


def test_newly_added_media_is_discovered_on_later_scan(config: AppConfig, media_root: Path) -> None:
    config = _fast_config(config)
    repo = open_repository(config)
    now = datetime.now(tz=UTC)
    first = scan(config, repo, now=now, probe=_FakeProbe())
    assert first.examined == 0
    video = media_root / "new.mkv"
    video.write_bytes(b"media-bytes")
    later = now + timedelta(seconds=1)
    second = scan(config, repo, now=later, probe=_FakeProbe())
    repo.close()
    assert second.examined == 1
    assert second.enqueued == 0
    assert second.skipped_unstable + second.skipped_too_young >= 1


def test_unsupported_and_subtitle_and_temporary_files_are_ignored(
    config: AppConfig, media_root: Path
) -> None:
    config = _fast_config(config)
    (media_root / "notes.txt").write_text("no", encoding="utf-8")
    (media_root / "episode.srt").write_text("sub", encoding="utf-8")
    (media_root / "film.mkv.part").write_bytes(b"partial")
    (media_root / "film.mkv.partial").write_bytes(b"partial")
    (media_root / "film.mkv.!qB").write_bytes(b"qb")
    repo = open_repository(config)
    summary = scan(config, repo, now=datetime.now(tz=UTC), probe=_FakeProbe())
    jobs = repo.list_jobs()
    repo.close()
    assert summary.examined == 0
    assert summary.enqueued == 0
    assert summary.skipped_temporary >= 3
    assert summary.skipped_unsupported >= 2
    assert jobs == ()
    assert is_temporary_name(Path("film.mkv.part"))
    assert not is_candidate_name(Path("episode.srt"))


def test_newly_observed_file_is_not_immediately_processed(
    config: AppConfig, media_root: Path
) -> None:
    config = _fast_config(config)
    video = media_root / "fresh.mkv"
    video.write_bytes(b"media-bytes")
    repo = open_repository(config)
    summary = scan(config, repo, now=datetime.now(tz=UTC), probe=_FakeProbe())
    jobs = repo.list_jobs()
    repo.close()
    assert summary.enqueued == 0
    assert jobs == ()
    assert summary.skipped_unstable + summary.skipped_too_young >= 1


def test_unchanged_size_and_mtime_become_eligible_after_window(
    config: AppConfig, media_root: Path
) -> None:
    config = _fast_config(config)
    video = media_root / "stable.mkv"
    video.write_bytes(b"media-bytes")
    repo = open_repository(config)
    now = datetime.now(tz=UTC)
    _stabilize(config, repo, video, now=now)
    observation = observe(repo, root=config.roots[0], path=video, now=now)
    assert is_stable(config, observation, now=now) is True
    summary = scan(config, repo, now=now, probe=_FakeProbe())
    repo.close()
    assert summary.enqueued == 1


def test_size_change_resets_stability(config: AppConfig, media_root: Path) -> None:
    config = _fast_config(config)
    video = media_root / "growing.mkv"
    video.write_bytes(b"a" * 32)
    repo = open_repository(config)
    now = datetime.now(tz=UTC)
    _stabilize(config, repo, video, now=now)
    video.write_bytes(b"a" * 64)
    later = now + timedelta(seconds=1)
    observation = observe(repo, root=config.roots[0], path=video, now=later)
    repo.close()
    assert observation.first_stable_seen_at == later
    assert is_stable(config, observation, now=later) is False


def test_mtime_change_resets_stability(config: AppConfig, media_root: Path) -> None:
    config = _fast_config(config)
    video = media_root / "touched.mkv"
    video.write_bytes(b"media-bytes")
    repo = open_repository(config)
    now = datetime.now(tz=UTC)
    _stabilize(config, repo, video, now=now)
    later_ts = now.timestamp() + 10
    os.utime(video, (later_ts, later_ts))
    later = now + timedelta(seconds=10)
    observation = observe(repo, root=config.roots[0], path=video, now=later)
    repo.close()
    assert observation.first_stable_seen_at == later
    assert is_stable(config, observation, now=later) is False


def test_unstable_file_is_not_queued(config: AppConfig, media_root: Path) -> None:
    config = _fast_config(config)
    video = media_root / "unstable.mkv"
    video.write_bytes(b"media-bytes")
    repo = open_repository(config)
    now = datetime.now(tz=UTC)
    observe(repo, root=config.roots[0], path=video, now=now)
    summary = scan(config, repo, now=now, probe=_FakeProbe())
    repo.close()
    assert summary.enqueued == 0
    assert summary.skipped_unstable >= 1


def test_existing_pt_br_sidecar_skips_and_is_not_overwritten(
    config: AppConfig, media_root: Path, caplog: pytest.LogCaptureFixture
) -> None:
    config = _fast_config(config)
    video = media_root / "episode.mkv"
    video.write_bytes(b"media-bytes")
    sidecar = canonical_target_sidecar(video, config.target_language)
    sidecar.write_text("keep-me", encoding="utf-8")
    repo = open_repository(config)
    now = datetime.now(tz=UTC)
    _stabilize(config, repo, video, now=now)
    with caplog.at_level(logging.INFO):
        summary = scan(config, repo, now=now, probe=_FakeProbe())
        skipped = enqueue_path(
            config, repo, video, require_stability=False, now=now, probe=_FakeProbe()
        )
    jobs = repo.list_jobs()
    repo.close()
    assert summary.enqueued == 0
    assert summary.skipped_existing_subtitle == 1
    assert skipped == (None, f"existing target sidecar ({config.target_language})")
    assert sidecar.read_text(encoding="utf-8") == "keep-me"
    assert len(jobs) == 1
    assert jobs[0].state is JobState.SKIPPED
    assert has_target_sidecar(video, "pt-BR") is True
    events = [record.getMessage() for record in caplog.records]
    assert "existing target subtitle found" in events
    assert "media skipped" in events


def test_pt_br_filename_is_recognized_and_pb_is_not_a_sidecar(
    config: AppConfig, media_root: Path
) -> None:
    video = media_root / "Dexter.S03E01.mkv"
    video.write_bytes(b"x")
    (media_root / "Dexter.S03E01.pb.srt").write_text("argos-code", encoding="utf-8")
    found = find_existing_subtitles(path=video)
    assert has_portuguese_subtitle(found) is False
    assert has_target_sidecar(video, "pt-BR") is False
    (media_root / "Dexter.S03E01.pt-BR.srt").write_text("ok", encoding="utf-8")
    found = find_existing_subtitles(path=video)
    assert has_portuguese_subtitle(found) is True
    sidecar = sidecar_path_for(config, config.roots[0], "Dexter.S03E01.mkv")
    assert sidecar.name == "Dexter.S03E01.pt-BR.srt"
    assert ".pb." not in sidecar.name
    assert not sidecar.name.endswith(".pb.srt")


def test_repeated_scan_does_not_duplicate_work(config: AppConfig, media_root: Path) -> None:
    config = _fast_config(config)
    video = media_root / "once.mkv"
    video.write_bytes(b"media-bytes")
    repo = open_repository(config)
    now = datetime.now(tz=UTC)
    _stabilize(config, repo, video, now=now)
    first = scan(config, repo, now=now, probe=_FakeProbe())
    second = scan(config, repo, now=now + timedelta(seconds=1), probe=_FakeProbe())
    jobs = repo.list_jobs()
    repo.close()
    assert first.enqueued == 1
    assert second.enqueued == 0
    assert second.already_queued == 1
    assert len(jobs) == 1


def test_completed_job_is_not_repeated_after_restart(config: AppConfig, media_root: Path) -> None:
    config = _fast_config(config)
    video = media_root / "done.mkv"
    video.write_bytes(b"media-bytes")
    repo = open_repository(config)
    now = datetime.now(tz=UTC)
    _stabilize(config, repo, video, now=now)
    scan(config, repo, now=now, probe=_FakeProbe())
    job = repo.list_jobs()[0]
    repo.transition(job_id=job.id, state=JobState.RUNNING)
    repo.transition(job_id=job.id, state=JobState.COMPLETED)
    worker = Worker(config, repo, owner="test")
    recovered = worker.recover_interrupted_jobs()
    again = scan(config, repo, now=now + timedelta(seconds=2), probe=_FakeProbe())
    jobs = repo.list_jobs()
    repo.close()
    assert recovered == 0
    assert again.enqueued == 0
    assert len(jobs) == 1
    assert jobs[0].state is JobState.COMPLETED


def test_eligible_media_is_queued_for_existing_worker(config: AppConfig, media_root: Path) -> None:
    config = _fast_config(config)
    video = media_root / "queued.mkv"
    video.write_bytes(b"media-bytes")
    repo = open_repository(config)
    now = datetime.now(tz=UTC)
    _stabilize(config, repo, video, now=now)
    scan(config, repo, now=now, probe=_FakeProbe())
    worker = Worker(config, repo, owner="test")
    claim = repo.claim_next_job(owner=worker.owner, lease_seconds=60)
    repo.close()
    assert claim is not None
    assert claim.job.state is JobState.RUNNING
    assert claim.job.relative_path == "queued.mkv"


def test_one_failed_job_does_not_stop_later_job(
    config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = open_repository(config)
    first = repo.enqueue(fingerprint=_fingerprint("a.mkv"), pipeline_config_hash="cfg")
    second = repo.enqueue(fingerprint=_fingerprint("b.mkv"), pipeline_config_hash="cfg")

    def factory(cfg: AppConfig, repository: object, job: object, stop: object) -> object:
        del cfg, repository, stop
        return job

    def fake_run(context: object) -> PipelineResult:
        job_id = context.id  # type: ignore[attr-defined]
        if job_id == first.id:
            raise NasSubtitlesError("bad media", code=ErrorCode.INVALID_MEDIA)
        repo.transition(job_id=job_id, state=JobState.COMPLETED)
        return PipelineResult(
            job_id=job_id,
            state=JobState.COMPLETED,
            last_stage=PipelineStage.VALIDATE,
            quality=QualityReport(),
        )

    monkeypatch.setattr("nas_subtitles.worker.run_job", fake_run)
    worker = Worker(config, repo, owner="test", context_factory=factory)
    assert worker.run_once() is True
    assert worker.run_once() is True
    assert repo.get_job(first.id).state is JobState.FAILED  # type: ignore[union-attr]
    assert repo.get_job(second.id).state is JobState.COMPLETED  # type: ignore[union-attr]
    repo.close()


def test_interrupted_running_job_is_requeued_on_recover(config: AppConfig) -> None:
    repo = open_repository(config)
    job = repo.enqueue(fingerprint=_fingerprint("crash.mkv"), pipeline_config_hash="cfg")
    claimed = repo.claim_next_job(owner="old-worker", lease_seconds=600)
    assert claimed is not None
    assert claimed.job.state is JobState.RUNNING
    worker = Worker(config, repo, owner="new-worker")
    recovered = worker.recover_interrupted_jobs()
    restored = repo.get_job(job.id)
    repo.close()
    assert recovered == 1
    assert restored is not None
    assert restored.state is JobState.QUEUED
    assert restored.error_code is ErrorCode.INTERRUPTED


def test_sidecar_naming_is_logical_target_language(config: AppConfig) -> None:
    sidecar = sidecar_path_for(config, config.roots[0], "show/Dexter.S03E01.mkv")
    assert sidecar.name == "Dexter.S03E01.pt-BR.srt"
    assert config.target_language == "pt-BR"


def test_publication_is_atomic_and_temp_is_not_final(tmp_path: Path) -> None:
    target = tmp_path / "episode.pt-BR.srt"
    result = publish_exclusive(content="1\n00:00:00,000 --> 00:00:01,000\nOi\n", target=target)
    assert result.outcome is PublishOutcome.PUBLISHED
    assert target.is_file()
    leftovers = [path.name for path in tmp_path.iterdir() if path.suffix == ".tmp"]
    assert leftovers == []
    hidden = tmp_path / ".episode.pt-BR.srt.tmp"
    hidden.write_text("partial", encoding="utf-8")
    assert has_target_sidecar(tmp_path / "episode.mkv", "pt-BR") is True
    assert not has_target_sidecar(tmp_path / "other.mkv", "pt-BR")


def test_failed_publication_does_not_leave_partial_final(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "episode.pt-BR.srt"

    real_link = os.link

    def boom(source: str | os.PathLike[str], dest: str | os.PathLike[str]) -> None:
        if Path(dest).name == "episode.pt-BR.srt":
            raise OSError(errno.EIO, "disk failed")
        real_link(source, dest)

    monkeypatch.setattr(os, "link", boom)
    with pytest.raises(NasSubtitlesError) as raised:
        publish_exclusive(content="partial-body\n", target=target)
    assert raised.value.code is ErrorCode.IO_ERROR
    assert not target.exists()
    assert list(tmp_path.iterdir()) == []


def test_publish_does_not_modify_source_media(config: AppConfig, media_root: Path) -> None:
    video = media_root / "keep.mkv"
    video.write_bytes(b"original-media")
    before = video.read_bytes()
    stat_before = video.stat()
    sidecar = sidecar_path_for(config, config.roots[0], "keep.mkv")
    publish_exclusive(content="1\n", target=sidecar)
    assert video.read_bytes() == before
    assert video.stat().st_mtime_ns == stat_before.st_mtime_ns
    assert sidecar.is_file()


def test_sidecar_appearing_during_skip_policy_is_not_overwritten(
    config: AppConfig, media_root: Path
) -> None:
    video = media_root / "race.mkv"
    video.write_bytes(b"media-bytes")
    sidecar = canonical_target_sidecar(video, "pt-BR")
    sidecar.write_text("already-there", encoding="utf-8")
    result = publish_exclusive(content="replacement\n", target=sidecar)
    assert result.outcome is PublishOutcome.CONFLICT
    assert sidecar.read_text(encoding="utf-8") == "already-there"


def test_daemon_command_starts_help_and_worker_remains(
    config_path: Path,
) -> None:
    runner = CliRunner()
    daemon = runner.invoke(app, ["daemon", "--help"])
    worker = runner.invoke(app, ["worker", "--help"])
    process = runner.invoke(app, ["process", "--help"])
    assert daemon.exit_code == 0
    assert worker.exit_code == 0
    assert process.exit_code == 0
    assert "--json" in daemon.output
    assert "--preview-seconds" in process.output
    doctor = runner.invoke(app, ["doctor", "--config", str(config_path), "--json"])
    assert doctor.exit_code in {0, 3}
