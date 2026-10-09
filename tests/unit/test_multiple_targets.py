"""Roadmap 005: one media item can produce independent per-target jobs."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from nas_subtitles.api import DashboardService
from nas_subtitles.config import AppConfig, LanguagesConfig
from nas_subtitles.discovery import (
    enqueue_path,
    enqueue_targets,
    has_target_sidecar,
    observe,
    scan,
)
from nas_subtitles.domain import AudioStreamInfo, JobState, ProbeResult
from nas_subtitles.repository import open_repository
from nas_subtitles.webhooks import ingest_webhook


class _FakeProbe:
    def probe(self, path: Path) -> ProbeResult:
        size = path.stat().st_size if path.is_file() else 0
        return ProbeResult(
            path=path,
            duration_seconds=2.0,
            size_bytes=size,
            audio_streams=(AudioStreamInfo(index=1, codec_name="aac", language="en"),),
        )


def _with_targets(config: AppConfig, *targets: str) -> AppConfig:
    languages = LanguagesConfig(source="auto", targets=targets, low_confidence="review")
    return config.model_copy(update={"languages": languages})


def _fast(config: AppConfig) -> AppConfig:
    return config.model_copy(update={"stability_window_seconds": 60, "minimum_file_age_seconds": 0})


def _stabilize(config: AppConfig, repo: object, path: Path, *, now: datetime) -> None:
    root = config.roots[0]
    age = config.stability_window_seconds + 5
    stamp = now.timestamp() - age
    path.touch()
    import os

    os.utime(path, (stamp, stamp))
    first = now - timedelta(seconds=config.stability_window_seconds + 1)
    observe(repo, root=root, path=path, now=first)  # type: ignore[arg-type]
    observe(repo, root=root, path=path, now=now)  # type: ignore[arg-type]


def test_two_targets_produce_two_independent_jobs(config: AppConfig, media_root: Path) -> None:
    config = _with_targets(config, "pt-BR", "en")
    video = media_root / "episode.mkv"
    video.write_bytes(b"media-bytes")
    repo = open_repository(config)
    queued = enqueue_targets(config, repo, video, require_stability=False, probe=_FakeProbe())
    jobs = repo.list_jobs()
    repo.close()
    assert [item.target_language for item in queued.outcomes] == ["pt-BR", "en"]
    assert {job.target_language for job in jobs} == {"pt-BR", "en"}
    assert len({job.pipeline_config_hash for job in jobs}) == 2
    assert jobs[0].id != jobs[1].id


def test_existing_sidecar_skip_is_per_language(config: AppConfig, media_root: Path) -> None:
    config = _fast(_with_targets(config, "pt-BR", "en"))
    video = media_root / "episode.mkv"
    video.write_bytes(b"media-bytes")
    (media_root / "episode.pt-BR.srt").write_text("keep-pt\n", encoding="utf-8")
    repo = open_repository(config)
    now = datetime.now(tz=UTC)
    _stabilize(config, repo, video, now=now)
    summary = scan(config, repo, now=now, probe=_FakeProbe())
    queued = enqueue_path(config, repo, video, require_stability=False, probe=_FakeProbe())
    jobs = repo.list_jobs()
    repo.close()
    assert summary.skipped_existing_subtitle == 1
    assert summary.enqueued == 1
    assert queued[0] is not None
    assert queued[0].target_language == "en"
    assert queued[1] is None
    languages = {job.target_language: job.state for job in jobs}
    assert languages["pt-BR"] is JobState.SKIPPED
    assert languages["en"] is JobState.QUEUED
    assert (media_root / "episode.pt-BR.srt").read_text(encoding="utf-8") == "keep-pt\n"
    assert has_target_sidecar(video, "en") is False


def test_english_sidecar_does_not_skip_portuguese_target(
    config: AppConfig, media_root: Path
) -> None:
    config = _with_targets(config, "pt-BR", "en")
    video = media_root / "episode.mkv"
    video.write_bytes(b"media-bytes")
    (media_root / "episode.en.srt").write_text("keep-en\n", encoding="utf-8")
    repo = open_repository(config)
    queued = enqueue_targets(config, repo, video, require_stability=False, probe=_FakeProbe())
    repo.close()
    by_target = {item.target_language: item for item in queued.outcomes}
    assert by_target["en"].skip_reason is not None
    assert "en" in by_target["en"].skip_reason
    assert by_target["pt-BR"].job is not None
    assert by_target["pt-BR"].skip_reason is None


def test_dashboard_and_webhook_expose_each_job_target(config: AppConfig, media_root: Path) -> None:
    config = _with_targets(config, "pt-BR", "en")
    video = media_root / "Show.S01E01.mkv"
    video.write_bytes(b"x" * 32)
    repo = open_repository(config)
    result = ingest_webhook(
        config,
        repo,
        {
            "eventType": "Download",
            "instanceName": "Sonarr",
            "episodeFile": {"path": str(video), "relativePath": video.name, "size": 32},
        },
        probe=_FakeProbe(),
    )
    service = DashboardService(config, repo)
    listed = service.list_jobs(view="queue")
    settings = service.settings()
    jobs = repo.list_jobs()
    repo.close()
    assert result["action"] == "enqueued"
    assert {item["target_language"] for item in result["results"][0]["jobs"]} == {"pt-BR", "en"}
    assert {job["target_language"] for job in listed["jobs"]} == {"pt-BR", "en"}
    assert settings["languages"]["targets"] == ["pt-BR", "en"]
    assert {job.target_language for job in jobs} == {"pt-BR", "en"}
    assert all("pb" not in str(job.target_language) for job in jobs)
