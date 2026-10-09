"""Configuration schema, defaults and the config hashes."""

from __future__ import annotations

from pathlib import Path

import pytest

from nas_subtitles.config import AppConfig, load_config, root_id_for
from nas_subtitles.domain import (
    ConfigurationError,
    ExistingSubtitlePolicy,
    PipelineStage,
    PublishMode,
)


def test_example_config_matches_the_documented_defaults(config: AppConfig) -> None:
    assert config.publish_mode is PublishMode.STAGING
    assert config.existing_subtitle_policy is ExistingSubtitlePolicy.SKIP
    assert config.target_language == "pt-BR"
    assert config.languages.source == "auto"
    assert config.languages.target == "pt-BR"
    assert config.languages.low_confidence == "review"
    assert config.audio.stream == "auto"
    assert config.audio.preferred_languages == ("en", "ja")
    assert config.scan_interval_seconds == 600
    assert config.stability_window_seconds == 600
    assert config.minimum_file_age_seconds == 600
    assert config.minimum_free_work_gib == 3
    assert config.worker.concurrency == 1
    assert config.worker.cpu_threads == 2
    assert config.worker.retry_delays_seconds == (300, 1800, 7200)
    assert config.worker.stale_lease_seconds == 180
    assert config.worker.heartbeat_seconds == 30
    assert config.asr.model == "small"
    assert config.asr.compute_type == "int8"
    assert config.asr.beam_size == 5
    assert config.asr.word_timestamps is True
    assert config.asr.vad_filter is True
    assert config.asr.condition_on_previous_text is False
    assert config.asr.chunk_seconds == 300
    assert config.asr.overlap_seconds == 2
    assert config.asr.detection_min_probability == pytest.approx(0.80)
    assert config.translation.engine == "argos"
    assert config.translation.allowed_pairs == ("en:pt",)
    assert config.translation.allow_pivot is False
    assert config.subtitles.max_lines == 2
    assert config.subtitles.max_chars_per_line == 42
    assert config.subtitles.target_max_chars_per_second == 20
    assert config.subtitles.min_duration_seconds == pytest.approx(1.0)
    assert config.subtitles.max_duration_seconds == pytest.approx(7.0)
    assert config.subtitles.write_source_srt is False
    assert config.dashboard.bind == "127.0.0.1"
    assert config.dashboard.port == 8787
    assert config.dashboard.token is None
    assert config.webhooks.bind == "127.0.0.1"
    assert config.webhooks.port == 8788
    assert config.webhooks.token is None
    assert config.webhooks.path_maps == ()


def test_derived_paths_live_under_state(config: AppConfig) -> None:
    assert config.database_path == config.state_dir / "jobs.sqlite3"
    assert config.lock_path.parent == config.state_dir
    # The manifest is provenance, so it stays in state rather than next to media.
    assert config.manifests_dir.parent == config.state_dir


def test_unknown_key_is_rejected(tmp_path: Path, config_path: Path) -> None:
    broken = tmp_path / "broken.yaml"
    broken.write_text(config_path.read_text(encoding="utf-8") + "typo_key: 1\n", encoding="utf-8")
    with pytest.raises(ConfigurationError, match="typo_key"):
        load_config(broken)


def test_relative_paths_are_rejected(tmp_path: Path, config_path: Path) -> None:
    broken = tmp_path / "relative.yaml"
    text = config_path.read_text(encoding="utf-8").replace(
        f"state_dir: {tmp_path / 'state'}", "state_dir: ./state"
    )
    broken.write_text(text, encoding="utf-8")
    with pytest.raises(ConfigurationError, match="absolute path"):
        load_config(broken)


def test_english_only_model_is_rejected(tmp_path: Path, config_path: Path) -> None:
    broken = tmp_path / "en_only.yaml"
    text = config_path.read_text(encoding="utf-8").replace("model: small", "model: small.en")
    broken.write_text(text, encoding="utf-8")
    with pytest.raises(ConfigurationError, match="English-only"):
        load_config(broken)


def test_pivot_translation_is_rejected(tmp_path: Path, config_path: Path) -> None:
    broken = tmp_path / "pivot.yaml"
    text = config_path.read_text(encoding="utf-8").replace(
        "allow_pivot: false", "allow_pivot: true"
    )
    broken.write_text(text, encoding="utf-8")
    with pytest.raises(ConfigurationError, match="allow_pivot"):
        load_config(broken)


