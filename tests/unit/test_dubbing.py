"""Roadmap 006 phase 1: job_kind, CLI, persistence, existing-subtitle isolation."""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from nas_subtitles.cli import app
from nas_subtitles.config import AppConfig
from nas_subtitles.discovery import enqueue_targets, observe, scan
from nas_subtitles.domain import (
    DB_SCHEMA_VERSION,
    DUBBING_PLAN_SCHEMA_VERSION,
    AudioChunk,
    AudioStreamInfo,
    DubbingProfile,
    DubSegment,
    DubSegmentReviewState,
    ErrorCode,
    JobExecutionScope,
    JobKind,
    JobRecord,
    JobState,
    LanguageDecision,
    LanguageSample,
    LanguageSource,
    MediaFingerprint,
    ModelIdentity,
    ModelKind,
    NasSubtitlesError,
    PipelineStage,
    ProbeResult,
    infer_job_kind,
    stage_window,
    stages_for,
)
from nas_subtitles.dubbing import (
    apply_plan,
    enqueue_dubbing,
    export_plan,
    parse_dubbing_profile,
    plan_payload,
)
from nas_subtitles.pipeline import StageContext, run_job
from nas_subtitles.repository import open_repository

runner = CliRunner()


class _FakeProbe:
    def probe(self, path: Path) -> ProbeResult:
        size = path.stat().st_size if path.is_file() else 0
        return ProbeResult(
            path=path,
            duration_seconds=2.0,
            size_bytes=size,
            audio_streams=(AudioStreamInfo(index=1, codec_name="aac", language="en"),),
        )


def _fast(config: AppConfig) -> AppConfig:
    return config.model_copy(update={"stability_window_seconds": 60, "minimum_file_age_seconds": 0})


class _FakeExtractor:
    def __init__(self) -> None:
        self.calls = 0

    def extract(
        self, *, source: Path, stream_index: int, spec: object, destination: Path
    ) -> AudioChunk:
        del source, stream_index
        self.calls += 1
        return AudioChunk(spec=spec, path=destination)  # type: ignore[arg-type]


class _FakeDubTranscriber:
    """Only ``detect_language`` is exercised: nothing past it is implemented."""

    def __init__(self, *, language: str = "en", probability: float = 0.95) -> None:
        self.language = language
        self.probability = probability

    @property
    def model_identity(self) -> ModelIdentity:
        return ModelIdentity(kind=ModelKind.ASR, name="fake", path=Path("/models/fake"))

    def detect_language(self, samples: object) -> LanguageDecision:
        collected = tuple(
            LanguageSample(
                offset_seconds=chunk.spec.extract_start_seconds,
                duration_seconds=chunk.spec.extract_duration_seconds,
                language=self.language,
                probability=self.probability,
                has_speech=self.probability >= 0.15,
            )
            for chunk in samples  # type: ignore[attr-defined]
        )
        confident = self.probability >= 0.80
        return LanguageDecision(
            language=self.language if confident else None,
            source=LanguageSource.DETECTION,
            confident=confident,
            probability=self.probability,
            samples=collected,
        )

    def transcribe(self, chunk: object, *, language: str) -> object:
        raise AssertionError("transcribe must not run: roadmap 006 stops at detect_language")


def _dub_context(
    config: AppConfig,
    repo: object,
    job: JobRecord,
    *,
    transcriber: object | None = None,
    extractor: object | None = None,
) -> StageContext:
    return StageContext(
        config=config,
        repository=repo,  # type: ignore[arg-type]
        job=job,
        probe=_FakeProbe(),
        extractor=extractor or _FakeExtractor(),  # type: ignore[arg-type]
        transcriber=transcriber or _FakeDubTranscriber(),  # type: ignore[arg-type]
        translator=None,  # type: ignore[arg-type]
        renderer=None,  # type: ignore[arg-type]
    )


def test_schema_version_is_four_after_migrate(config: AppConfig) -> None:
    repo = open_repository(config)
    connection = sqlite3.connect(config.database_path)
    try:
        version = connection.execute("SELECT max(version) FROM schema_migrations").fetchone()[0]
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    finally:
        connection.close()
        repo.close()
    assert version == DB_SCHEMA_VERSION
    assert {"dub_segments", "voice_assignments", "synthesis_artifacts"} <= tables


