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
    LanguageDecision,
    LanguageSource,
    ModelIdentity,
    ModelKind,
    TranscriptSegment,
    TranslatedUnit,
    Word,
)
from nas_subtitles.media import FfmpegAudioExtractor, FfprobeMediaProbe
from nas_subtitles.output import SrtSubtitleRenderer
from nas_subtitles.pipeline import StageContext, run_job
from nas_subtitles.repository import open_repository

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is required"),
]


class _FakeTranscriber:
    @property
    def model_identity(self) -> ModelIdentity:
        return ModelIdentity(kind=ModelKind.ASR, name="fake", path=Path("/models/fake"))

    def detect_language(self, samples):
        del samples
        return LanguageDecision(language="en", source=LanguageSource.OVERRIDE, confident=True)

    def transcribe(self, chunk: AudioChunk, *, language: str) -> ChunkTranscript:
        start = chunk.spec.owned_start_seconds
        end = min(chunk.spec.owned_end_seconds, start + 1.5)
        word = Word(text="hello", start_seconds=start + 0.1, end_seconds=end, probability=0.9)
        return ChunkTranscript(
            chunk=chunk.spec,
            language=language,
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
    @property
    def engine_identity(self) -> str:
        return "fake-translator"

    def supports(self, *, source_language: str, target_language: str) -> bool:
        return source_language == "en" and target_language.startswith("pt")

    def translate(self, units):
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


def test_fake_pipeline_writes_staging_srt(config: AppConfig, media_root: Path) -> None:
    video = media_root / "episode.mkv"
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
    repo = open_repository(config)
    job, skipped = enqueue_path(config, repo, video, require_stability=False, source_language="en")
    assert skipped is None and job is not None
    context = StageContext(
        config=config,
        repository=repo,
        job=job,
        probe=FfprobeMediaProbe(),
        extractor=FfmpegAudioExtractor(),
        transcriber=_FakeTranscriber(),
        translator=_FakeTranslator(),
        renderer=SrtSubtitleRenderer(),
        stop_event=Event(),
    )
    result = run_job(context)
    repo.close()
    assert result.output_path is not None
    assert result.output_path.is_file()
    assert result.cue_count >= 1
    assert "ola" in result.output_path.read_text(encoding="utf-8").lower()
