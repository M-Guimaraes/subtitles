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


def _merge_words(*words: Word) -> tuple[Word, ...]:
    start = words[0].start_seconds
    end = words[-1].end_seconds
    transcript = ChunkTranscript(
        chunk=_spec(0, 0.0, 600.0, 0.0, 600.0),
        language="en",
        segments=(
            TranscriptSegment(
                index=0,
                start_seconds=start,
                end_seconds=end,
                text=" ".join(word.text for word in words),
                words=words,
            ),
        ),
    )
    return merge_chunk_transcripts((transcript,), duration_seconds=600).words


def test_merge_drops_touching_duplicate_when_confidence_splits() -> None:
    merged = _merge_words(
        Word(text="means", start_seconds=19.50, end_seconds=19.80, probability=0.9986),
        Word(text="means", start_seconds=19.80, end_seconds=20.28, probability=0.3833),
    )
    assert [word.text for word in merged] == ["means"]
    assert merged[0].probability == 0.9986
    assert merged[0].start_seconds == 19.50
    assert merged[0].end_seconds == 19.80


def test_merge_keeps_touching_duplicates_when_both_are_high_confidence() -> None:
    merged = _merge_words(
        Word(text="very", start_seconds=30.00, end_seconds=30.30, probability=0.94),
        Word(text="very", start_seconds=30.30, end_seconds=30.60, probability=0.91),
    )
    assert [(word.text, word.probability) for word in merged] == [
        ("very", 0.94),
        ("very", 0.91),
    ]


def test_merge_keeps_touching_duplicates_when_probability_is_missing() -> None:
    merged = _merge_words(
        Word(text="no", start_seconds=40.00, end_seconds=40.30, probability=None),
        Word(text="no", start_seconds=40.30, end_seconds=40.60, probability=None),
    )
    assert [word.text for word in merged] == ["no", "no"]
    assert merged[0].probability is None
    assert merged[1].probability is None


def test_merge_keeps_same_token_when_gap_exceeds_boundary_epsilon() -> None:
    merged = _merge_words(
        Word(text="means", start_seconds=19.50, end_seconds=19.80, probability=0.9986),
        Word(text="means", start_seconds=19.83, end_seconds=20.28, probability=0.3833),
    )
    assert [word.text for word in merged] == ["means", "means"]
    assert merged[0].probability == 0.9986
    assert merged[1].probability == 0.3833


def test_merge_never_drops_different_adjacent_tokens() -> None:
    merged = _merge_words(
        Word(text="that", start_seconds=19.20, end_seconds=19.50, probability=0.99),
        Word(text="means", start_seconds=19.50, end_seconds=19.80, probability=0.20),
    )
    assert [word.text for word in merged] == ["that", "means"]


def test_merge_drops_touching_duplicate_when_peripheral_punctuation_differs() -> None:
    merged = _merge_words(
        Word(
            text="means",
            start_seconds=19.64,
            end_seconds=19.98,
            probability=0.9985106587409973,
        ),
        Word(
            text="means?",
            start_seconds=19.98,
            end_seconds=20.30,
            probability=0.5405997037887573,
        ),
    )
    assert [word.text for word in merged] == ["means"]
    assert merged[0].probability == 0.9985106587409973
    assert merged[0].start_seconds == 19.64
    assert merged[0].end_seconds == 19.98


def test_merge_treats_peripheral_punctuation_as_the_same_token() -> None:
    hello = _merge_words(
        Word(text="hello", start_seconds=1.00, end_seconds=1.20, probability=0.99),
        Word(text="hello,", start_seconds=1.20, end_seconds=1.40, probability=0.50),
    )
    assert [word.text for word in hello] == ["hello"]
    assert hello[0].probability == 0.99

    no = _merge_words(
        Word(text="No", start_seconds=2.00, end_seconds=2.20, probability=0.99),
        Word(text="no!", start_seconds=2.20, end_seconds=2.40, probability=0.40),
    )
    assert [word.text for word in no] == ["No"]
    assert no[0].probability == 0.99


def test_merge_keeps_internal_characters_when_comparing_tokens() -> None:
    merged = _merge_words(
        Word(text="don't", start_seconds=3.00, end_seconds=3.20, probability=0.99),
        Word(text="dont", start_seconds=3.20, end_seconds=3.40, probability=0.20),
    )
    assert [word.text for word in merged] == ["don't", "dont"]


def test_merge_keeps_high_confidence_repetition_with_peripheral_punctuation() -> None:
    merged = _merge_words(
        Word(text="No", start_seconds=4.00, end_seconds=4.30, probability=0.94),
        Word(text="no!", start_seconds=4.30, end_seconds=4.60, probability=0.91),
    )
    assert [(word.text, word.probability) for word in merged] == [
        ("No", 0.94),
        ("no!", 0.91),
    ]
