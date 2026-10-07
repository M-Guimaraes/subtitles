"""Turning translated units into cues. Owned by stage 6.

Width and reading-speed limits are targets, never a licence to cut a word or
drop text. When a unit cannot fit, the renderer emits a quality flag and the
job goes to review with all of its text intact.
"""

from __future__ import annotations

from collections.abc import Sequence

from .config import AppConfig
from .domain import Seconds, SubtitleCue, TranslatedUnit

__all__ = ["redistribute_within_unit", "segment_units_into_cues", "wrap_text"]


def wrap_text(text: str, *, max_lines: int, max_chars_per_line: int) -> tuple[str, ...]:
    """Break on word boundaries into balanced lines, never mid-word.

    May return more than ``max_lines`` lines when the text genuinely does not
    fit; the caller flags that for review instead of truncating.
    """
    raise NotImplementedError("segmentation is implemented in stage 6 (output)")


def redistribute_within_unit(
    *,
    start_seconds: Seconds,
    end_seconds: Seconds,
    parts: Sequence[str],
) -> tuple[tuple[Seconds, Seconds], ...]:
    """Split a unit's own interval proportionally to text length.

    Length proportion is an approximation, not a claim of word-level
    alignment, and the split never extends past the unit's own interval.
    """
    raise NotImplementedError("segmentation is implemented in stage 6 (output)")


def segment_units_into_cues(
    config: AppConfig, units: Sequence[TranslatedUnit]
) -> tuple[SubtitleCue, ...]:
    """Produce consecutive, non-overlapping cues numbered from 1."""
    raise NotImplementedError("segmentation is implemented in stage 6 (output)")
