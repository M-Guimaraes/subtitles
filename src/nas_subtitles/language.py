"""Language tag normalisation and source-language decision. Owned by stage 3.

A filename is never evidence of the spoken language, and the language is
never asked for interactively: without enough confidence the job becomes
``needs_review`` and the queue moves on.
"""

from __future__ import annotations

from collections.abc import Sequence

from .config import AppConfig
from .domain import AudioStreamInfo, LanguageDecision, LanguageSample, Transcriber

__all__ = [
    "decide_source_language",
    "is_supported_source",
    "normalize_language_tag",
    "subtitle_suffix_language",
]


def normalize_language_tag(tag: str | None) -> str | None:
    """Fold ISO 639-1/2/3 and locale tags to a two-letter code.

    ``eng``/``en``/``en-US`` all become ``en``; ``por``/``pt``/``pob``/``pt-BR``
    all become ``pt``. Unknown or empty tags return ``None``.
    """
    raise NotImplementedError("language handling is implemented in stage 3 (media)")


def subtitle_suffix_language(filename: str) -> str | None:
    """Language implied by an external subtitle suffix, or ``None``.

    An ``.srt`` with no language suffix has no inferable language.
    """
    raise NotImplementedError("language handling is implemented in stage 3 (media)")


def is_supported_source(language: str | None) -> bool:
    """Only English and Portuguese are supported sources in this MVP."""
    raise NotImplementedError("language handling is implemented in stage 3 (media)")


def decide_source_language(
    config: AppConfig,
    *,
    stream: AudioStreamInfo,
    override: str | None = None,
    transcriber: Transcriber | None = None,
    samples: Sequence[LanguageSample] = (),
) -> LanguageDecision:
    """Resolve the source language from override, metadata, then ASR samples.

    Samples without speech are ignored. Disagreement between samples, or no
    sample reaching ``asr.detection_min_probability``, yields a decision with
    ``confident=False``.
    """
    raise NotImplementedError("language detection is implemented in stage 4 (asr)")