def test_legacy_jobs_become_subtitles(config: AppConfig) -> None:
    database = config.database_path
    database.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database)
    try:
        initial = (
            Path(__file__).resolve().parents[2] / "src/nas_subtitles/migrations/001_initial.sql"
        )
        connection.executescript(initial.read_text(encoding="utf-8"))
        now = datetime.now(tz=UTC).isoformat()
        fingerprint = json.dumps(
            {
                "audio_stream_index": 1,
                "head_sha256": "a",
                "mtime_ns": 1,
                "relative_path": "show/a.mkv",
                "root_id": "library-aaaa1111",
                "size_bytes": 1,
                "tail_sha256": "b",
            },
            sort_keys=True,
        )
        connection.execute(
            """
            INSERT INTO jobs (
                id, root_id, relative_path, fingerprint, pipeline_config_hash,
                state, priority, attempt_count, created_at, updated_at
            ) VALUES ('legacy-1', 'library-aaaa1111', 'show/a.mkv', ?, 'h',
                      'queued', 0, 0, ?, ?)
            """,
            (fingerprint, now, now),
        )
        connection.commit()
    finally:
        connection.close()
    repo = open_repository(config)
    job = repo.require_job("legacy-1")
    repo.close()
    assert infer_job_kind(job.job_kind) is JobKind.SUBTITLES


def test_subtitle_and_dubbing_jobs_are_distinct_identities(
    config: AppConfig, media_root: Path
) -> None:
    video = media_root / "episode.mkv"
    video.write_bytes(b"media-bytes")
    (media_root / "episode.pt-BR.srt").write_text("existing\n", encoding="utf-8")
    repo = open_repository(config)
    subtitles = enqueue_targets(config, repo, video, require_stability=False, probe=_FakeProbe())
    dubbed = enqueue_dubbing(config, repo, video, require_stability=False, probe=_FakeProbe())
    jobs = repo.list_jobs()
    repo.close()
    assert not subtitles.jobs
    assert subtitles.skip_reasons
    assert dubbed.job.job_kind is JobKind.DUBBING
    assert dubbed.job.state is JobState.QUEUED
    assert {infer_job_kind(job.job_kind) for job in jobs} == {JobKind.DUBBING}
    assert dubbed.job.pipeline_config_hash != config.pipeline_config_hash


def test_scanner_does_not_enqueue_dubbing_jobs(config: AppConfig, media_root: Path) -> None:
    config = _fast(config)
    video = media_root / "episode.mkv"
    video.write_bytes(b"media-bytes")
    repo = open_repository(config)
    now = datetime.now(tz=UTC)
    root = config.roots[0]
    stamp = now.timestamp() - config.stability_window_seconds - 5
    os.utime(video, (stamp, stamp))
    observe(repo, root=root, path=video, now=now - timedelta(seconds=70))
    observe(repo, root=root, path=video, now=now)
    summary = scan(config, repo, now=now, probe=_FakeProbe())
    jobs = repo.list_jobs()
    repo.close()
    assert summary.enqueued == 1
    assert all(infer_job_kind(job.job_kind) is JobKind.SUBTITLES for job in jobs)


def test_dubbing_enqueue_reuses_the_same_full_identity(config: AppConfig, media_root: Path) -> None:
    video = media_root / "episode.mkv"
    video.write_bytes(b"media-bytes")
    repo = open_repository(config)
    first = enqueue_dubbing(config, repo, video, require_stability=False, probe=_FakeProbe())
    second = enqueue_dubbing(config, repo, video, require_stability=False, probe=_FakeProbe())
    repo.close()
    assert first.job.id == second.job.id


def test_plan_export_does_not_overwrite(
    config: AppConfig, media_root: Path, tmp_path: Path
) -> None:
    video = media_root / "episode.mkv"
    video.write_bytes(b"media-bytes")
    repo = open_repository(config)
    queued = enqueue_dubbing(config, repo, video, require_stability=False, probe=_FakeProbe())
    destination = tmp_path / "plan.json"
    export_plan(repo, queued.job.id, destination=destination)
    payload = json.loads(destination.read_text(encoding="utf-8"))
    assert payload["schema_version"] == DUBBING_PLAN_SCHEMA_VERSION
    assert payload["job_id"] == queued.job.id
    assert payload["segments"] == []
    with pytest.raises(NasSubtitlesError) as raised:
        export_plan(repo, queued.job.id, destination=destination)
    repo.close()
    assert raised.value.code is ErrorCode.OUTPUT_CONFLICT


