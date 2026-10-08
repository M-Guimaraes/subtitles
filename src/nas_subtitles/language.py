"""Language tag normalisation and source-language decision. Owned by stage 3.

A filename is never evidence of the spoken language, and the language is
never asked for interactively: without enough confidence the job follows
``languages.low_confidence`` (review) and the queue moves on.

Deterministic policy when ``languages.source`` is ``auto``:

1. An explicit override (CLI ``--source-language`` or ``languages.source``
   other than ``auto``) wins; ASR is not consulted.
2. Stream language tags are a candidate only. Missing, unknown, ``und``, or
   Argos-only codes (``pb``) are treated as absent. Metadata alone is never
   a confident decision.
3. ASR samples without speech are ignored. Qualifying samples must reach
   ``asr.detection_min_probability`` and agree on one public language family.
4. If ASR is confident and metadata is absent, the ASR language is used.
5. If ASR is confident and metadata agrees (same public family, for example
   ``pt`` and ``pt-BR``), the ASR language is used.
6. If ASR is confident and metadata disagrees, the decision is not confident.
7. If ASR is not confident, the decision is not confident even when metadata
   exists.

Translation is skipped when the decided source and ``languages.target`` share
the same public language family. Comparison uses public identifiers only;
Argos ``pb`` is never treated as equivalent to ``pt-BR``.
"""

from __future__ import annotations

from collections.abc import Sequence

from .config import SOURCE_LANGUAGE_AUTO, AppConfig
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
    "effective_source_override",
    "is_backend_only_code",
    "is_supported_source",
    "normalize_language_tag",
    "public_language_family",
    "subtitle_suffix_language",
    "translation_is_required",
]

_SUBTITLE_EXTENSIONS = frozenset({".srt", ".vtt", ".ass"})

# Argos names Brazilian Portuguese ``pb``. That code is never a public tag.
_BACKEND_ONLY_CODES = frozenset({"pb"})

# Fold common ISO 639-1/2/3 and locale tags onto the two-letter codes we use.
_TAG_ALIASES: dict[str, str] = {
    "en": ENGLISH,
    "eng": ENGLISH,
    "pt": PORTUGUESE,
    "por": PORTUGUESE,
    "pob": PORTUGUESE,  # Brazilian Portuguese, still Portuguese for routing.
    "ja": "ja",
    "jpn": "ja",
}


def is_backend_only_code(tag: str | None) -> bool:
    """True for engine codes that must not appear in config or filenames."""
    if tag is None:
        return False
    lowered = tag.strip().replace("_", "-").lower()
    if not lowered:
        return False
    primary = lowered.split("-", 1)[0]
    return lowered in _BACKEND_ONLY_CODES or primary in _BACKEND_ONLY_CODES


def normalize_language_tag(tag: str | None) -> str | None:
    """Fold ISO 639-1/2/3 and locale tags to a two-letter public code.

    ``eng``/``en``/``en-US`` all become ``en``; ``por``/``pt``/``pob``/``pt-BR``
    all become ``pt``. Argos ``pb`` is rejected rather than folded onto ``pt``.
    Unknown or empty tags return ``None``.
    """
    if tag is None:
        return None
    stripped = tag.strip()
    if not stripped:
        return None
    lowered = stripped.replace("_", "-").lower()
    if lowered in {"und", "unknown", "unk", "zxx"}:
        return None
    if is_backend_only_code(lowered):
        return None
    primary = lowered.split("-", 1)[0]
    if primary in _TAG_ALIASES:
        return _TAG_ALIASES[primary]
    if len(primary) == 2 and primary.isalpha():
        return primary
    return None


def public_language_family(tag: str | None) -> str | None:
    """Public language family for routing. Never maps Argos ``pb`` onto ``pt``."""
    return normalize_language_tag(tag)


def translation_is_required(*, source_language: str, target_language: str) -> bool:
    """False when source and target are the same public language family.

    ``pt`` and ``pt-BR`` skip translation. ``en`` and ``pt-BR`` do not.
    Backend code ``pb`` is not a public identifier, so it never matches
    ``pt-BR`` and never authorises a skip by itself.
    """
    source_family = public_language_family(source_language)
    target_family = public_language_family(target_language)
    if source_family is None or target_family is None:
        return True
    return source_family != target_family


def effective_source_override(config: AppConfig, *, job_override: str | None) -> str | None:
    """CLI job override wins; otherwise a configured non-``auto`` source."""
    if job_override is not None and job_override.strip():
        return job_override
    source = config.languages.source
    if source == SOURCE_LANGUAGE_AUTO:
        return None
    return source


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
    """Resolve the source language from override, then metadata plus ASR.

    Samples without speech are ignored. Disagreement between samples, a miss
    against ``asr.detection_min_probability``, or a clash with stream metadata
    yields ``confident=False``. The filename is never consulted.
    ``transcriber`` is accepted so later stages can pass the engine; this
    function only votes on samples already collected.
    """
    del transcriber  # The pipeline extracts samples; this function only votes.
    if override is not None:
        if is_backend_only_code(override):
            raise NasSubtitlesError(
                "source language override must be a public identifier; "
                "Argos backend code 'pb' is not accepted",
                code=ErrorCode.UNSUPPORTED_LANGUAGE,
                detail={"override": override},
            )
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

    metadata_language = _stream_metadata_language(stream)
    asr_language, asr_probability, asr_reason = _asr_vote(
        samples, threshold=config.asr.detection_min_probability
    )

    if not samples:
        return LanguageDecision(
            language=None,
            source=LanguageSource.DETECTION,
            confident=False,
            samples=tuple(samples),
            reason="metadata is a candidate; awaiting asr samples",
        )

    if asr_language is not None:
        if metadata_language is None:
            return LanguageDecision(
                language=asr_language,
                source=LanguageSource.DETECTION,
                confident=True,
                probability=asr_probability,
                samples=tuple(samples),
                reason="asr samples agreed",
            )
        if public_language_family(asr_language) == public_language_family(metadata_language):
            return LanguageDecision(
                language=asr_language,
                source=LanguageSource.DETECTION,
                confident=True,
                probability=asr_probability,
                samples=tuple(samples),
                reason="metadata and asr agreed",
            )
        return LanguageDecision(
            language=None,
            source=LanguageSource.DETECTION,
            confident=False,
            probability=asr_probability,
            samples=tuple(samples),
            reason="metadata and asr disagreed",
        )

    return LanguageDecision(
        language=None,
        source=LanguageSource.DETECTION,
        confident=False,
        samples=tuple(samples),
        reason=asr_reason,
    )


def _stream_metadata_language(stream: AudioStreamInfo) -> str | None:
    return normalize_language_tag(stream.language) or normalize_language_tag(
        stream.raw_language_tag
    )


def _asr_vote(
    samples: Sequence[LanguageSample], *, threshold: float
) -> tuple[str | None, float | None, str]:
    spoken = tuple(sample for sample in samples if sample.has_speech)
    if not spoken:
        return None, None, "no speech in language samples"

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
        return language, probability, "asr samples agreed"
    if len(languages) > 1:
        return None, None, "language samples disagreed"
    return None, None, "no sample reached the detection probability threshold"
