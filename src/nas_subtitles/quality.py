"""Quality gates. Owned by stage 6.

These checks never delete text. A structural error blocks publication; an
advisory flag leaves the result in staging as ``needs_review``. None of this
is a claim about translation fluency, which only a human can judge.
"""

from __future__ import annotations

from collections.abc import Sequence

from .config import AppConfig
from .domain import (
    STRUCTURAL_FLAG_CODES,
    JobState,
    QualityFlag,
    QualityFlagCode,
    QualityReport,
    QualitySeverity,
    Seconds,
    SubtitleCue,
    Transcript,
    TranslatedUnit,
)

__all__ = ["evaluate_cues", "evaluate_transcript", "gate_state", "verify_roundtrip"]


def evaluate_transcript(config: AppConfig, transcript: Transcript) -> QualityReport:
    """Flag long repetitions, low confidence and text in silent stretches."""
    del config
    flags: list[QualityFlag] = []
    previous_text = ""
    for segment in transcript.segments:
        if (
            segment.no_speech_probability is not None
            and segment.no_speech_probability >= 0.8
            and segment.text.strip()
        ):
            flags.append(
                QualityFlag(
                    code=QualityFlagCode.TEXT_WITHOUT_SPEECH,
                    message="text produced in a stretch with high no-speech probability",
                    cue_index=segment.index,
                )
            )
        if segment.avg_logprob is not None and segment.avg_logprob < -1.2:
            flags.append(
                QualityFlag(
                    code=QualityFlagCode.LOW_CONFIDENCE,
                    message="segment average log probability is low",
                    cue_index=segment.index,
                    observed=segment.avg_logprob,
                )
            )
        normalised = " ".join(segment.text.split()).casefold()
        if normalised and normalised == previous_text:
            flags.append(
                QualityFlag(
                    code=QualityFlagCode.REPEATED_TEXT,
                    message="adjacent segments repeat the same text",
                    cue_index=segment.index,
                )
            )
        previous_text = normalised
    return QualityReport(flags=tuple(flags))