def test_lease_must_outlive_heartbeat(tmp_path: Path, config_path: Path) -> None:
    broken = tmp_path / "lease.yaml"
    text = config_path.read_text(encoding="utf-8").replace(
        "stale_lease_seconds: 180", "stale_lease_seconds: 20"
    )
    broken.write_text(text, encoding="utf-8")
    with pytest.raises(ConfigurationError, match="stale_lease_seconds"):
        load_config(broken)


def test_working_directory_inside_a_media_root_is_rejected(
    tmp_path: Path, config_path: Path
) -> None:
    broken = tmp_path / "inside.yaml"
    text = config_path.read_text(encoding="utf-8").replace(
        f"output_dir: {tmp_path / 'output'}", f"output_dir: {tmp_path / 'media' / 'subs'}"
    )
    broken.write_text(text, encoding="utf-8")
    with pytest.raises(ConfigurationError, match="never write into the library"):
        load_config(broken)


def test_missing_file_reports_the_path(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="not found"):
        load_config(tmp_path / "absent.yaml")


def test_root_ids_distinguish_roots_with_the_same_basename() -> None:
    first = root_id_for(Path("/a/series"))
    second = root_id_for(Path("/b/series"))
    assert first != second
    assert first.startswith("series-")
    assert root_id_for(Path("/a/series")) == first


def test_pipeline_hash_is_deterministic_and_ignores_local_paths(
    config: AppConfig, tmp_path: Path
) -> None:
    moved = config.model_copy(update={"work_dir": tmp_path / "elsewhere"})
    assert moved.pipeline_config_hash == config.pipeline_config_hash


