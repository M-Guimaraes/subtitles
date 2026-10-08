"""Local translation with Argos. Owned by stage 5.

Only the direct ``en -> pt`` pair is used. Routing through a third language
is never enabled implicitly, and an empty translation of a non-empty source
is an error rather than a success.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from .config import AppConfig
from .domain import (
    ENGLISH,
    PORTUGUESE,
    TRANSLATION_NORMALIZER_VERSION,
    ErrorCode,
    JobRepository,
    ModelIdentity,
    NasSubtitlesError,
    Transcript,
    TranslatedUnit,
    TranslationCacheEntry,
    TranslationUnit,
    Translator,
    Word,
    stable_digest,
    stable_unit_id,
)
from .language import normalize_language_tag
from .models import configure_argos_environment, configure_stanza_offline, translation_package_path

__all__ = [
    "ArgosTranslator",
    "build_translation_units",
    "normalize_for_cache",
    "translate_with_cache",
    "translation_cache_key",
]

_WHITESPACE = re.compile(r"\s+")
_SENTENCE_END = re.compile(r"[.!?…][\"')\]]*$")
_PAUSE_SECONDS = 0.7


class ArgosTranslator:
    """``Translator`` implementation over an installed Argos package."""

    def __init__(self, config: AppConfig, *, model_identity: ModelIdentity) -> None:
        self.config = config
        self._model_identity = model_identity
        self._loaded = False

    @property
    def engine_identity(self) -> str:
        """Engine name, package version and checksum, as one stable token."""
        return self._model_identity.identity_token()

    def supports(self, *, source_language: str, target_language: str) -> bool:
        source = normalize_language_tag(source_language) or source_language
        target = (normalize_language_tag(target_language) or target_language).split("-")[0]
        if self.config.translation.allow_pivot:
            return False
        return self.config.translation.allows(source_language=source, target_language=target)

    def translate(self, units: Sequence[TranslationUnit]) -> tuple[TranslatedUnit, ...]:
        self._ensure_loaded()
        import argostranslate.translate as argos_translate

        translated: list[TranslatedUnit] = []
        for unit in units:
            source = unit.source_text.strip()
            if not source:
                translated.append(
                    TranslatedUnit(
                        unit_id=unit.unit_id,
                        source_text=unit.source_text,
                        translated_text="",
                        source_language=unit.source_language,
                        target_language=unit.target_language,
                        engine_identity=self.engine_identity,
                    )
                )
                continue
            if not self.supports(
                source_language=unit.source_language, target_language=unit.target_language
            ):
                raise NasSubtitlesError(
                    "no direct translation pair is installed for this language",
                    code=ErrorCode.TRANSLATION_PAIR_MISSING,
                    detail={"source": unit.source_language, "target": unit.target_language},
                )
            try:
                output = argos_translate.translate(
                    source,
                    unit.source_language,
                    normalize_language_tag(unit.target_language) or PORTUGUESE,
                )
            except Exception as exc:
                raise NasSubtitlesError(
                    "Argos failed to translate a unit",
                    code=ErrorCode.EMPTY_TRANSLATION,
                ) from exc
            text = (output or "").strip()
            if not text:
                raise NasSubtitlesError(
                    "translation produced empty text for speech",
                    code=ErrorCode.EMPTY_TRANSLATION,
                    detail={"unit_id": unit.unit_id},
                )
            translated.append(
                TranslatedUnit(
                    unit_id=unit.unit_id,
                    source_text=unit.source_text,
                    translated_text=text,
                    source_language=unit.source_language,
                    target_language=unit.target_language,
                    engine_identity=self.engine_identity,
                )
            )
        return tuple(translated)

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        configure_argos_environment(self.config)
        configure_stanza_offline()
        translation_package_path(self.config, source=ENGLISH, target=PORTUGUESE)
        import argostranslate.translate  # noqa: F401  # registers installed packages

        configure_argos_environment(self.config)
        self._loaded = True


def build_translation_units(
    config: AppConfig,
    transcript: Transcript,
    *,
    fingerprint_digest: str,
    target_language: str,
) -> tuple[TranslationUnit, ...]:
    """Group words into sentence-like units by punctuation, pause and duration.

    Each unit gets a stable ID so a restart reuses cached translations and the
    renderer can map cues back to their source.
    """
    max_duration = config.subtitles.max_duration_seconds
    words = list(transcript.words)
    if not words:
        for segment in transcript.segments:
            if segment.text.strip():
                words.extend(segment.words or ())
        if not words:
            fallback: list[TranslationUnit] = []
            for index, segment in enumerate(transcript.segments):
                text = segment.text.strip()
                if not text:
                    continue
                fallback.append(
                    _unit_from_text(
                        text,
                        start=segment.start_seconds,
                        end=segment.end_seconds,
                        index=index,
                        fingerprint_digest=fingerprint_digest,
                        source_language=transcript.language,
                        target_language=target_language,
                        word_count=len(text.split()),
                    )
                )
            return tuple(fallback)

    units: list[TranslationUnit] = []
    current: list[Word] = []
    current_start = 0.0
    for word in words:
        if not current:
            current = [word]
            current_start = word.start_seconds
            continue
        previous = current[-1]
        pause = word.start_seconds - previous.end_seconds
        duration = word.end_seconds - current_start
        should_break = (
            pause >= _PAUSE_SECONDS
            or duration > max_duration
            or _SENTENCE_END.search(previous.text.strip()) is not None
        )
        if should_break:
            units.append(
                _unit_from_words(
                    current,
                    index=len(units),
                    fingerprint_digest=fingerprint_digest,
                    source_language=transcript.language,
                    target_language=target_language,
                )
            )
            current = [word]
            current_start = word.start_seconds
        else:
            current.append(word)
    if current:
        units.append(
            _unit_from_words(
                current,
                index=len(units),
                fingerprint_digest=fingerprint_digest,
                source_language=transcript.language,
                target_language=target_language,
            )
        )
    return tuple(units)


def normalize_for_cache(text: str) -> str:
    """Normalisation applied before hashing; versioned by the normalizer constant."""
    return _WHITESPACE.sub(" ", text).strip().casefold()


def translation_cache_key(
    *,
    text: str,
    source_language: str,
    target_language: str,
    engine_identity: str,
) -> str:
    """Hash of normalised text, the language pair, engine identity and version."""
    return stable_digest(
        {
            "text": normalize_for_cache(text),
            "source": normalize_language_tag(source_language) or source_language,
            "target": normalize_language_tag(target_language) or target_language,
            "engine": engine_identity,
            "normalizer": TRANSLATION_NORMALIZER_VERSION,
        }
    )


def translate_with_cache(
    units: Sequence[TranslationUnit],
    *,
    translator: Translator,
    repository: JobRepository,
) -> tuple[TranslatedUnit, ...]:
    """Translate the units that are not cached and persist the new results."""
    missing: list[TranslationUnit] = []
    cached: dict[str, TranslatedUnit] = {}
    for unit in units:
        key = translation_cache_key(
            text=unit.source_text,
            source_language=unit.source_language,
            target_language=unit.target_language,
            engine_identity=translator.engine_identity,
        )
        entry = repository.get_translation(key)
        if entry is None or entry.engine_identity != translator.engine_identity:
            missing.append(unit)
            continue
        cached[unit.unit_id] = TranslatedUnit(
            unit_id=unit.unit_id,
            source_text=unit.source_text,
            translated_text=entry.translated_text,
            source_language=unit.source_language,
            target_language=unit.target_language,
            engine_identity=entry.engine_identity,
            from_cache=True,
        )
    if missing:
        fresh = translator.translate(missing)
        for unit, translated in zip(missing, fresh, strict=True):
            if not translated.translated_text.strip() and unit.source_text.strip():
                raise NasSubtitlesError(
                    "translation produced empty text for speech",
                    code=ErrorCode.EMPTY_TRANSLATION,
                    detail={"unit_id": unit.unit_id},
                )
            key = translation_cache_key(
                text=unit.source_text,
                source_language=unit.source_language,
                target_language=unit.target_language,
                engine_identity=translator.engine_identity,
            )
            repository.put_translation(
                TranslationCacheEntry(
                    cache_key=key,
                    translated_text=translated.translated_text,
                    engine_identity=translator.engine_identity,
                )
            )
            cached[unit.unit_id] = translated
    return tuple(cached[unit.unit_id] for unit in units)


def _unit_from_words(
    words: Sequence[Word],
    *,
    index: int,
    fingerprint_digest: str,
    source_language: str,
    target_language: str,
) -> TranslationUnit:
    text = " ".join(word.text for word in words).strip()
    return _unit_from_text(
        text,
        start=words[0].start_seconds,
        end=words[-1].end_seconds,
        index=index,
        fingerprint_digest=fingerprint_digest,
        source_language=source_language,
        target_language=target_language,
        word_count=len(words),
    )


def _unit_from_text(
    text: str,
    *,
    start: float,
    end: float,
    index: int,
    fingerprint_digest: str,
    source_language: str,
    target_language: str,
    word_count: int,
) -> TranslationUnit:
    normalised = normalize_for_cache(text)
    return TranslationUnit(
        unit_id=stable_unit_id(
            fingerprint_digest=fingerprint_digest,
            index=index,
            start_seconds=start,
            end_seconds=end,
            normalized_text=normalised,
        ),
        source_text=text,
        start_seconds=start,
        end_seconds=end,
        source_language=normalize_language_tag(source_language) or source_language,
        target_language=target_language,
        word_count=word_count,
    )