def evaluate_cues(
    config: AppConfig,
    cues: Sequence[SubtitleCue],
    *,
    duration_seconds: Seconds,
    units: Sequence[TranslatedUnit] = (),
) -> QualityReport:
    """Check indices, ordering, overlap, bounds, width and reading speed."""
    flags: list[QualityFlag] = []
    seen_units = {unit.unit_id for unit in units if unit.source_text.strip()}
    covered: set[str] = set()
    previous: SubtitleCue | None = None
    for cue in cues:
        covered.update(cue.unit_ids)
        if not cue.text.strip():
            flags.append(_structural(QualityFlagCode.EMPTY_CUE_TEXT, "cue text is empty", cue))
        if cue.index != (previous.index + 1 if previous else 1):
            flags.append(
                _structural(
                    QualityFlagCode.INDEX_NOT_SEQUENTIAL,
                    "cue indices are not consecutive from 1",
                    cue,
                )
            )
        if cue.start_seconds < 0 or cue.end_seconds > duration_seconds + 0.05:
            flags.append(
                _structural(
                    QualityFlagCode.TIMESTAMP_OUT_OF_RANGE,
                    "cue times are outside the media duration",
                    cue,
                    observed=cue.end_seconds,
                    threshold=duration_seconds,
                )
            )
        if cue.end_seconds <= cue.start_seconds:
            flags.append(
                _structural(
                    QualityFlagCode.NON_MONOTONIC_TIMESTAMPS,
                    "cue end is not after cue start",
                    cue,
                )
            )
        if previous is not None:
            if cue.start_seconds < previous.start_seconds:
                flags.append(
                    _structural(
                        QualityFlagCode.NON_MONOTONIC_TIMESTAMPS,
                        "cue starts before the previous cue",
                        cue,
                    )
                )
            if cue.start_seconds < previous.end_seconds - 1e-6:
                flags.append(_structural(QualityFlagCode.CUE_OVERLAP, "cues overlap", cue))
        if len(cue.lines) > config.subtitles.max_lines:
            flags.append(
                QualityFlag(
                    code=QualityFlagCode.LINE_COUNT_EXCEEDED,
                    message="cue has more than the target number of lines",
                    cue_index=cue.index,
                    observed=float(len(cue.lines)),
                    threshold=float(config.subtitles.max_lines),
                )
            )
        for line in cue.lines:
            if len(line) > config.subtitles.max_chars_per_line:
                flags.append(
                    QualityFlag(
                        code=QualityFlagCode.LINE_WIDTH_EXCEEDED,
                        message="a line is wider than the target width",
                        cue_index=cue.index,
                        observed=float(len(line)),
                        threshold=float(config.subtitles.max_chars_per_line),
                    )
                )
        if cue.duration_seconds + 1e-9 < config.subtitles.min_duration_seconds:
            flags.append(
                QualityFlag(
                    code=QualityFlagCode.DURATION_BELOW_MINIMUM,
                    message="cue is shorter than the target minimum",
                    cue_index=cue.index,
                    observed=cue.duration_seconds,
                    threshold=config.subtitles.min_duration_seconds,
                )
            )
        if cue.duration_seconds > config.subtitles.max_duration_seconds:
            flags.append(
                QualityFlag(
                    code=QualityFlagCode.DURATION_ABOVE_MAXIMUM,
                    message="cue is longer than the target maximum",
                    cue_index=cue.index,
                    observed=cue.duration_seconds,
                    threshold=config.subtitles.max_duration_seconds,
                )
            )
        if cue.characters_per_second > config.subtitles.target_max_chars_per_second:
            flags.append(
                QualityFlag(
                    code=QualityFlagCode.READING_SPEED_EXCEEDED,
                    message="cue exceeds the target reading speed",
                    cue_index=cue.index,
                    observed=cue.characters_per_second,
                    threshold=float(config.subtitles.target_max_chars_per_second),
                )
            )
        previous = cue
    missing = seen_units - covered
    for unit_id in sorted(missing):
        flags.append(
            _structural(
                QualityFlagCode.TRANSLATION_MISSING,
                "a translation unit was not rendered",
                None,
                unit_id=unit_id,
            )
        )
    return QualityReport(flags=tuple(flags))


def verify_roundtrip(content: str, cues: Sequence[SubtitleCue]) -> QualityReport:
    """Re-parse the rendered file with ``srt`` and compare against the cues."""
    from .output import SrtSubtitleRenderer

    parsed = SrtSubtitleRenderer().parse(content)
    if len(parsed) != len(cues):
        return QualityReport(
            flags=(
                _structural(
                    QualityFlagCode.SRT_ROUNDTRIP_FAILED,
                    "parsed cue count does not match the renderer",
                    None,
                    observed=float(len(parsed)),
                    threshold=float(len(cues)),
                ),
            )
        )
    flags: list[QualityFlag] = []
    for original, again in zip(cues, parsed, strict=True):
        if original.text.strip() != again.text.strip() or original.index != again.index:
            flags.append(
                _structural(
                    QualityFlagCode.SRT_ROUNDTRIP_FAILED,
                    "parsed cue does not match the renderer",
                    original,
                )
            )
    return QualityReport(flags=tuple(flags))


def gate_state(report: QualityReport) -> JobState:
    """``needs_review`` for any flag, ``ready_to_publish`` for a clean report."""
    if report.flags:
        return JobState.NEEDS_REVIEW
    return JobState.READY_TO_PUBLISH


def _structural(
    code: QualityFlagCode,
    message: str,
    cue: SubtitleCue | None,
    *,
    unit_id: str | None = None,
    observed: float | None = None,
    threshold: float | None = None,
) -> QualityFlag:
    severity = (
        QualitySeverity.STRUCTURAL if code in STRUCTURAL_FLAG_CODES else QualitySeverity.ADVISORY
    )
    return QualityFlag(
        code=code,
        message=message,
        severity=severity,
        cue_index=None if cue is None else cue.index,
        unit_id=unit_id,
        observed=observed,
        threshold=threshold,
    )
