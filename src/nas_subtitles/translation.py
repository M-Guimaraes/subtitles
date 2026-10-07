"""Local translation with Argos. Owned by stage 5.

Only the direct ``en -> pt`` pair is used. Routing through a third language
is never enabled implicitly, and an empty translation of a non-empty source
is an error rather than a success.
"""

from __future__ import annotations

from collections.abc import Sequence

from .config import AppConfig
from .domain import (
    JobRepository,
    ModelIdentity,
    Transcript,
    TranslatedUnit,
    TranslationUnit,
)

__all__ = [
    "ArgosTranslator",
    "build_translation_units",
    "normalize_for_cache",
    "translate_with_cache",
    "translation_cache_key",
]


class ArgosTranslator:
    """``Translator`` implementation over an installed Argos package."""

    def __init__(self, config: AppConfig, *, model_identity: ModelIdentity) -> None:
        self.config = config
        self._model_identity = model_identity

    @property
    def engine_identity(self) -> str:
        """Engine name, package version and checksum, as one stable token."""
        raise NotImplementedError("translation is implemented in stage 5 (translation)")

    def supports(self, *, source_language: str, target_language: str) -> bool:
        raise NotImplementedError("translation is implemented in stage 5 (translation)")

    def translate(self, units: Sequence[TranslationUnit]) -> tuple[TranslatedUnit, ...]:
        raise NotImplementedError("translation is implemented in stage 5 (translation)")


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
    raise NotImplementedError("translation is implemented in stage 5 (translation)")


def normalize_for_cache(text: str) -> str:
    """Normalisation applied before hashing; versioned by the normalizer constant."""
    raise NotImplementedError("translation is implemented in stage 5 (translation)")


def translation_cache_key(
    *,
    text: str,
    source_language: str,
    target_language: str,
    engine_identity: str,
) -> str:
    """Hash of normalised text, the language pair, engine identity and version."""
    raise NotImplementedError("translation is implemented in stage 5 (translation)")


def translate_with_cache(
    units: Sequence[TranslationUnit],
    *,
    translator: ArgosTranslator,
    repository: JobRepository,
) -> tuple[TranslatedUnit, ...]:
    """Translate the units that are not cached and persist the new results."""
    raise NotImplementedError("translation is implemented in stage 5 (translation)")
