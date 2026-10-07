"""Language tag normalisation and source-language decision. Owned by stage 3.

A filename is never evidence of the spoken language, and the language is
never asked for interactively: without enough confidence the job becomes
``needs_review`` and the queue moves on.
"""

from __future__ import annotations

from collections.abc import Sequence

from .config import AppConfig
from .domain import (
    ENGLISH,
    PORTUGUESE,
    PORTUGUESE_SUBTITLE_SUFFIXES,
    SUPPORTED_SOURCE_LANGUAGES,
    AudioStreamInfo,
    ErrorCode,
    LanguageDecision,
    LanguageSample,
    LanguageSource,
    NasSubtitlesError,
    Transcriber,
)

__all__ = [
    "decide_source_language",
    "is_supported_source",
    "normalize_language_tag",
    "subtitle_suffix_language",
]

_SUBTITLE_EXTENSIONS = frozenset({".srt", ".vtt", ".ass"})

# Fold common ISO 639-1/2/3 and locale tags onto the two-letter codes we use.
_TAG_ALIASES: dict[str, str] = {
    "en": ENGLISH,
    "eng": ENGLISH,
    "pt": PORTUGUESE,
    "por": PORTUGUESE,
    "pob": PORTUGUESE,  # Brazilian Portuguese, still Portuguese for routing.
}


def normalize_language_tag(tag: str | None) -> str | None:
    """Fold ISO 639-1/2/3 and locale tags to a two-letter code.

    ``eng``/``en``/``en-US`` all become ``en``; ``por``/``pt``/``pob``/``pt-BR``
    all become ``pt``. Unknown or empty tags return ``None``.
    """
    if tag is None:
        return None
    stripped = tag.strip()
    if not stripped:
        return None
    lowered = stripped.replace("_", "-").lower()
    if lowered in {"und", "unknown", "unk", "zxx"}:
        return None
    primary = lowered.split("-", 1)[0]
    if primary in _TAG_ALIASES:
        return _TAG_ALIASES[primary]
    if len(primary) == 2 and primary.isalpha():
        return primary
    return None


def subtitle_suffix_language(filename: str) -> str | None:
    """Language implied by an external subtitle suffix, or ``None``.

    An ``.srt`` with no language suffix has no inferable language.
    """
    name = filename.rsplit("/", 1)[-1]
    lowered = name.lower()
    suffix = ""
    for extension in _SUBTITLE_EXTENSIONS:
        if lowered.endswith(extension):
            suffix = extension
            break
    if not suffix:
        return None
    stem = lowered[: -len(suffix)]
    tokens = [token for token in stem.split(".") if token]
    if not tokens:
        return None
    # Walk from the right so ``show.pt-BR.forced`` and ``show.forced.pt`` both work.
    for token in reversed(tokens):
        if token in {"forced", "sdh", "hi", "cc"}:
            continue
        if token in PORTUGUESE_SUBTITLE_SUFFIXES:
            return PORTUGUESE
        normalised = normalize_language_tag(token)
        if normalised is not None:
            return normalised
    return None


def is_supported_source(language: str | None) -> bool:
    """Only English and Portuguese are supported sources in this MVP."""
    if language is None:
        return False
    return normalize_language_tag(language) in SUPPORTED_SOURCE_LANGUAGES


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
    ``confident=False``. The filename is never consulted. ``transcriber`` is
    accepted so later stages can pass the engine; this function only votes on
    samples already collected.
    """
    del transcriber  # The pipeline extracts samples; this function only votes.
    if override is not None:
        language = normalize_language_tag(override)
        if language is None:
            raise NasSubtitlesError(
                f"source language override {override!r} is not a recognised language tag",
                code=ErrorCode.UNSUPPORTED_LANGUAGE,
                detail={"override": override},
            )
        return LanguageDecision(
            language=language,
            source=LanguageSource.OVERRIDE,
            confident=True,
            reason="cli override",
        )

    tagged = normalize_language_tag(stream.language)
    if tagged is not None:
        return LanguageDecision(
            language=tagged,
            source=LanguageSource.METADATA,
            confident=True,
            probability=None,
            reason="audio stream language tag",
        )

    threshold = config.asr.detection_min_probability
    spoken = tuple(sample for sample in samples if sample.has_speech)
    if not spoken:
        return LanguageDecision(
            language=None,
            source=LanguageSource.DETECTION,
            confident=False,
            samples=tuple(samples),
            reason="no speech in language samples",
        )

    qualified: list[LanguageSample] = []
    for sample in spoken:
        language = normalize_language_tag(sample.language)
        probability = sample.probability
        if language is None or probability is None or probability < threshold:
            continue
        qualified.append(sample)

    languages = {normalize_language_tag(sample.language) for sample in qualified if sample.language}
    languages.discard(None)
    if len(languages) == 1:
        language = next(iter(languages))
        probability = max(sample.probability or 0.0 for sample in qualified)
        return LanguageDecision(
            language=language,
            source=LanguageSource.DETECTION,
            confident=True,
            probability=probability,
            samples=tuple(samples),
            reason="asr samples agreed",
        )
    if len(languages) > 1:
        return LanguageDecision(
            language=None,
            source=LanguageSource.DETECTION,
            confident=False,
            samples=tuple(samples),
            reason="language samples disagreed",
        )
    return LanguageDecision(
        language=None,
        source=LanguageSource.DETECTION,
        confident=False,
        samples=tuple(samples),
        reason="no sample reached the detection probability threshold",
    )
