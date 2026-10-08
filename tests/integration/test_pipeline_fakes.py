"""Pipeline with fake engines and an FFmpeg-generated clip."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from threading import Event

import pytest

from nas_subtitles.config import AppConfig
from nas_subtitles.discovery import enqueue_path
from nas_subtitles.domain import (
    AudioChunk,
    ChunkTranscript,
    ErrorCode,
    JobState,
    LanguageDecision,
    LanguageSample,
    LanguageSource,
    ModelIdentity,
    ModelKind,
    PublishMode,
    TranscriptSegment,
    TranslatedUnit,
    Word,
)
from nas_subtitles.media import FfmpegAudioExtractor, FfprobeMediaProbe
from nas_subtitles.output import SrtSubtitleRenderer, read_manifest_payload, sidecar_path_for
from nas_subtitles.pipeline import StageContext, run_job
from nas_subtitles.repository import open_repository

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is required"),
]


class _FakeTranscriber:
    def __init__(self, *, language: str = "en", probability: float = 0.95) -> None:
        self.language = language
        self.probability = probability
        self.detect_calls = 0

    @property
    def model_identity(self) -> ModelIdentity:
        return ModelIdentity(kind=ModelKind.ASR, name="fake", path=Path("/models/fake"))

    def detect_language(self, samples):
        self.detect_calls += 1
        collected = tuple(
            LanguageSample(
                offset_seconds=chunk.spec.extract_start_seconds,
                duration_seconds=chunk.spec.extract_duration_seconds,
                language=self.language,
                probability=self.probability,
                has_speech=self.probability >= 0.15,
            )
            for chunk in samples
        )
        confident = self.probability >= 0.80
        return LanguageDecision(
            language=self.language if confident else None,
            source=LanguageSource.DETECTION,
            confident=confident,
            probability=self.probability,
            samples=collected,
        )

    def transcribe(self, chunk: AudioChunk, *, language: str) -> ChunkTranscript:
        start = chunk.spec.owned_start_seconds
        end = min(chunk.spec.owned_end_seconds, start + 1.5)
        word = Word(text="hello", start_seconds=start + 0.1, end_seconds=end, probability=0.9)
        return ChunkTranscript(
            chunk=chunk.spec,
            language=language,
            language_probability=self.probability,
            model_identity=self.model_identity,
            segments=(
                TranscriptSegment(
                    index=0,
                    start_seconds=word.start_seconds,
                    end_seconds=word.end_seconds,
                    text="hello",
                    words=(word,),
                    chunk_index=chunk.spec.index,
                ),
            ),
        )


class _FakeTranslator:
    def __init__(self) -> None:
        self.calls = 0

    @property
    def engine_identity(self) -> str:
        return "fake-translator"

    def supports(self, *, source_language: str, target_language: str) -> bool:
        return source_language == "en" and target_language.startswith("pt")

    def translate(self, units):
        self.calls += 1
        return tuple(
            TranslatedUnit(
                unit_id=unit.unit_id,
                source_text=unit.source_text,
                translated_text="ola",
                source_language=unit.source_language,
                target_language=unit.target_language,
                engine_identity=self.engine_identity,
            )
            for unit in units
        )


def _context(config: AppConfig, repo, job, *, transcriber=None, translator=None) -> StageContext:
    return StageContext(
        config=config,
        repository=repo,
        job=job,
        probe=FfprobeMediaProbe(),
        extractor=FfmpegAudioExtractor(),
        transcriber=transcriber or _FakeTranscriber(),
        translator=translator or _FakeTranslator(),
        renderer=SrtSubtitleRenderer(),
        stop_event=Event(),
    )


def _write_clip(video: Path) -> None:
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=32x32:d=2",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=2",
            "-c:v",
            "mpeg4",
            "-c:a",
            "aac",
            str(video),
        ],
        check=True,
        capture_output=True,
        timeout=60,
    )


def test_fake_pipeline_writes_staging_srt(config: AppConfig, media_root: Path) -> None:
    video = media_root / "episode.mkv"
    _write_clip(video)
    repo = open_repository(config)
    job, skipped = enqueue_path(config, repo, video, require_stability=False, source_language="en")
    assert skipped is None and job is not None
    context = _context(config, repo, job)
    result = run_job(context)
    repo.close()
    assert result.output_path is not None
    assert result.output_path.is_file()
    assert result.cue_count >= 1
    assert "ola" in result.output_path.read_text(encoding="utf-8").lower()
    assert result.state is not JobState.COMPLETED
    sidecar = sidecar_path_for(config, config.roots[0], "episode.mkv")
    assert not sidecar.exists()


def test_sidecar_mode_publishes_atomic_pt_br_next_to_media(
    config: AppConfig, media_root: Path
) -> None:
    config = config.model_copy(update={"publish_mode": PublishMode.SIDECAR})
    video = media_root / "Dexter.S03E01.mkv"
    _write_clip(video)
    original = video.read_bytes()
    repo = open_repository(config)
    job, skipped = enqueue_path(config, repo, video, require_stability=False, source_language="en")
    assert skipped is None and job is not None
    context = _context(config, repo, job)
    result = run_job(context)
    sidecar = sidecar_path_for(config, config.roots[0], job.relative_path)
    refreshed = repo.get_job(job.id)
    repo.close()
    assert result.output_path == sidecar
    assert sidecar.is_file()
    assert sidecar.name == "Dexter.S03E01.pt-BR.srt"
    assert "ola" in sidecar.read_text(encoding="utf-8").lower()
    assert video.read_bytes() == original
    assert refreshed is not None
    assert refreshed.state is JobState.COMPLETED


def test_sidecar_skip_policy_does_not_overwrite_during_processing(
    config: AppConfig, media_root: Path
) -> None:
    config = config.model_copy(update={"publish_mode": PublishMode.SIDECAR})
    video = media_root / "episode.mkv"
    _write_clip(video)
    sidecar = sidecar_path_for(config, config.roots[0], "episode.mkv")
    sidecar.write_text("existing-cues\n", encoding="utf-8")
    repo = open_repository(config)
    _job, skipped = enqueue_path(config, repo, video, require_stability=False, source_language="en")
    repo.close()
    assert skipped is not None
    assert "pt-BR" in skipped
    assert sidecar.read_text(encoding="utf-8") == "existing-cues\n"


def test_auto_english_detection_persists_and_translates_to_pt_br(
    config: AppConfig, media_root: Path
) -> None:
    video = media_root / "episode.mkv"
    _write_clip(video)
    repo = open_repository(config)
    job, skipped = enqueue_path(config, repo, video, require_stability=False)
    assert skipped is None and job is not None
    transcriber = _FakeTranscriber(language="en", probability=0.93)
    translator = _FakeTranslator()
    result = run_job(_context(config, repo, job, transcriber=transcriber, translator=translator))
    payload = read_manifest_payload(config, job.id)
    repo.close()
    assert transcriber.detect_calls == 1
    assert translator.calls == 1
    assert result.output_path is not None
    assert result.output_path.suffix == ".srt"
    assert ".pt-BR.srt" in result.output_path.name
    assert "pb" not in result.output_path.name
    assert payload is not None
    assert payload["source_language"] == "en"
    assert payload["detected_language"] == "en"
    assert payload["detection_probability"] == pytest.approx(0.93)
    assert payload["target_language"] == "pt-BR"
    assert payload["translation_executed"] is True
    assert payload["selected_audio_stream_index"] is not None
    assert payload["models"]
    assert any(item.get("kind") == "asr" for item in payload["models"])
    assert "pb" not in str(payload["target_language"])
    dumped = str(payload)
    assert '"pb"' not in dumped


def test_portuguese_source_skips_translation_for_pt_br_target(
    config: AppConfig, media_root: Path
) -> None:
    video = media_root / "episodio.mkv"
    _write_clip(video)
    repo = open_repository(config)
    job, skipped = enqueue_path(config, repo, video, require_stability=False, source_language="pt")
    assert skipped is None and job is not None
    transcriber = _FakeTranscriber(language="pt")
    translator = _FakeTranslator()
    result = run_job(_context(config, repo, job, transcriber=transcriber, translator=translator))
    payload = read_manifest_payload(config, job.id)
    repo.close()
    assert transcriber.detect_calls == 0
    assert translator.calls == 0
    assert result.output_path is not None
    assert "hello" in result.output_path.read_text(encoding="utf-8")
    assert payload is not None
    assert payload["source_language"] == "pt"
    assert payload["target_language"] == "pt-BR"
    assert payload["translation_executed"] is False
    assert payload["translation_engine_identity"] == "passthrough"
    assert payload["detected_language"] is None
    assert payload["source_language_source"] == "override"


def test_low_confidence_detection_goes_to_review(config: AppConfig, media_root: Path) -> None:
    video = media_root / "unclear.mkv"
    _write_clip(video)
    repo = open_repository(config)
    job, skipped = enqueue_path(config, repo, video, require_stability=False)
    assert skipped is None and job is not None
    transcriber = _FakeTranscriber(language="en", probability=0.2)
    translator = _FakeTranslator()
    result = run_job(_context(config, repo, job, transcriber=transcriber, translator=translator))
    refreshed = repo.get_job(job.id)
    payload = read_manifest_payload(config, job.id)
    repo.close()
    assert translator.calls == 0
    assert result.state is JobState.NEEDS_REVIEW
    assert refreshed is not None
    assert refreshed.error_code is ErrorCode.LANGUAGE_UNDETERMINED
    assert result.output_path is None
    assert payload is not None
    assert payload["source_language_confident"] is False
    assert payload["translation_executed"] is False
    assert payload["target_language"] == "pt-BR"
