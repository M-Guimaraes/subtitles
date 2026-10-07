"""Checkpoints, absolute timestamps and overlap deduplication."""

from __future__ import annotations

from pathlib import Path

from nas_subtitles.config import AppConfig
from nas_subtitles.domain import (
    CHECKPOINT_SCHEMA_VERSION,
    AudioChunkSpec,
    ChunkTranscript,
    MediaFingerprint,
    ModelIdentity,
    ModelKind,
    PipelineStage,
    TranscriptSegment,
    Word,
)
from nas_subtitles.transcription import (
    chunk_checkpoint_path,
    merge_chunk_transcripts,
    read_chunk_checkpoint,
    write_chunk_checkpoint,
)


def _fingerprint() -> MediaFingerprint:
    return MediaFingerprint(
        root_id="library-aaaa1111",
        relative_path="show/episode.mkv",
        size_bytes=1024,
        mtime_ns=1,
        head_sha256="a" * 64,
        tail_sha256="b" * 64,
        audio_stream_index=1,
    )


def _identity() -> ModelIdentity:
    return ModelIdentity(kind=ModelKind.ASR, name="small", path=Path("/models/whisper/small"))


def _spec(
    index: int, start: float, end: float, extract_start: float, extract_end: float
) -> AudioChunkSpec:
    return AudioChunkSpec(
        index=index,
        owned_start_seconds=start,
        owned_end_seconds=end,
        extract_start_seconds=extract_start,
        extract_end_seconds=extract_end,
    )


def test_checkpoint_round_trip_and_identity_mismatch(config: AppConfig) -> None:
    spec = _spec(0, 0, 300, 0, 302)
    transcript = ChunkTranscript(
        chunk=spec,
        language="en",
        model_identity=_identity(),
        segments=(
            TranscriptSegment(
                index=0,
                start_seconds=1.0,
                end_seconds=2.0,
                text="hello",
                words=(Word(text="hello", start_seconds=1.0, end_seconds=2.0, probability=0.9),),
            ),
        ),
    )
    path = chunk_checkpoint_path(config, job_id="job-1", chunk_index=0)
    stage_hash = config.stage_config_hash(PipelineStage.TRANSCRIBE)
    write_chunk_checkpoint(
        path, transcript, job_id="job-1", fingerprint=_fingerprint(), stage_config_hash=stage_hash
    )
    loaded = read_chunk_checkpoint(
        path,
        job_id="job-1",
        fingerprint=_fingerprint(),
        stage_config_hash=stage_hash,
        model_identity=_identity(),
    )
    assert loaded is not None
    assert loaded.segments[0].text == "hello"
    assert loaded.segments[0].start_seconds == 1.0
    other = read_chunk_checkpoint(
        path,
        job_id="job-other",
        fingerprint=_fingerprint(),
        stage_config_hash=stage_hash,
        model_identity=_identity(),
    )
    assert other is None


def test_truncated_checkpoint_is_ignored(config: AppConfig) -> None:
    path = chunk_checkpoint_path(config, job_id="job-1", chunk_index=1)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{", encoding="utf-8")
    loaded = read_chunk_checkpoint(
        path,
        job_id="job-1",
        fingerprint=_fingerprint(),
        stage_config_hash=config.stage_config_hash(PipelineStage.TRANSCRIBE),
        model_identity=_identity(),
    )
    assert loaded is None
    assert CHECKPOINT_SCHEMA_VERSION == 1


def test_merge_dedupes_overlapping_boundary_tokens_only() -> None:
    first = ChunkTranscript(
        chunk=_spec(0, 0, 300, 0, 302),
        language="en",
        segments=(
            TranscriptSegment(
                index=0,
                start_seconds=298.0,
                end_seconds=301.0,
                text="hello world",
                words=(
                    Word(text="hello", start_seconds=297.0, end_seconds=298.0, probability=0.4),
                    Word(text="world", start_seconds=298.5, end_seconds=299.8, probability=0.4),
                ),
            ),
        ),
    )
    second = ChunkTranscript(
        chunk=_spec(1, 300, 600, 298, 602),
        language="en",
        segments=(
            TranscriptSegment(
                index=0,
                start_seconds=299.0,
                end_seconds=302.0,
                text="world again",
                words=(
                    Word(text="world", start_seconds=299.2, end_seconds=301.0, probability=0.9),
                    Word(text="again", start_seconds=320.0, end_seconds=321.0, probability=0.9),
                ),
            ),
        ),
    )
    merged = merge_chunk_transcripts((first, second), duration_seconds=600)
    world = [word for word in merged.words if word.text == "world"]
    assert len(world) == 1
    assert world[0].probability == 0.9
    # The same word later in time is kept.
    assert any(word.text == "again" for word in merged.words)
    assert any(word.text == "hello" for word in merged.words)
