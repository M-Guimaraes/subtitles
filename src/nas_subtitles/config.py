"""Typed configuration: the YAML contract, its validation and its hashes.

The schema mirrors ``config/config.example.yaml`` field by field. Unknown keys
are rejected so a typo never silently falls back to a default.

Two families of hashes are produced here and consumed elsewhere:

``pipeline_config_hash``
    Identifies the settings that change *what* the pipeline would produce. It
    is part of the ``jobs`` uniqueness key, so editing the ASR or subtitle
    settings creates a new job instead of reusing a stale result.

``stage_config_hash(stage)``
    Narrower, per stage. A checkpoint is only reusable when the hash of its
    own stage still matches, so changing the subtitle width does not throw
    away hours of transcription.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Literal, assert_never

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from .domain import (
    AUDIO_CHANNELS,
    AUDIO_SAMPLE_FORMAT,
    AUDIO_SAMPLE_RATE_HZ,
    TRANSCRIBE_WORD_DEDUPE_VERSION,
    TRANSLATION_NORMALIZER_VERSION,
    ConfigurationError,
    PipelineStage,
    PublishMode,
    stable_digest,
)

__all__ = [
    "ALLOWED_ASR_MODELS",
    "CONFIG_HASH_SCHEMA_VERSION",
    "DEFAULT_CONFIG_PATH",
    "AppConfig",
    "AsrConfig",
    "MediaRoot",
    "SubtitlesConfig",
    "TranslationConfig",
    "WorkerConfig",
    "load_config",
    "root_id_for",
]

DEFAULT_CONFIG_PATH = Path("/config/config.yaml")

CONFIG_HASH_SCHEMA_VERSION = 1
"""Bumping this invalidates every checkpoint and every job uniqueness key."""

ALLOWED_ASR_MODELS = frozenset(
    {"tiny", "base", "small", "medium", "large-v1", "large-v2", "large-v3"}
)

_LANGUAGE_PAIR_RE = re.compile(r"^[a-z]{2,3}:[a-z]{2,3}$")
_SLUG_RE = re.compile(r"[^a-z0-9]+")

_STRICT = ConfigDict(extra="forbid", frozen=True)


def root_id_for(path: Path) -> str:
    """Short, stable, human-recognisable identifier for a media root.

    The digest suffix keeps two roots whose last path component matches (for
    example ``/a/series`` and ``/b/series``) from colliding in staging.
    """
    slug = _SLUG_RE.sub("-", path.name.lower()).strip("-") or "root"
    return f"{slug}-{stable_digest(str(path))[:8]}"


class MediaRoot(BaseModel):
    """A configured library root together with its derived identifier."""

    model_config = _STRICT

    path: Path
    root_id: str

    def contains(self, candidate: Path) -> bool:
        return candidate == self.path or self.path in candidate.parents

    def relative_path_for(self, candidate: Path) -> str:
        return candidate.relative_to(self.path).as_posix()


class WorkerConfig(BaseModel):
    model_config = _STRICT

    concurrency: int = 1
    cpu_threads: int = Field(default=2, ge=1, le=64)
    retry_delays_seconds: tuple[int, ...] = (300, 1800, 7200)
    stale_lease_seconds: int = Field(default=180, ge=1)
    heartbeat_seconds: int = Field(default=30, ge=1)

    @field_validator("concurrency")
    @classmethod
    def _single_worker(cls, value: int) -> int:
        if value != 1:
            raise ValueError("this MVP processes one job at a time; worker.concurrency must be 1")
        return value

    @field_validator("retry_delays_seconds")
    @classmethod
    def _increasing_delays(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        if not value:
            raise ValueError("worker.retry_delays_seconds must list at least one delay")
        if any(delay < 0 for delay in value):
            raise ValueError("worker.retry_delays_seconds must not contain negative delays")
        if list(value) != sorted(value):
            raise ValueError("worker.retry_delays_seconds must be non-decreasing")
        return value

    @model_validator(mode="after")
    def _lease_outlives_heartbeat(self) -> WorkerConfig:
        if self.stale_lease_seconds <= self.heartbeat_seconds:
            raise ValueError(
                "worker.stale_lease_seconds must be greater than worker.heartbeat_seconds, "
                "otherwise a healthy worker loses its own lease"
            )
        return self

    @property
    def max_attempts(self) -> int:
        """One initial attempt plus one per configured retry delay."""
        return len(self.retry_delays_seconds) + 1


class AsrConfig(BaseModel):
    model_config = _STRICT

    model: str = "small"
    device: Literal["cpu"] = "cpu"
    compute_type: Literal["int8", "int8_float32", "float32"] = "int8"
    beam_size: int = Field(default=5, ge=1, le=20)
    word_timestamps: bool = True
    vad_filter: bool = True
    condition_on_previous_text: bool = False
    chunk_seconds: int = Field(default=300, ge=10, le=300)
    overlap_seconds: int = Field(default=2, ge=0, le=60)
    detection_min_probability: float = Field(default=0.80, ge=0.0, le=1.0)

    @field_validator("model")
    @classmethod
    def _multilingual_model(cls, value: str) -> str:
        if value.endswith(".en"):
            raise ValueError(
                f"asr.model {value!r} is English-only; a multilingual library needs a "
                "multilingual model"
            )
        if value not in ALLOWED_ASR_MODELS:
            allowed = ", ".join(sorted(ALLOWED_ASR_MODELS))
            raise ValueError(f"asr.model {value!r} is not one of: {allowed}")
        return value

    @model_validator(mode="after")
    def _overlap_fits_chunk(self) -> AsrConfig:
        if self.overlap_seconds * 2 >= self.chunk_seconds:
            raise ValueError(
                "asr.overlap_seconds must be smaller than half of asr.chunk_seconds, "
                "otherwise chunks would overlap their neighbours entirely"
            )
        return self


class TranslationConfig(BaseModel):
    model_config = _STRICT

    engine: Literal["argos"] = "argos"
    allowed_pairs: tuple[str, ...] = ("en:pt",)
    allow_pivot: bool = False

    @field_validator("allowed_pairs")
    @classmethod
    def _well_formed_pairs(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value:
            raise ValueError("translation.allowed_pairs must list at least one pair")
        for pair in value:
            if not _LANGUAGE_PAIR_RE.match(pair):
                raise ValueError(f"translation.allowed_pairs entry {pair!r} must look like 'en:pt'")
        if "en:pt" not in value:
            raise ValueError("translation.allowed_pairs must include 'en:pt' in this MVP")
        return value

    @field_validator("allow_pivot")
    @classmethod
    def _no_pivot(cls, value: bool) -> bool:
        if value:
            raise ValueError(
                "translation.allow_pivot must be false; routing through a third language "
                "is not supported"
            )
        return value

    def allows(self, *, source_language: str, target_language: str) -> bool:
        """``target_language`` is matched on its base tag, so ``pt-BR`` matches ``pt``."""
        base_target = target_language.split("-")[0].lower()
        return f"{source_language.lower()}:{base_target}" in self.allowed_pairs


class SubtitlesConfig(BaseModel):
    model_config = _STRICT

    max_lines: int = Field(default=2, ge=1, le=2)
    max_chars_per_line: int = Field(default=42, ge=10, le=120)
    target_max_chars_per_second: int = Field(default=20, ge=1, le=100)
    min_duration_seconds: float = Field(default=1.0, gt=0.0)
    max_duration_seconds: float = Field(default=7.0, gt=0.0)
    write_source_srt: bool = False

    @model_validator(mode="after")
    def _duration_window(self) -> SubtitlesConfig:
        if self.max_duration_seconds <= self.min_duration_seconds:
            raise ValueError(
                "subtitles.max_duration_seconds must be greater than subtitles.min_duration_seconds"
            )
        return self


class AppConfig(BaseModel):
    """The whole configuration file. Every internal path is absolute."""

    model_config = _STRICT

    media_roots: tuple[Path, ...]
    state_dir: Path
    work_dir: Path
    models_dir: Path
    output_dir: Path
    publish_mode: PublishMode = PublishMode.STAGING
    target_language: Literal["pt-BR"] = "pt-BR"
    scan_interval_seconds: int = Field(default=600, ge=1)
    stability_window_seconds: int = Field(default=600, ge=0)
    minimum_file_age_seconds: int = Field(default=600, ge=0)
    minimum_free_work_gib: int = Field(default=3, ge=1)
    worker: WorkerConfig = Field(default_factory=WorkerConfig)
    asr: AsrConfig = Field(default_factory=AsrConfig)
    translation: TranslationConfig = Field(default_factory=TranslationConfig)
    subtitles: SubtitlesConfig = Field(default_factory=SubtitlesConfig)

    # -- validation -------------------------------------------------------- #

    @field_validator("media_roots", "state_dir", "work_dir", "models_dir", "output_dir")
    @classmethod
    def _absolute_paths(cls, value: Path | tuple[Path, ...]) -> Path | tuple[Path, ...]:
        candidates = value if isinstance(value, tuple) else (value,)
        for candidate in candidates:
            if not candidate.is_absolute():
                raise ValueError(f"{str(candidate)!r} must be an absolute path")
            if ".." in candidate.parts:
                raise ValueError(f"{str(candidate)!r} must not contain '..'")
        return value

    @field_validator("media_roots")
    @classmethod
    def _distinct_roots(cls, value: tuple[Path, ...]) -> tuple[Path, ...]:
        if not value:
            raise ValueError("media_roots must list at least one directory")
        if len(set(value)) != len(value):
            raise ValueError("media_roots must not repeat the same directory")
        for root in value:
            for other in value:
                if root is not other and other in root.parents:
                    raise ValueError(
                        f"media root {str(root)!r} is nested inside {str(other)!r}; "
                        "the same file would be discovered twice"
                    )
        return value

    @model_validator(mode="after")
    def _working_dirs_outside_media(self) -> AppConfig:
        named = {
            "state_dir": self.state_dir,
            "work_dir": self.work_dir,
            "models_dir": self.models_dir,
            "output_dir": self.output_dir,
        }
        seen: dict[Path, str] = {}
        for name, path in named.items():
            if path in seen:
                raise ValueError(f"{name} and {seen[path]} must not be the same directory")
            seen[path] = name
            for root in self.media_roots:
                if path == root or root in path.parents:
                    raise ValueError(
                        f"{name} {str(path)!r} is inside media root {str(root)!r}; "
                        "the application must never write into the library"
                    )
        return self

    # -- derived values ---------------------------------------------------- #

    @property
    def roots(self) -> tuple[MediaRoot, ...]:
        return tuple(MediaRoot(path=path, root_id=root_id_for(path)) for path in self.media_roots)

    @property
    def database_path(self) -> Path:
        return self.state_dir / "jobs.sqlite3"

    @property
    def lock_path(self) -> Path:
        """``flock`` target enforcing a single worker per ``state_dir``."""
        return self.state_dir / "worker.lock"

    @property
    def manifests_dir(self) -> Path:
        """Manifests live here, never beside the video."""
        return self.state_dir / "manifests"

    @property
    def staging_dir(self) -> Path:
        return self.output_dir

    @property
    def asr_models_dir(self) -> Path:
        return self.models_dir / "whisper"

    @property
    def translation_models_dir(self) -> Path:
        return self.models_dir / "argos"

    def root_for(self, path: Path) -> MediaRoot | None:
        """Return the configured root containing ``path``, longest match first."""
        matches = [root for root in self.roots if root.contains(path)]
        if not matches:
            return None
        return max(matches, key=lambda root: len(root.path.parts))

    def root_by_id(self, root_id: str) -> MediaRoot | None:
        return next((root for root in self.roots if root.root_id == root_id), None)

    # -- hashes ------------------------------------------------------------ #

    @property
    def pipeline_config_hash(self) -> str:
        """Hash of everything that changes the content the pipeline produces."""
        return stable_digest(
            {
                "schema": CONFIG_HASH_SCHEMA_VERSION,
                "target_language": self.target_language,
                "translation_backend": self._translation_backend_identity(),
                "asr": self.asr.model_dump(mode="json"),
                "translation": self.translation.model_dump(mode="json"),
                "subtitles": self.subtitles.model_dump(mode="json"),
                "word_dedupe": TRANSCRIBE_WORD_DEDUPE_VERSION,
            }
        )

    def stage_config_hash(self, stage: PipelineStage) -> str:
        """Hash of only the settings that stage depends on."""
        return stable_digest(
            {
                "schema": CONFIG_HASH_SCHEMA_VERSION,
                "stage": stage,
                "settings": self._stage_settings(stage),
            }
        )

    def _stage_settings(self, stage: PipelineStage) -> dict[str, Any]:
        asr = self.asr.model_dump(mode="json")
        chunking = {
            "chunk_seconds": self.asr.chunk_seconds,
            "overlap_seconds": self.asr.overlap_seconds,
            "sample_rate": AUDIO_SAMPLE_RATE_HZ,
            "channels": AUDIO_CHANNELS,
            "sample_format": AUDIO_SAMPLE_FORMAT,
        }
        subtitles = self.subtitles.model_dump(mode="json")
        match stage:
            case PipelineStage.PROBE:
                return {}
            case PipelineStage.DETECT_LANGUAGE:
                return {
                    "model": asr["model"],
                    "device": asr["device"],
                    "compute_type": asr["compute_type"],
                    "detection_min_probability": asr["detection_min_probability"],
                }
            case PipelineStage.EXTRACT:
                return chunking
            case PipelineStage.TRANSCRIBE:
                return {
                    "asr": asr,
                    "chunking": chunking,
                    "word_dedupe": TRANSCRIBE_WORD_DEDUPE_VERSION,
                }
            case PipelineStage.MERGE:
                return chunking
            case PipelineStage.TRANSLATE:
                return {
                    "translation": self.translation.model_dump(mode="json"),
                    "target_language": self.target_language,
                    "translation_backend": self._translation_backend_identity(),
                    "normalizer": TRANSLATION_NORMALIZER_VERSION,
                }
            case PipelineStage.RENDER:
                return {"subtitles": subtitles, "target_language": self.target_language}
            case PipelineStage.VALIDATE:
                return {"subtitles": subtitles}
            case PipelineStage.PUBLISH:
                return {
                    "publish_mode": str(self.publish_mode),
                    "target_language": self.target_language,
                }
            case _:
                assert_never(stage)

    def _translation_backend_identity(self) -> dict[str, str]:
        """Argos from/to codes, so ``en→pb`` is not hashed as ``en→pt``.

        Public ``target_language`` stays ``pt-BR``. The lazy import avoids a
        module cycle with ``models``.
        """
        from .models import to_argos_language_code

        return {
            "engine": self.translation.engine,
            "argos_from": to_argos_language_code("en"),
            "argos_to": to_argos_language_code(self.target_language),
        }


def load_config(path: Path) -> AppConfig:
    """Read and validate a configuration file.

    Raises :class:`ConfigurationError` (exit code 2) with a readable message
    listing every offending field instead of a raw traceback.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ConfigurationError(f"configuration file not found: {path}") from exc
    except OSError as exc:
        raise ConfigurationError(f"cannot read configuration file {path}: {exc}") from exc

    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigurationError(f"{path} is not valid YAML: {exc}") from exc

    if raw is None:
        raise ConfigurationError(f"{path} is empty")
    if not isinstance(raw, dict):
        raise ConfigurationError(f"{path} must contain a YAML mapping at the top level")

    try:
        return AppConfig.model_validate(raw)
    except ValidationError as exc:
        raise ConfigurationError(_format_validation_error(path, exc)) from exc


def _format_validation_error(path: Path, error: ValidationError) -> str:
    lines = [f"{path} is invalid:"]
    for issue in error.errors():
        location = ".".join(str(part) for part in issue["loc"]) or "<root>"
        lines.append(f"  - {location}: {issue['msg']}")
    return "\n".join(lines)