def test_pipeline_hash_distinguishes_argos_en_pb_from_en_pt(
    config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Public target stays pt-BR; the Argos backend pair still participates in the hash."""
    pipeline_before = config.pipeline_config_hash
    translate_before = config.stage_config_hash(PipelineStage.TRANSLATE)
    transcribe_before = config.stage_config_hash(PipelineStage.TRANSCRIBE)

    monkeypatch.setattr(
        "nas_subtitles.models.to_argos_language_code",
        lambda language: "pt" if language == "pt-BR" else language,
    )
    assert config.target_language == "pt-BR"
    assert config.pipeline_config_hash != pipeline_before
    assert config.stage_config_hash(PipelineStage.TRANSLATE) != translate_before
    assert config.stage_config_hash(PipelineStage.TRANSCRIBE) == transcribe_before


def test_dashboard_settings_do_not_change_pipeline_hash(config: AppConfig) -> None:
    changed = config.model_copy(
        update={"dashboard": config.dashboard.model_copy(update={"port": 9999, "bind": "0.0.0.0"})}
    )
    assert changed.pipeline_config_hash == config.pipeline_config_hash


def test_webhook_path_map_prefixes_must_be_absolute(tmp_path: Path, config_path: Path) -> None:
    broken = tmp_path / "webhook-map.yaml"
    text = config_path.read_text(encoding="utf-8").replace(
        "path_maps: []",
        "path_maps:\n    - host_prefix: relative/host\n      container_prefix: /media",
    )
    broken.write_text(text, encoding="utf-8")
    with pytest.raises(ConfigurationError, match="absolute path"):
        load_config(broken)


def test_webhook_settings_do_not_change_pipeline_hash(config: AppConfig) -> None:
    changed = config.model_copy(
        update={
            "webhooks": config.webhooks.model_copy(
                update={"port": 9998, "bind": "0.0.0.0", "token": "secret"}
            )
        }
    )
    assert changed.pipeline_config_hash == config.pipeline_config_hash


def test_pipeline_hash_changes_with_output_affecting_settings(config: AppConfig) -> None:
    changed = config.model_copy(
        update={"subtitles": config.subtitles.model_copy(update={"max_chars_per_line": 40})}
    )
    assert changed.pipeline_config_hash != config.pipeline_config_hash


def test_stage_hashes_are_isolated(config: AppConfig) -> None:
    """Changing a rendering setting must not invalidate transcription work."""
    changed = config.model_copy(
        update={"subtitles": config.subtitles.model_copy(update={"max_chars_per_line": 40})}
    )
    assert changed.stage_config_hash(PipelineStage.TRANSCRIBE) == config.stage_config_hash(
        PipelineStage.TRANSCRIBE
    )
    assert changed.stage_config_hash(PipelineStage.RENDER) != config.stage_config_hash(
        PipelineStage.RENDER
    )


def test_every_stage_has_a_hash(config: AppConfig) -> None:
    hashes = {stage: config.stage_config_hash(stage) for stage in PipelineStage}
    assert len(hashes) == len(PipelineStage)
    assert all(len(value) == 64 for value in hashes.values())


def test_scan_interval_must_be_positive(tmp_path: Path, config_path: Path) -> None:
    broken = tmp_path / "scan.yaml"
    text = config_path.read_text(encoding="utf-8").replace(
        "scan_interval_seconds: 600", "scan_interval_seconds: 0"
    )
    broken.write_text(text, encoding="utf-8")
    with pytest.raises(ConfigurationError, match="scan_interval_seconds"):
        load_config(broken)


def test_stability_window_must_not_be_negative(tmp_path: Path, config_path: Path) -> None:
    broken = tmp_path / "stability.yaml"
    text = config_path.read_text(encoding="utf-8").replace(
        "stability_window_seconds: 600", "stability_window_seconds: -1"
    )
    broken.write_text(text, encoding="utf-8")
    with pytest.raises(ConfigurationError, match="stability_window_seconds"):
        load_config(broken)


def test_legacy_top_level_target_language_still_loads(tmp_path: Path, config_path: Path) -> None:
    text = config_path.read_text(encoding="utf-8")
    text = text.replace(
        "languages:\n  source: auto\n  target: pt-BR\n  low_confidence: review\n",
        "target_language: pt-BR\n",
    )
    legacy = tmp_path / "legacy.yaml"
    legacy.write_text(text, encoding="utf-8")
    loaded = load_config(legacy)
    assert loaded.target_language == "pt-BR"
    assert loaded.languages.source == "auto"


def test_argos_backend_code_is_rejected_in_public_language_config(
    tmp_path: Path, config_path: Path
) -> None:
    broken = tmp_path / "pb.yaml"
    text = config_path.read_text(encoding="utf-8").replace("source: auto", "source: pb")
    broken.write_text(text, encoding="utf-8")
    with pytest.raises(ConfigurationError, match="pb"):
        load_config(broken)


def test_argos_backend_code_is_rejected_in_preferred_languages(
    tmp_path: Path, config_path: Path
) -> None:
    broken = tmp_path / "pb-audio.yaml"
    text = config_path.read_text(encoding="utf-8").replace(
        "preferred_languages:\n    - en\n    - ja\n",
        "preferred_languages:\n    - en\n    - pb\n",
    )
    broken.write_text(text, encoding="utf-8")
    with pytest.raises(ConfigurationError, match="pb"):
        load_config(broken)


def test_language_and_audio_settings_participate_in_pipeline_identity(config: AppConfig) -> None:
    before = config.pipeline_config_hash
    detect_before = config.stage_config_hash(PipelineStage.DETECT_LANGUAGE)
    transcribe_before = config.stage_config_hash(PipelineStage.TRANSCRIBE)
    changed = config.model_copy(
        update={
            "languages": config.languages.model_copy(update={"source": "en"}),
            "audio": config.audio.model_copy(update={"preferred_languages": ("en",)}),
        }
    )
    assert changed.pipeline_config_hash != before
    assert changed.stage_config_hash(PipelineStage.DETECT_LANGUAGE) != detect_before
    assert changed.stage_config_hash(PipelineStage.TRANSCRIBE) == transcribe_before
    assert changed.target_language == "pt-BR"


def test_transcribe_hash_includes_word_dedupe_version(
    config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = config.stage_config_hash(PipelineStage.TRANSCRIBE)
    pipeline_before = config.pipeline_config_hash
    merge_before = config.stage_config_hash(PipelineStage.MERGE)
    monkeypatch.setattr("nas_subtitles.config.TRANSCRIBE_WORD_DEDUPE_VERSION", 3)
    assert config.stage_config_hash(PipelineStage.TRANSCRIBE) != before
    assert config.pipeline_config_hash != pipeline_before
    assert config.stage_config_hash(PipelineStage.MERGE) == merge_before
