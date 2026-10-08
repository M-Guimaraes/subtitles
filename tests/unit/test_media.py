"""Stream selection and chunk ownership on the video timeline."""

from __future__ import annotations

from pathlib import Path

import pytest

from nas_subtitles.config import AppConfig
from nas_subtitles.domain import (
    AudioStreamInfo,
    ErrorCode,
    NasSubtitlesError,
    ProbeResult,
    SubtitleStreamInfo,
)
from nas_subtitles.media import ensure_free_space, plan_chunks, select_audio_stream


def _probe(*streams: AudioStreamInfo, path: Path | None = None) -> ProbeResult:
    return ProbeResult(
        path=path or Path("/media/library/series/episode.mkv"),
        duration_seconds=600.0,
        size_bytes=1,
        audio_streams=streams,
    )


def test_global_index_override_is_not_an_a_n_ordinal() -> None:
    """``1`` is the global ffprobe index, never the relative ``a:1`` ordinal."""
    streams = (
        AudioStreamInfo(index=1, language="en", is_commentary=True, title="Director commentary"),
        AudioStreamInfo(index=2, language="en", is_default=True),
    )
    chosen = select_audio_stream(_probe(*streams), override_index=2)
    assert chosen.index == 2
    # Passing 1 selects the commentary stream (global index 1), not "the second
    # audio track". A missing global index is invalid_media.
    commentary = select_audio_stream(_probe(*streams), override_index=1)
    assert commentary.is_commentary is True
    with pytest.raises(NasSubtitlesError) as missing:
        select_audio_stream(_probe(*streams), override_index=0)
    assert missing.value.code is ErrorCode.INVALID_MEDIA


def test_missing_global_index_is_invalid_media() -> None:
    streams = (AudioStreamInfo(index=1, language="en"),)
    with pytest.raises(NasSubtitlesError) as raised:
        select_audio_stream(_probe(*streams), override_index=3)
    assert raised.value.code is ErrorCode.INVALID_MEDIA


def test_english_non_commentary_beats_portuguese_then_default() -> None:
    streams = (
        AudioStreamInfo(index=1, language="pt", is_default=True),
        AudioStreamInfo(index=2, language="en", title="Commentary", is_commentary=True),
        AudioStreamInfo(index=3, language="en"),
    )
    assert select_audio_stream(_probe(*streams)).index == 3


def test_portuguese_is_chosen_when_english_is_only_commentary() -> None:
    streams = (
        AudioStreamInfo(index=1, language="en", is_commentary=True, title="Commentary"),
        AudioStreamInfo(index=2, language="pt"),
    )
    assert select_audio_stream(_probe(*streams)).index == 2


def test_preferred_languages_are_deterministic_and_prefer_ja_over_pt(config: AppConfig) -> None:
    streams = (
        AudioStreamInfo(index=1, language="pt", is_default=True),
        AudioStreamInfo(index=2, language="jpn"),
        AudioStreamInfo(index=3, language="en", is_commentary=True),
    )
    assert select_audio_stream(_probe(*streams), config=config).index == 2
    english_only = config.model_copy(
        update={"audio": config.audio.model_copy(update={"preferred_languages": ("en",)})}
    )
    assert select_audio_stream(_probe(*streams), config=english_only).index == 1


def test_configured_global_stream_index_is_not_an_a_n_ordinal(config: AppConfig) -> None:
    streams = (
        AudioStreamInfo(index=1, language="en"),
        AudioStreamInfo(index=2, language="ja"),
    )
    pinned = config.model_copy(update={"audio": config.audio.model_copy(update={"stream": 2})})
    assert select_audio_stream(_probe(*streams), config=pinned).index == 2
    assert select_audio_stream(_probe(*streams), override_index=1, config=pinned).index == 1


def test_no_audio_is_invalid_media() -> None:
    with pytest.raises(NasSubtitlesError) as raised:
        select_audio_stream(_probe())
    assert raised.value.code is ErrorCode.INVALID_MEDIA


def test_chunk_k_owns_the_central_interval_not_the_overlap() -> None:
    specs = plan_chunks(
        duration_seconds=650.0,
        stream_start_seconds=0.0,
        chunk_seconds=300.0,
        overlap_seconds=2.0,
    )
    assert len(specs) == 3
    first, second, third = specs
    assert first.owned_start_seconds == 0.0
    assert first.owned_end_seconds == 300.0
    assert first.extract_start_seconds == 0.0
    assert first.extract_end_seconds == 302.0
    assert second.owned_start_seconds == 300.0
    assert second.owned_end_seconds == 600.0
    assert second.extract_start_seconds == 298.0
    assert second.extract_end_seconds == 602.0
    assert third.owned_start_seconds == 600.0
    assert third.owned_end_seconds == 650.0
    # The overlap belongs to chunk 0 as context only: 299.0 is owned by chunk 0.
    assert first.owns(299.0)
    assert not second.owns(299.0)
    assert second.owns(300.0)


def test_non_zero_stream_start_does_not_shift_owned_intervals() -> None:
    specs = plan_chunks(
        duration_seconds=10.0,
        stream_start_seconds=1.4,
        chunk_seconds=300.0,
        overlap_seconds=2.0,
    )
    assert len(specs) == 1
    assert specs[0].owned_start_seconds == 0.0
    assert specs[0].owned_end_seconds == 10.0


def test_forced_embedded_subtitle_does_not_count_as_complete() -> None:
    from nas_subtitles.discovery import find_existing_subtitles, has_portuguese_subtitle

    probe = ProbeResult(
        path=Path("/media/library/series/episode.mkv"),
        duration_seconds=10.0,
        size_bytes=1,
        subtitle_streams=(SubtitleStreamInfo(index=2, language="pt", is_forced=True),),
    )
    found = find_existing_subtitles(path=probe.path, probe_result=probe)
    assert found[0].is_forced is True
    assert has_portuguese_subtitle(found) is False


def test_ensure_free_space_accepts_the_configured_floor(config: AppConfig, tmp_path: Path) -> None:
    ensure_free_space(config, config.work_dir)