def test_plan_apply_increments_revision(
    config: AppConfig, media_root: Path, tmp_path: Path
) -> None:
    video = media_root / "episode.mkv"
    video.write_bytes(b"media-bytes")
    repo = open_repository(config)
    queued = enqueue_dubbing(config, repo, video, require_stability=False, probe=_FakeProbe())
    source = tmp_path / "edited.json"
    source.write_text(
        json.dumps(
            {
                "schema_version": DUBBING_PLAN_SCHEMA_VERSION,
                "job_id": queued.job.id,
                "revision": 0,
                "segments": [
                    {
                        "id": "seg-0000",
                        "start_seconds": 1.0,
                        "end_seconds": 2.5,
                        "original_text": "hello",
                        "translated_text": "olá",
                        "adapted_text": "oi",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    job, segments = apply_plan(repo, queued.job.id, source=source)
    repo.close()
    assert job.id == queued.job.id
    assert len(segments) == 1
    assert segments[0].revision == 1
    assert segments[0].adapted_text == "oi"
    assert segments[0].review_state is DubSegmentReviewState.PENDING


def test_plan_apply_rejects_stale_revision(
    config: AppConfig, media_root: Path, tmp_path: Path
) -> None:
    video = media_root / "episode.mkv"
    video.write_bytes(b"media-bytes")
    repo = open_repository(config)
    queued = enqueue_dubbing(config, repo, video, require_stability=False, probe=_FakeProbe())
    repo.replace_dub_plan(
        job_id=queued.job.id,
        revision=1,
        segments=(
            DubSegment(
                segment_id="seg-0000",
                job_id=queued.job.id,
                revision=1,
                start_seconds=0.0,
                end_seconds=1.0,
            ),
        ),
    )
    source = tmp_path / "stale.json"
    source.write_text(
        json.dumps(
            {
                "schema_version": DUBBING_PLAN_SCHEMA_VERSION,
                "job_id": queued.job.id,
                "revision": 0,
                "segments": [
                    {"id": "seg-0000", "start_seconds": 0.0, "end_seconds": 1.0},
                ],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(NasSubtitlesError) as raised:
        apply_plan(repo, queued.job.id, source=source)
    repo.close()
    assert raised.value.code is ErrorCode.CHECKPOINT_INVALID


def test_dubbing_process_stop_after_probe_does_not_require_engines(
    config: AppConfig, media_root: Path
) -> None:
    video = media_root / "episode.mkv"
    video.write_bytes(b"media-bytes")
    (media_root / "episode.pt-BR.srt").write_text("existing\n", encoding="utf-8")
    repo = open_repository(config)
    queued = enqueue_dubbing(config, repo, video, require_stability=False, probe=_FakeProbe())

    class _Extractor:
        def plan_chunks(self, **kwargs: object) -> tuple[object, ...]:
            del kwargs
            return ()

        def extract(self, **kwargs: object) -> object:
            raise AssertionError("extract must not run when stop_after=probe")

    context = StageContext(
        config=config,
        repository=repo,
        job=queued.job,
        probe=_FakeProbe(),
        extractor=_Extractor(),  # type: ignore[arg-type]
        transcriber=None,  # type: ignore[arg-type]
        translator=None,  # type: ignore[arg-type]
        renderer=None,  # type: ignore[arg-type]
    )
    result = run_job(context, stop_after=PipelineStage.PROBE)
    stored = repo.require_job(queued.job.id)
    repo.close()
    assert result.last_stage is PipelineStage.PROBE
    assert stored.state is JobState.RUNNING
    assert stored.job_kind is JobKind.DUBBING


def test_dubbing_detect_language_confident_stops_there_when_asked(
    config: AppConfig, media_root: Path
) -> None:
    video = media_root / "episode.mkv"
    video.write_bytes(b"media-bytes")
    repo = open_repository(config)
    queued = enqueue_dubbing(config, repo, video, require_stability=False, probe=_FakeProbe())
    transcriber = _FakeDubTranscriber(language="en", probability=0.93)
    context = _dub_context(config, repo, queued.job, transcriber=transcriber)
    result = run_job(context, stop_after=PipelineStage.DETECT_LANGUAGE)
    stored = repo.require_job(queued.job.id)
    events = repo.list_events(job_id=queued.job.id)
    repo.close()
    assert result.last_stage is PipelineStage.DETECT_LANGUAGE
    assert stored.state is JobState.RUNNING
    assert [event.code for event in events] == ["language_decision"]
    assert events[0].payload["detected_language"] == "en"
    assert events[0].payload["confident"] is True


def test_dubbing_low_confidence_goes_to_needs_review(config: AppConfig, media_root: Path) -> None:
    video = media_root / "episode.mkv"
    video.write_bytes(b"media-bytes")
    repo = open_repository(config)
    queued = enqueue_dubbing(config, repo, video, require_stability=False, probe=_FakeProbe())
    transcriber = _FakeDubTranscriber(language="en", probability=0.2)
    context = _dub_context(config, repo, queued.job, transcriber=transcriber)
    result = run_job(context)
    stored = repo.require_job(queued.job.id)
    repo.close()
    assert result.state is JobState.NEEDS_REVIEW
    assert result.last_stage is PipelineStage.DETECT_LANGUAGE
    assert stored.error_code is ErrorCode.LANGUAGE_UNDETERMINED


def test_dubbing_unsupported_language_fails(config: AppConfig, media_root: Path) -> None:
    video = media_root / "episode.mkv"
    video.write_bytes(b"media-bytes")
    repo = open_repository(config)
    queued = enqueue_dubbing(
        config, repo, video, require_stability=False, probe=_FakeProbe(), source_language="ja"
    )
    context = _dub_context(config, repo, queued.job)
    result = run_job(context)
    stored = repo.require_job(queued.job.id)
    repo.close()
    assert result.state is JobState.FAILED
    assert result.last_stage is PipelineStage.DETECT_LANGUAGE
    assert stored.error_code is ErrorCode.UNSUPPORTED_LANGUAGE


def test_dubbing_extract_stops_there_when_asked(config: AppConfig, media_root: Path) -> None:
    video = media_root / "episode.mkv"
    video.write_bytes(b"media-bytes")
    repo = open_repository(config)
    queued = enqueue_dubbing(config, repo, video, require_stability=False, probe=_FakeProbe())
    extractor = _FakeExtractor()
    context = _dub_context(config, repo, queued.job, extractor=extractor)
    result = run_job(context, stop_after=PipelineStage.EXTRACT)
    stored = repo.require_job(queued.job.id)
    repo.close()
    assert result.last_stage is PipelineStage.EXTRACT
    assert stored.state is JobState.RUNNING
    # 2 language-detection samples (duration=2s -> offsets 0.0 and 1.0) + 1
    # real chunk (2s fits under the default 300s chunk_seconds).
    assert extractor.calls == 3


def test_dubbing_beyond_extract_is_still_not_implemented(
    config: AppConfig, media_root: Path
) -> None:
    video = media_root / "episode.mkv"
    video.write_bytes(b"media-bytes")
    repo = open_repository(config)
    queued = enqueue_dubbing(config, repo, video, require_stability=False, probe=_FakeProbe())
    context = _dub_context(config, repo, queued.job)
    with pytest.raises(NasSubtitlesError) as excinfo:
        run_job(context)
    repo.close()
    assert excinfo.value.code is ErrorCode.NOT_IMPLEMENTED
    assert excinfo.value.detail == {"stage": "separate"}


def test_cli_dub_enqueue_json(config: AppConfig, media_root: Path, config_path: Path) -> None:
    video = media_root / "episode.mkv"
    video.write_bytes(b"media-bytes")
    result = runner.invoke(
        app, ["dub", "enqueue", str(video), "--config", str(config_path), "--json"]
    )
    assert result.exit_code != 0
    assert "Traceback" not in result.output
    payload = json.loads(result.stderr)
    assert payload["error_code"]


def test_dubbing_hash_does_not_change_subtitle_hash(config: AppConfig) -> None:
    changed = config.model_copy(
        update={"dubbing": config.dubbing.model_copy(update={"voice": "other-voice"})}
    )
    assert changed.pipeline_config_hash == config.pipeline_config_hash
    assert changed.pipeline_config_hash_for("pt-BR", job_kind=JobKind.DUBBING) != (
        config.pipeline_config_hash_for("pt-BR", job_kind=JobKind.DUBBING)
    )


def test_stage_window_and_kind_sequences() -> None:
    assert stages_for(JobKind.SUBTITLES)[0] is PipelineStage.PROBE
    assert stages_for(JobKind.SUBTITLES)[-1] is PipelineStage.PUBLISH
    assert PipelineStage.RENDER in stages_for(JobKind.SUBTITLES)
    assert PipelineStage.RENDER not in stages_for(JobKind.DUBBING)
    assert PipelineStage.SYNTHESIZE in stages_for(JobKind.DUBBING)
    window = stage_window(
        stages_for(JobKind.DUBBING),
        start_stage=PipelineStage.PROBE,
        stop_after=PipelineStage.SEPARATE,
    )
    assert window[0] is PipelineStage.PROBE
    assert window[-1] is PipelineStage.SEPARATE


def test_plan_payload_omits_filesystem_paths() -> None:
    now = datetime.now(tz=UTC)
    job = JobRecord(
        id="job-1",
        root_id="library-aaaa1111",
        relative_path="show/a.mkv",
        fingerprint=MediaFingerprint(
            root_id="library-aaaa1111",
            relative_path="show/a.mkv",
            size_bytes=1,
            mtime_ns=1,
            head_sha256="a",
            tail_sha256="b",
        ),
        pipeline_config_hash="h",
        state=JobState.QUEUED,
        created_at=now,
        updated_at=now,
        execution_scope=JobExecutionScope.FULL,
        job_kind=JobKind.DUBBING,
        target_language="pt-BR",
    )
    payload = plan_payload(job, ())
    dumped = json.dumps(payload)
    assert "/media" not in dumped
    assert payload["job_kind"] == "dubbing"


def test_unknown_profile_is_config_invalid() -> None:
    with pytest.raises(NasSubtitlesError) as raised:
        parse_dubbing_profile("gpu-dream")
    assert raised.value.code is ErrorCode.CONFIG_INVALID
    assert parse_dubbing_profile("cpu-fixed") is DubbingProfile.CPU_FIXED
