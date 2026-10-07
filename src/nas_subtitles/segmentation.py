"""Turning translated units into cues. Owned by stage 6.

Width and reading-speed limits are targets, never a licence to cut a word or
drop text. When a unit cannot fit, the renderer emits a quality flag and the
job goes to review with all of its text intact.
"""

from __future__ import annotations

from collections.abc import Sequence

from .config import AppConfig
from .domain import Seconds, SubtitleCue, TranslatedUnit, TranslationUnit

__all__ = ["redistribute_within_unit", "segment_units_into_cues", "wrap_text"]


def wrap_text(text: str, *, max_lines: int, max_chars_per_line: int) -> tuple[str, ...]:
    """Break on word boundaries into balanced lines, never mid-word.

    May return more than ``max_lines`` lines when the text genuinely does not
    fit; the caller flags that for review instead of truncating.
    """
    del max_lines  # The limit is enforced by the caller via quality flags.
    words = text.split()
    if not words:
        return ()
    lines: list[str] = []
    current: list[str] = []
    for word in words:
        candidate = " ".join([*current, word])
        if current and len(candidate) > max_chars_per_line:
            lines.append(" ".join(current))
            current = [word]
        else:
            current.append(word)
    if current:
        lines.append(" ".join(current))
    return tuple(lines)


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
    if not parts:
        return ()
    weights = [max(len(part.replace("\n", " ")), 1) for part in parts]
    total = sum(weights)
    span = max(end_seconds - start_seconds, 0.0)
    cursor = start_seconds
    windows: list[tuple[Seconds, Seconds]] = []
    for index, weight in enumerate(weights):
        if index == len(weights) - 1:
            windows.append((cursor, end_seconds))
            break
        width = span * (weight / total)
        nxt = min(end_seconds, cursor + width)
        windows.append((cursor, nxt))
        cursor = nxt
    return tuple(windows)


def segment_units_into_cues(
    config: AppConfig,
    units: Sequence[TranslatedUnit],
    *,
    source_units: Sequence[TranslationUnit] = (),
) -> tuple[SubtitleCue, ...]:
    """Produce consecutive, non-overlapping cues numbered from 1."""
    max_lines = config.subtitles.max_lines
    max_chars = config.subtitles.max_chars_per_line
    times = {unit.unit_id: unit for unit in source_units}
    cues: list[SubtitleCue] = []
    previous_end = 0.0
    for unit in units:
        source = times.get(unit.unit_id)
        start = source.start_seconds if source is not None else previous_end
        end = source.end_seconds if source is not None else start + 1.0
        lines = wrap_text(unit.translated_text, max_lines=max_lines, max_chars_per_line=max_chars)
        if not lines:
            continue
        groups = _group_lines(lines, max_lines=max_lines)
        windows = redistribute_within_unit(
            start_seconds=start,
            end_seconds=end,
            parts=["\n".join(group) for group in groups],
        )
        for group, (cue_start, cue_end) in zip(groups, windows, strict=True):
            start = max(cue_start, previous_end)
            end = cue_end
            if end <= start:
                end = start + 0.001
            cues.append(
                SubtitleCue(
                    index=len(cues) + 1,
                    start_seconds=start,
                    end_seconds=end,
                    lines=group,
                    unit_ids=(unit.unit_id,),
                )
            )
            previous_end = end
    return tuple(cues)


def _group_lines(lines: tuple[str, ...], *, max_lines: int) -> tuple[tuple[str, ...], ...]:
    if max_lines < 1:
        max_lines = 1
    groups: list[tuple[str, ...]] = []
    for offset in range(0, len(lines), max_lines):
        groups.append(tuple(lines[offset : offset + max_lines]))
    return tuple(groups)
