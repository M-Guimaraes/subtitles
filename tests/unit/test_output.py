"""Wrapping without loss, exclusive publish and preview naming."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from nas_subtitles.config import AppConfig
from nas_subtitles.domain import (
    JobRecord,
    JobState,
    MediaFingerprint,
    PublishMode,
    PublishOutcome,
    SubtitleCue,
    TranslatedUnit,
)
from nas_subtitles.output import (
    PREVIEW_MARKER,
    SrtSubtitleRenderer,
    preview_path_for,
    publish_exclusive,
    sidecar_path_for,
    staging_path_for,
)
from nas_subtitles.quality import evaluate_cues, verify_roundtrip
from nas_subtitles.segmentation import segment_units_into_cues, wrap_text


def test_wrap_never_drops_or_splits_words() -> None:
    text = "supercalifragilisticexpialidocious is a very long word indeed"
    lines = wrap_text(text, max_lines=2, max_chars_per_line=20)
    assert "supercalifragilisticexpialidocious" in " ".join(lines)
    assert "indeed" in " ".join(lines)
    joined = " ".join(lines)
    for word in text.split():
        assert word in joined


def test_cues_are_consecutive_and_keep_all_text(config: AppConfig) -> None:
    from nas_subtitles.domain import TranslationUnit

    source = TranslationUnit(
        unit_id="u1",
        source_text="Hello there friend",
        start_seconds=1.0,
        end_seconds=3.0,
        source_language="en",
        target_language="pt-BR",
        word_count=3,
    )
    translated = TranslatedUnit(
        unit_id="u1",
        source_text=source.source_text,
        translated_text="Ola amigo querido",
        source_language="en",
        target_language="pt-BR",
        engine_identity="t",
    )
    cues = segment_units_into_cues(config, (translated,), source_units=(source,))
    assert cues
    assert "amigo" in cues[0].text
    assert cues[0].index == 1


def test_renderer_round_trip() -> None:
    cues = (
        SubtitleCue(index=1, start_seconds=1.0, end_seconds=2.5, lines=("Ola", "mundo")),
        SubtitleCue(index=2, start_seconds=2.5, end_seconds=4.0, lines=("Tchau",)),
    )
    renderer = SrtSubtitleRenderer()
    content = renderer.render(cues)
    parsed = renderer.parse(content)
    assert [cue.text for cue in parsed] == [cue.text for cue in cues]
    report = verify_roundtrip(content, cues)
    assert report.structural_errors == ()


def test_publish_does_not_overwrite(tmp_path: Path) -> None:
    target = tmp_path / "episode.pt-BR.srt"
    first = publish_exclusive(content="1\n", target=target)
    assert first.outcome is PublishOutcome.PUBLISHED
    second = publish_exclusive(content="2\n", target=target)
    assert second.outcome is PublishOutcome.CONFLICT
    assert target.read_text(encoding="utf-8") == "1\n"
    assert second.conflict_path is not None
    assert second.conflict_path.read_text(encoding="utf-8") == "2\n"


def test_preview_name_cannot_be_a_sidecar(config: AppConfig) -> None:
    job = JobRecord(
        id="job-1",
        root_id=config.roots[0].root_id,
        relative_path="show/episode.mkv",
        fingerprint=MediaFingerprint(
            root_id=config.roots[0].root_id,
            relative_path="show/episode.mkv",
            size_bytes=1,
            mtime_ns=1,
            head_sha256="a" * 64,
            tail_sha256="b" * 64,
        ),
        pipeline_config_hash="h",
        state=JobState.QUEUED,
        created_at=datetime.now(tz=UTC),
        updated_at=datetime.now(tz=UTC),
        preview_seconds=20,
    )
    preview = preview_path_for(config, job)
    sidecar = sidecar_path_for(config, config.roots[0], job.relative_path)
    staged = staging_path_for(config, job)
    assert PREVIEW_MARKER in preview.name
    assert PREVIEW_MARKER not in sidecar.name
    assert preview != sidecar
    assert staged.parent.name == "show" or "show" in str(staged)
    assert config.publish_mode is PublishMode.STAGING


def test_evaluate_cues_flags_overlap_without_deleting(config: AppConfig) -> None:
    cues = (
        SubtitleCue(index=1, start_seconds=1.0, end_seconds=3.0, lines=("one",)),
        SubtitleCue(index=2, start_seconds=2.0, end_seconds=4.0, lines=("two",)),
    )
    report = evaluate_cues(config, cues, duration_seconds=10)
    assert report.blocks_publication is True
    assert cues[0].text == "one"
