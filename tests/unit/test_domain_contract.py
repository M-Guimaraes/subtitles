"""Invariants of the shared domain contract."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from nas_subtitles.domain import (
    EXIT_CODE_BY_ERROR,
    LIBRARY_SCAN_KNOWN_STATES,
    PIPELINE_STAGE_ORDER,
    RETRYABLE_ERROR_CODES,
    STRUCTURAL_FLAG_CODES,
    AudioChunkSpec,
    ErrorCode,
    ExitCode,
    JobExecutionScope,
    JobState,
    MediaFingerprint,
    PipelineStage,
    QualityFlag,
    QualityFlagCode,
    QualityReport,
    QualitySeverity,
    SubtitleCue,
    Word,
    exit_code_for,
    infer_execution_scope,
    stable_digest,
    stable_unit_id,
)


def test_exit_codes_are_the_documented_set() -> None:
    assert sorted(int(code) for code in ExitCode) == [0, 2, 3, 4, 5, 6]


def test_every_error_code_maps_to_an_exit_code() -> None:
    for code in ErrorCode:
        assert code in EXIT_CODE_BY_ERROR, f"{code} has no documented exit code"


def test_the_four_named_error_codes_exist() -> None:
    for name in (
        "invalid_media",
        "unsupported_language",
        "output_conflict",
        "unsupported_atomic_publish",
    ):
        assert ErrorCode(name)


def test_conflict_and_review_use_exit_code_five() -> None:
    assert exit_code_for(ErrorCode.OUTPUT_CONFLICT) is ExitCode.REVIEW_REQUIRED
    assert exit_code_for(ErrorCode.LANGUAGE_UNDETERMINED) is ExitCode.REVIEW_REQUIRED
    assert exit_code_for(ErrorCode.LOCK_BUSY) is ExitCode.LOCK_BUSY


def test_permission_media_and_conflict_are_never_retried() -> None:
    for code in (
        ErrorCode.PERMISSION_DENIED,
        ErrorCode.INVALID_MEDIA,
        ErrorCode.MODEL_MISSING,
        ErrorCode.UNSUPPORTED_LANGUAGE,
        ErrorCode.OUTPUT_CONFLICT,
    ):
        assert code not in RETRYABLE_ERROR_CODES


def test_stage_order_covers_every_stage_once() -> None:
    assert len(PIPELINE_STAGE_ORDER) == len(PipelineStage)
    assert set(PIPELINE_STAGE_ORDER) == set(PipelineStage)
    assert PIPELINE_STAGE_ORDER[0] is PipelineStage.PROBE
    assert PIPELINE_STAGE_ORDER[-1] is PipelineStage.PUBLISH


def test_chunk_ownership_is_half_open_so_no_timestamp_belongs_to_two_chunks() -> None:
    first = AudioChunkSpec(
        index=0,
        owned_start_seconds=0.0,
        owned_end_seconds=300.0,
        extract_start_seconds=0.0,
        extract_end_seconds=302.0,
    )
    second = AudioChunkSpec(
        index=1,
        owned_start_seconds=300.0,
        owned_end_seconds=600.0,
        extract_start_seconds=298.0,
        extract_end_seconds=602.0,
    )
    assert first.owns(299.999)
    assert not first.owns(300.0)
    assert second.owns(300.0)
    # The overlap is context only: it is extracted but not owned.
    assert first.extract_end_seconds > first.owned_end_seconds
    assert not first.owns(301.0)


def test_word_midpoint_decides_ownership_at_a_boundary() -> None:
    spanning = Word(text="boundary", start_seconds=299.6, end_seconds=300.6)
    assert spanning.midpoint_seconds == pytest.approx(300.1)


def test_fingerprint_detects_a_changed_file_but_ignores_stream_choice() -> None:
    base = MediaFingerprint(
        root_id="series-abc",
        relative_path="Show/S01E01.mkv",
        size_bytes=1000,
        mtime_ns=123,
        head_sha256="aa",
        tail_sha256="bb",
        audio_stream_index=1,
    )
    other_stream = replace(base, audio_stream_index=2)
    assert base.content_matches(other_stream)
    assert base.digest() != other_stream.digest()

    reencoded = replace(base, size_bytes=2000)
    assert not base.content_matches(reencoded)


def test_unit_ids_are_stable_and_fingerprint_scoped() -> None:
    def unit_id(fingerprint_digest: str) -> str:
        return stable_unit_id(
            fingerprint_digest=fingerprint_digest,
            index=3,
            start_seconds=12.0,
            end_seconds=15.0,
            normalized_text="hello there",
        )

    assert unit_id("abc") == unit_id("abc")
    assert unit_id("abc") != unit_id("def")


def test_canonical_digest_is_order_independent() -> None:
    assert stable_digest({"a": 1, "b": 2}) == stable_digest({"b": 2, "a": 1})


def test_canonical_digest_serialises_paths() -> None:
    assert stable_digest(Path("/state")) == stable_digest("/state")


def test_structural_flags_block_publication_and_advisories_only_review() -> None:
    advisory = QualityReport(
        flags=(
            QualityFlag(
                code=QualityFlagCode.READING_SPEED_EXCEEDED,
                message="too fast",
                severity=QualitySeverity.ADVISORY,
            ),
        )
    )
    assert not advisory.blocks_publication
    assert advisory.requires_review

    structural = QualityReport(
        flags=(
            QualityFlag(
                code=QualityFlagCode.CUE_OVERLAP,
                message="overlap",
                severity=QualitySeverity.STRUCTURAL,
            ),
        )
    )
    assert structural.blocks_publication
    assert QualityFlagCode.CUE_OVERLAP in STRUCTURAL_FLAG_CODES


def test_execution_scope_treats_legacy_preview_seconds_as_preview() -> None:
    assert infer_execution_scope(preview_seconds=300.0) is JobExecutionScope.PREVIEW
    assert infer_execution_scope() is JobExecutionScope.FULL
    assert infer_execution_scope(stored="full", preview_seconds=300.0) is JobExecutionScope.FULL
    assert frozenset(JobState) == LIBRARY_SCAN_KNOWN_STATES


def test_cue_reading_speed_uses_visible_characters() -> None:
    cue = SubtitleCue(index=1, start_seconds=0.0, end_seconds=2.0, lines=("ab", "cd"))
    assert cue.text == "ab\ncd"
    assert cue.duration_seconds == pytest.approx(2.0)
    # The newline counts as a space rather than a character.
    assert cue.characters_per_second == pytest.approx(2.5)
