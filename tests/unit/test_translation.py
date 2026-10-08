"""Translation units, empty-output errors and cache identity."""

from __future__ import annotations

from pathlib import Path

import pytest

from nas_subtitles.config import AppConfig
from nas_subtitles.domain import (
    ErrorCode,
    ModelIdentity,
    ModelKind,
    NasSubtitlesError,
    Transcript,
    TranslatedUnit,
    TranslationUnit,
    Word,
)
from nas_subtitles.repository import open_repository
from nas_subtitles.translation import (
    ArgosTranslator,
    build_translation_units,
    translate_with_cache,
    translation_cache_key,
)


class _FakeTranslator(ArgosTranslator):
    def __init__(self, config: AppConfig, *, empty: bool = False) -> None:
        super().__init__(
            config,
            model_identity=ModelIdentity(
                kind=ModelKind.TRANSLATION, name="en-pt", path=Path("/models/argos/en_pt")
            ),
        )
        self.empty = empty
        self.calls = 0

    def translate(self, units):  # type: ignore[override]
        self.calls += 1
        if self.empty:
            return tuple(
                TranslatedUnit(
                    unit_id=unit.unit_id,
                    source_text=unit.source_text,
                    translated_text="",
                    source_language=unit.source_language,
                    target_language=unit.target_language,
                    engine_identity=self.engine_identity,
                )
                for unit in units
            )
        return tuple(
            TranslatedUnit(
                unit_id=unit.unit_id,
                source_text=unit.source_text,
                translated_text=f"pt:{unit.source_text}",
                source_language=unit.source_language,
                target_language=unit.target_language,
                engine_identity=self.engine_identity,
            )
            for unit in units
        )


def test_units_split_on_punctuation_and_pauses(config: AppConfig) -> None:
    transcript = Transcript(
        language="en",
        duration_seconds=20,
        words=(
            Word(text="Hello", start_seconds=0.0, end_seconds=0.4),
            Word(text="there.", start_seconds=0.4, end_seconds=0.8),
            Word(text="Later", start_seconds=3.0, end_seconds=3.4),
        ),
    )
    units = build_translation_units(
        config, transcript, fingerprint_digest="abc", target_language="pt-BR"
    )
    assert len(units) == 2
    assert units[0].source_text == "Hello there."
    assert units[1].source_text == "Later"


def test_empty_translation_of_speech_is_an_error(config: AppConfig) -> None:
    repo = open_repository(config)
    unit = TranslationUnit(
        unit_id="u1",
        source_text="hello",
        start_seconds=0,
        end_seconds=1,
        source_language="en",
        target_language="pt-BR",
        word_count=1,
    )
    translator = _FakeTranslator(config, empty=True)
    with pytest.raises(NasSubtitlesError) as raised:
        translate_with_cache((unit,), translator=translator, repository=repo)
    repo.close()
    assert raised.value.code is ErrorCode.EMPTY_TRANSLATION


def test_argos_translator_calls_backend_with_en_pb_for_pt_br(
    config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, str, str]] = []

    def fake_translate(text: str, source: str, target: str) -> str:
        calls.append((text, source, target))
        return "texto"

    import argostranslate.translate as argos_translate

    monkeypatch.setattr(ArgosTranslator, "_ensure_loaded", lambda self: None)
    monkeypatch.setattr(argos_translate, "translate", fake_translate)
    translator = ArgosTranslator(
        config,
        model_identity=ModelIdentity(
            kind=ModelKind.TRANSLATION,
            name="translation:en:pt-BR",
            path=Path("/models/argos/en_pb"),
        ),
    )
    unit = TranslationUnit(
        unit_id="u1",
        source_text="Hello.",
        start_seconds=0,
        end_seconds=1,
        source_language="en",
        target_language="pt-BR",
        word_count=1,
    )
    result = translator.translate((unit,))
    assert calls == [("Hello.", "en", "pb")]
    assert result[0].target_language == "pt-BR"
    assert result[0].translated_text == "texto"


def test_cache_key_distinguishes_argos_en_pb_from_en_pt() -> None:
    shared = {"text": "hello", "source_language": "en", "engine_identity": "same-engine"}
    key_br = translation_cache_key(target_language="pt-BR", **shared)
    key_eu = translation_cache_key(target_language="pt", **shared)
    assert key_br != key_eu


def test_cache_is_skipped_when_engine_identity_changes(config: AppConfig) -> None:
    repo = open_repository(config)
    unit = TranslationUnit(
        unit_id="u1",
        source_text="hello",
        start_seconds=0,
        end_seconds=1,
        source_language="en",
        target_language="pt-BR",
        word_count=1,
    )
    first = _FakeTranslator(config)
    translate_with_cache((unit,), translator=first, repository=repo)
    again = translate_with_cache((unit,), translator=first, repository=repo)
    assert again[0].from_cache is True
    assert first.calls == 1
    other = ArgosTranslator(
        config,
        model_identity=ModelIdentity(
            kind=ModelKind.TRANSLATION,
            name="en-pt",
            path=Path("/models/argos/en_pt"),
            version="other",
        ),
    )
    key_old = translation_cache_key(
        text="hello",
        source_language="en",
        target_language="pt-BR",
        engine_identity=first.engine_identity,
    )
    key_new = translation_cache_key(
        text="hello",
        source_language="en",
        target_language="pt-BR",
        engine_identity=other.engine_identity,
    )
    repo.close()
    assert key_old != key_new
