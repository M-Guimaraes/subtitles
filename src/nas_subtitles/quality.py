"""Quality gates. Owned by stage 6.

These checks never delete text. A structural error blocks publication; an
advisory flag leaves the result in staging as ``needs_review``. None of this
is a claim about translation fluency, which only a human can judge.
"""

from __future__ import annotations

from collections.abc import Sequence

from .config import AppConfig
from .domain import (
    JobState,
    QualityReport,
    Seconds,
    SubtitleCue,
    Transcript,
    TranslatedUnit,
)

__all__ = ["evaluate_cues", "evaluate_transcript", "gate_state", "verify_roundtrip"]


def evaluate_transcript(config: AppConfig, transcript: Transcript) -> QualityReport:
    """Flag long repetitions, low confidence and text in silent stretches."""
    raise NotImplementedError("quality gates are implemented in stage 6 (output)")


def evaluate_cues(
    config: AppConfig,
    cues: Sequence[SubtitleCue],
    *,
    duration_seconds: Seconds,
    units: Sequence[TranslatedUnit] = (),
) -> QualityReport:
    """Check indices, ordering, overlap, bounds, width and reading speed."""
    raise NotImplementedError("quality gates are implemented in stage 6 (output)")


def verify_roundtrip(content: str, cues: Sequence[SubtitleCue]) -> QualityReport:
    """Re-parse the rendered file with ``srt`` and compare against the cues."""
    raise NotImplementedError("quality gates are implemented in stage 6 (output)")


def gate_state(report: QualityReport) -> JobState:
    """``needs_review`` for any flag, ``ready_to_publish`` for a clean report."""
    raise NotImplementedError("quality gates are implemented in stage 6 (output)")
