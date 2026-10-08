"""Language tag folding, subtitle suffixes and source-language decisions."""

from __future__ import annotations

import pytest

from nas_subtitles.config import AppConfig
from nas_subtitles.domain import (
    AudioStreamInfo,
    ErrorCode,
    LanguageSample,
    LanguageSource,
    NasSubtitlesError,
)
from nas_subtitles.language import (
    decide_source_language,
    effective_source_override,
    is_supported_source,
    normalize_language_tag,
    subtitle_suffix_language,
    translation_is_required,
)


def test_normalize_folds_iso_and_locale_tags() -> None:
    assert normalize_language_tag("eng") == "en"
    assert normalize_language_tag("en-US") == "en"
    assert normalize_language_tag("en_GB") == "en"
    assert normalize_language_tag("por") == "pt"
    assert normalize_language_tag("pt-BR") == "pt"
    assert normalize_language_tag("pob") == "pt"
    assert normalize_language_tag("jpn") == "ja"
    assert normalize_language_tag("und") is None
    assert normalize_language_tag("pb") is None
    assert normalize_language_tag("") is None
    assert normalize_language_tag(None) is None


def test_srt_without_suffix_has_no_language() -> None:
    assert subtitle_suffix_language("episode.srt") is None
    assert subtitle_suffix_language("episode.en.srt") == "en"
    assert subtitle_suffix_language("episode.pt-BR.srt") == "pt"
    assert subtitle_suffix_language("episode.forced.pt.srt") == "pt"
    assert subtitle_suffix_language("episode.por.ass") == "pt"


def test_only_english_and_portuguese_are_supported_sources() -> None:
    assert is_supported_source("en")
    assert is_supported_source("pt-BR")
    assert not is_supported_source("es")
    assert not is_supported_source(None)


def test_override_beats_metadata(config: AppConfig) -> None:
    stream = AudioStreamInfo(index=1, language="en")
    decision = decide_source_language(config, stream=stream, override="pt")
    assert decision.source is LanguageSource.OVERRIDE
    assert decision.language == "pt"
    assert decision.confident is True


def test_filename_is_never_used_as_language_evidence(config: AppConfig) -> None:
    stream = AudioStreamInfo(index=1, language=None, title="Movie.Portuguese.Track")
    decision = decide_source_language(config, stream=stream, samples=())
    assert decision.language is None
    assert decision.confident is False


def test_sample_disagreement_is_not_confident(config: AppConfig) -> None:
    samples = (
        LanguageSample(
            offset_seconds=0, duration_seconds=8, language="en", probability=0.9, has_speech=True
        ),
        LanguageSample(
            offset_seconds=60, duration_seconds=8, language="pt", probability=0.91, has_speech=True
        ),
    )
    stream = AudioStreamInfo(index=1)
    decision = decide_source_language(config, stream=stream, samples=samples)
    assert decision.confident is False
    assert decision.language is None


def test_silent_samples_are_ignored(config: AppConfig) -> None:
    samples = (
        LanguageSample(
            offset_seconds=0, duration_seconds=8, language="en", probability=0.99, has_speech=False
        ),
        LanguageSample(
            offset_seconds=60, duration_seconds=8, language="en", probability=0.9, has_speech=True
        ),
    )
    stream = AudioStreamInfo(index=1)
    decision = decide_source_language(config, stream=stream, samples=samples)
    assert decision.confident is True
    assert decision.language == "en"


def test_metadata_alone_is_not_a_confident_decision(config: AppConfig) -> None:
    stream = AudioStreamInfo(index=1, language="en")
    decision = decide_source_language(config, stream=stream, samples=())
    assert decision.confident is False
    assert decision.language is None


def test_automatic_english_detection_without_stream_language(config: AppConfig) -> None:
    samples = (
        LanguageSample(
            offset_seconds=0, duration_seconds=8, language="en", probability=0.92, has_speech=True
        ),
        LanguageSample(
            offset_seconds=60, duration_seconds=8, language="eng", probability=0.88, has_speech=True
        ),
    )
    stream = AudioStreamInfo(index=1, language=None)
    decision = decide_source_language(config, stream=stream, samples=samples)
    assert decision.confident is True
    assert decision.language == "en"
    assert decision.source is LanguageSource.DETECTION
    assert decision.probability == pytest.approx(0.92)


def test_metadata_and_asr_agreement_is_confident(config: AppConfig) -> None:
    samples = (
        LanguageSample(
            offset_seconds=0, duration_seconds=8, language="pt", probability=0.9, has_speech=True
        ),
    )
    stream = AudioStreamInfo(index=1, language="pt-BR")
    decision = decide_source_language(config, stream=stream, samples=samples)
    assert decision.confident is True
    assert decision.language == "pt"
    assert decision.reason == "metadata and asr agreed"


def test_metadata_and_asr_disagreement_is_not_confident(config: AppConfig) -> None:
    samples = (
        LanguageSample(
            offset_seconds=0, duration_seconds=8, language="en", probability=0.95, has_speech=True
        ),
    )
    stream = AudioStreamInfo(index=1, language="ja")
    decision = decide_source_language(config, stream=stream, samples=samples)
    assert decision.confident is False
    assert decision.language is None


def test_low_confidence_samples_are_not_assumed_english(config: AppConfig) -> None:
    samples = (
        LanguageSample(
            offset_seconds=0, duration_seconds=8, language="en", probability=0.4, has_speech=True
        ),
    )
    stream = AudioStreamInfo(index=1, language="en")
    decision = decide_source_language(config, stream=stream, samples=samples)
    assert decision.confident is False
    assert decision.language is None


def test_backend_code_is_not_a_public_override(config: AppConfig) -> None:
    stream = AudioStreamInfo(index=1, language="en")
    with pytest.raises(NasSubtitlesError) as raised:
        decide_source_language(config, stream=stream, override="pb")
    assert raised.value.code is ErrorCode.UNSUPPORTED_LANGUAGE


def test_pt_and_pt_br_skip_translation_without_using_argos_pb() -> None:
    assert translation_is_required(source_language="en", target_language="pt-BR") is True
    assert translation_is_required(source_language="pt", target_language="pt-BR") is False
    assert translation_is_required(source_language="pt-BR", target_language="pt-BR") is False
    assert translation_is_required(source_language="pb", target_language="pt-BR") is True


def test_configured_source_is_an_override_when_not_auto(config: AppConfig) -> None:
    forced = config.model_copy(
        update={"languages": config.languages.model_copy(update={"source": "pt"})}
    )
    assert effective_source_override(forced, job_override=None) == "pt"
    assert effective_source_override(forced, job_override="en") == "en"
    assert effective_source_override(config, job_override=None) is None
