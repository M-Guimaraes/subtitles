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
    LANGUAGE_DECISION_POLICY_VERSION,
    TRANSCRIBE_WORD_DEDUPE_VERSION,
    TRANSLATION_NORMALIZER_VERSION,
    ConfigurationError,
    DubbingProfile,
    ExistingSubtitlePolicy,
    JobKind,
    PipelineStage,
    PublishMode,
    stable_digest,
)

__all__ = [
    "ALLOWED_ASR_MODELS",
    "CONFIG_HASH_SCHEMA_VERSION",
    "DEFAULT_CONFIG_PATH",
    "SOURCE_LANGUAGE_AUTO",
    "AppConfig",
    "AsrConfig",
    "AudioConfig",
    "DashboardConfig",
    "DubbingConfig",
    "LanguagesConfig",
    "MediaRoot",
    "SubtitlesConfig",
    "TranslationConfig",
    "WebhookPathMap",
    "WebhooksConfig",
    "WorkerConfig",
    "canonicalize_public_language_tag",
    "load_config",
    "root_id_for",
]

DEFAULT_CONFIG_PATH = Path("/config/config.yaml")

CONFIG_HASH_SCHEMA_VERSION = 1
"""Bumping this invalidates every checkpoint and every job uniqueness key."""

ALLOWED_ASR_MODELS = frozenset(
    {"tiny", "base", "small", "medium", "large-v1", "large-v2", "large-v3"}
)

SOURCE_LANGUAGE_AUTO = "auto"
"""Sentinel for automatic source-language detection."""

_LANGUAGE_PAIR_RE = re.compile(r"^[a-z]{2,3}:[a-z]{2,3}$")
_PUBLIC_LANGUAGE_TAG_RE = re.compile(r"^[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})*$")
_BACKEND_ONLY_PUBLIC_REJECT = frozenset({"pb"})
"""Argos-only codes; configuration and filenames use ``pt-BR``, never ``pb``."""
_CANONICAL_PUBLIC_TAGS = {
    "pt-br": "pt-BR",
    "en": "en",
    "es": "es",
    "ja": "ja",
}
_SLUG_RE = re.compile(r"[^a-z0-9]+")

_STRICT = ConfigDict(extra="forbid", frozen=True)


def canonicalize_public_language_tag(value: str, *, field_name: str) -> str:
    """Fold a public language tag. Argos ``pb`` is rejected, never aliased to ``pt``."""
    stripped = value.strip().replace("_", "-")
    if not stripped:
        raise ValueError(f"{field_name} entries must not be empty")
    lowered = stripped.lower()
    primary = lowered.split("-", 1)[0]
    if lowered in _BACKEND_ONLY_PUBLIC_REJECT or primary in _BACKEND_ONLY_PUBLIC_REJECT:
        raise ValueError(
            f"{field_name} must use a public identifier such as pt-BR; "
            "Argos backend code 'pb' is not accepted"
        )
    if not _PUBLIC_LANGUAGE_TAG_RE.match(stripped):
        raise ValueError(f"{field_name} {value!r} is not a public language tag")
    if lowered in _CANONICAL_PUBLIC_TAGS:
        return _CANONICAL_PUBLIC_TAGS[lowered]
    parts = stripped.split("-")
    primary_folded = parts[0].lower()
    if len(parts) == 1:
        return primary_folded
    return primary_folded + "-" + "-".join(parts[1:])


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
        """Pairs use public families: ``pt-BR`` matches ``pt``, never Argos ``pb``."""
        base_source = source_language.split("-")[0].lower()
        base_target = target_language.split("-")[0].lower()
        return f"{base_source}:{base_target}" in self.allowed_pairs


class LanguagesConfig(BaseModel):
    """Public language identifiers. Backend codes such as Argos ``pb`` stay internal.

    ``targets`` is the schema. A legacy single ``target`` is folded into
    ``targets: [target]``; if both keys appear they must represent the same
    one-item list.
    """

    model_config = _STRICT

    source: str = SOURCE_LANGUAGE_AUTO
    targets: tuple[str, ...] = ("pt-BR",)
    low_confidence: Literal["review"] = "review"

    @model_validator(mode="before")
    @classmethod
    def _fold_legacy_target(cls, value: Any) -> Any:
        """Accept the historical nested ``languages.target`` key."""
        if not isinstance(value, dict):
            return value
        payload = dict(value)
        single = payload.pop("target", None)
        targets = payload.get("targets")
        if single is None:
            return payload
        if targets is None:
            payload["targets"] = [single]
            return payload
        if not isinstance(targets, list | tuple):
            return payload
        if list(targets) != [single]:
            raise ValueError("languages.target and languages.targets must agree")
        return payload

    @field_validator("source")
    @classmethod
    def _source_auto_or_public_tag(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("languages.source must be 'auto' or a public language tag")
        if stripped.lower() == SOURCE_LANGUAGE_AUTO:
            return SOURCE_LANGUAGE_AUTO
        return canonicalize_public_language_tag(stripped, field_name="languages.source")

    @field_validator("targets")
    @classmethod
    def _targets_are_public(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value:
            raise ValueError("languages.targets must list at least one public language")
        normalised: list[str] = []
        seen: set[str] = set()
        for raw in value:
            tag = canonicalize_public_language_tag(raw, field_name="languages.targets")
            if tag in seen:
                raise ValueError(f"languages.targets repeats {tag!r}")
            seen.add(tag)
            normalised.append(tag)
        return tuple(normalised)

    @property
    def target(self) -> str:
        """Primary public destination tag. Never ``pb``."""
        return self.targets[0]


class AudioConfig(BaseModel):
    """Audio-stream selection. ``stream`` is a global ffprobe index, never ``a:N``."""

    model_config = _STRICT

    stream: Literal["auto"] | int = "auto"
    preferred_languages: tuple[str, ...] = ("en", "ja")

    @field_validator("stream")
    @classmethod
    def _stream_auto_or_global_index(cls, value: Literal["auto"] | int) -> Literal["auto"] | int:
        if value == "auto":
            return value
        if value < 0:
            raise ValueError("audio.stream must be 'auto' or a non-negative global ffprobe index")
        return value

    @field_validator("preferred_languages")
    @classmethod
    def _public_preferred_languages(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value:
            raise ValueError("audio.preferred_languages must list at least one public language")
        normalised: list[str] = []
        for raw in value:
            tag = raw.strip().replace("_", "-")
            if not tag:
                raise ValueError("audio.preferred_languages entries must not be empty")
            lowered = tag.lower()
            if (
                lowered in _BACKEND_ONLY_PUBLIC_REJECT
                or lowered.split("-", 1)[0] in _BACKEND_ONLY_PUBLIC_REJECT
            ):
                raise ValueError(
                    "audio.preferred_languages must use public identifiers; "
                    "Argos backend code 'pb' is not accepted"
                )
            normalised.append(tag)
        return tuple(normalised)


class DashboardConfig(BaseModel):
    """Listen address for the optional operator dashboard.

    Defaults bind loopback only. The dashboard is not part of the pipeline
    hash: changing bind or port must not invalidate jobs or checkpoints.
    ``token`` is a shared secret for LAN deployments; it is never logged.
    """

    model_config = _STRICT

    bind: str = "127.0.0.1"
    port: int = Field(default=8787, ge=1, le=65535)
    token: str | None = None

    @field_validator("bind")
    @classmethod
    def _bind_not_empty(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("dashboard.bind must be a host address")
        return stripped

    @field_validator("token")
    @classmethod
    def _token_optional(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None


class WebhookPathMap(BaseModel):
    """Rewrite a Sonarr/Radarr host path onto a configured container root.

    Payloads often carry the *host* library path. Mapping is a prefix
    replacement only; the result must still fall inside ``media_roots``.
    """

    model_config = _STRICT

    host_prefix: Path
    container_prefix: Path

    @field_validator("host_prefix", "container_prefix")
    @classmethod
    def _absolute_prefix(cls, value: Path) -> Path:
        if not value.is_absolute():
            raise ValueError("webhooks.path_maps prefixes must be absolute paths")
        if ".." in value.parts:
            raise ValueError("webhooks.path_maps prefixes must not contain '..'")
        return value


class WebhooksConfig(BaseModel):
    """Listen address for the optional Sonarr/Radarr webhook listener.

    Defaults bind loopback only. Bind, port, token and path maps are not
    part of the pipeline hash: changing them must not invalidate jobs.
    A token is required at runtime; unauthenticated webhooks are refused.
    """

    model_config = _STRICT

    bind: str = "127.0.0.1"
    port: int = Field(default=8788, ge=1, le=65535)
    token: str | None = None
    path_maps: tuple[WebhookPathMap, ...] = ()

    @field_validator("bind")
    @classmethod
    def _bind_not_empty(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("webhooks.bind must be a host address")
        return stripped

    @field_validator("token")
    @classmethod
    def _token_optional_in_file(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None


class DubbingConfig(BaseModel):
    """Local pt-BR dubbing. Changing these settings does not invalidate subtitle jobs."""

    model_config = _STRICT

    enabled: bool = False
    """Dubbing is paused unless this is true. Not part of any job hash."""
    profile: DubbingProfile = DubbingProfile.CPU_FIXED
    target_language: str = "pt-BR"
    voice: str = "pt_BR-faber-medium"
    min_speed: float = Field(default=0.90, gt=0.0, le=1.0)
    max_speed: float = Field(default=1.15, gt=0.0, le=4.0)

    @field_validator("target_language")
    @classmethod
    def _public_target(cls, value: str) -> str:
        return canonicalize_public_language_tag(value, field_name="dubbing.target_language")

    def hash_payload(self) -> dict[str, object]:
        """Settings that change dubbing output; ``enabled`` only gates running."""
        return self.model_dump(mode="json", exclude={"enabled"})

    @field_validator("voice")
    @classmethod
    def _voice_not_empty(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("dubbing.voice must name a local voice id")
        return stripped

    @model_validator(mode="after")
    def _speed_window(self) -> DubbingConfig:
        if self.max_speed < self.min_speed:
            raise ValueError("dubbing.max_speed must be greater than or equal to dubbing.min_speed")
        return self


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
    existing_subtitle_policy: ExistingSubtitlePolicy = ExistingSubtitlePolicy.SKIP
    languages: LanguagesConfig = Field(default_factory=LanguagesConfig)
    audio: AudioConfig = Field(default_factory=AudioConfig)
    scan_interval_seconds: int = Field(default=600, ge=1)
    stability_window_seconds: int = Field(default=600, ge=0)
    minimum_file_age_seconds: int = Field(default=600, ge=0)
    minimum_free_work_gib: int = Field(default=3, ge=1)
    worker: WorkerConfig = Field(default_factory=WorkerConfig)
    asr: AsrConfig = Field(default_factory=AsrConfig)
    translation: TranslationConfig = Field(default_factory=TranslationConfig)
    subtitles: SubtitlesConfig = Field(default_factory=SubtitlesConfig)
    dashboard: DashboardConfig = Field(default_factory=DashboardConfig)
    webhooks: WebhooksConfig = Field(default_factory=WebhooksConfig)
    dubbing: DubbingConfig = Field(default_factory=DubbingConfig)

    # -- validation -------------------------------------------------------- #

    @model_validator(mode="before")
    @classmethod
    def _fold_legacy_target_language(cls, value: Any) -> Any:
        """Accept the historical top-level ``target_language`` key.

        Nested ``languages.target`` is the schema. An old file that still has
        ``target_language: pt-BR`` at the root is folded in; both keys must
        agree when they appear together.
        """
        if not isinstance(value, dict):
            return value
        payload = dict(value)
        top = payload.pop("target_language", None)
        if top is None:
            return payload
        languages = payload.get("languages")
        if languages is None:
            payload["languages"] = {"target": top}
            return payload
        if not isinstance(languages, dict):
            return payload
        nested = languages.get("target")
        if nested is None:
            payload["languages"] = {**languages, "target": top}
            return payload
        if nested != top:
            raise ValueError("languages.target and legacy target_language must match")
        return payload

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
    def target_language(self) -> str:
        """Primary public destination tag used in filenames and manifests, never ``pb``."""
        return self.languages.target

    @property
    def target_languages(self) -> tuple[str, ...]:
        """Configured public destination tags, in order. Never includes ``pb``."""
        return self.languages.targets

    def pipeline_config_hash_for(
        self, target_language: str, *, job_kind: JobKind | None = None
    ) -> str:
        """Hash of settings that change what this *target* would produce.

        Each target is its own job identity. The payload shape for a single
        ``pt-BR`` subtitle target stays the historical ``languages.target``
        object so existing single-target jobs are not invalidated. Dubbing
        hashes include ``job_kind`` and :attr:`dubbing` and never collide with
        a subtitle job even when the unique index is absent.
        """
        kind = JobKind.SUBTITLES if job_kind is None else job_kind
        if kind is JobKind.DUBBING:
            return stable_digest(
                {
                    **self._pipeline_hash_payload(target_language),
                    "job_kind": str(kind),
                    "dubbing": self.dubbing.hash_payload(),
                }
            )
        return stable_digest(self._pipeline_hash_payload(target_language))

    def target_language_for_job(self, target_language: str | None) -> str:
        """Resolve a job's public target, falling back to the primary."""
        return target_language or self.target_language

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

    @property
    def tts_models_dir(self) -> Path:
        return self.models_dir / "piper"

    @property
    def separation_models_dir(self) -> Path:
        return self.models_dir / "separator"

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
        """Hash of everything that changes the content the pipeline produces.

        Uses the primary target so a single-target ``pt-BR`` deployment keeps
        the same identity. Multi-target enqueue calls
        :meth:`pipeline_config_hash_for` per destination.
        """
        return self.pipeline_config_hash_for(self.target_language)

    def stage_config_hash(self, stage: PipelineStage, *, target_language: str | None = None) -> str:
        """Hash of only the settings that stage depends on."""
        return stable_digest(
            {
                "schema": CONFIG_HASH_SCHEMA_VERSION,
                "stage": stage,
                "settings": self._stage_settings(
                    stage, target_language=target_language or self.target_language
                ),
            }
        )

    def _pipeline_hash_payload(self, target_language: str) -> dict[str, Any]:
        return {
            "schema": CONFIG_HASH_SCHEMA_VERSION,
            "target_language": target_language,
            "languages": self._languages_identity(target_language),
            "audio": self.audio.model_dump(mode="json"),
            "language_policy": LANGUAGE_DECISION_POLICY_VERSION,
            "translation_backend": self._translation_backend_identity(target_language),
            "asr": self.asr.model_dump(mode="json"),
            "translation": self.translation.model_dump(mode="json"),
            "subtitles": self.subtitles.model_dump(mode="json"),
            "word_dedupe": TRANSCRIBE_WORD_DEDUPE_VERSION,
        }

    def _languages_identity(self, target_language: str) -> dict[str, Any]:
        """Historical ``languages`` hash object: source, one target, policy."""
        return {
            "source": self.languages.source,
            "target": target_language,
            "low_confidence": self.languages.low_confidence,
        }

    def _stage_settings(self, stage: PipelineStage, *, target_language: str) -> dict[str, Any]:
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
                return {"audio": self.audio.model_dump(mode="json")}
            case PipelineStage.DETECT_LANGUAGE:
                return {
                    "model": asr["model"],
                    "device": asr["device"],
                    "compute_type": asr["compute_type"],
                    "detection_min_probability": asr["detection_min_probability"],
                    "languages": self._languages_identity(target_language),
                    "audio": self.audio.model_dump(mode="json"),
                    "language_policy": LANGUAGE_DECISION_POLICY_VERSION,
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
                    "target_language": target_language,
                    "languages": self._languages_identity(target_language),
                    "language_policy": LANGUAGE_DECISION_POLICY_VERSION,
                    "translation_backend": self._translation_backend_identity(target_language),
                    "normalizer": TRANSLATION_NORMALIZER_VERSION,
                }
            case PipelineStage.RENDER:
                return {"subtitles": subtitles, "target_language": target_language}
            case PipelineStage.VALIDATE:
                return {"subtitles": subtitles}
            case PipelineStage.PUBLISH:
                return {
                    "publish_mode": str(self.publish_mode),
                    "existing_subtitle_policy": str(self.existing_subtitle_policy),
                    "target_language": target_language,
                }
            case PipelineStage.SEPARATE:
                return {"dubbing": self.dubbing.hash_payload(), "stage": "separate"}
            case PipelineStage.ADAPT:
                return {
                    "dubbing": self.dubbing.hash_payload(),
                    "target_language": target_language,
                    "normalizer": TRANSLATION_NORMALIZER_VERSION,
                }
            case PipelineStage.SYNTHESIZE:
                return {
                    "dubbing": self.dubbing.hash_payload(),
                    "voice": self.dubbing.voice,
                    "profile": str(self.dubbing.profile),
                }
            case PipelineStage.SYNC:
                return {
                    "min_speed": self.dubbing.min_speed,
                    "max_speed": self.dubbing.max_speed,
                }
            case PipelineStage.MIX:
                return {"dubbing": self.dubbing.hash_payload(), "stage": "mix"}
            case PipelineStage.VALIDATE_AUDIO:
                return {"dubbing": self.dubbing.hash_payload(), "stage": "validate_audio"}
            case _:
                assert_never(stage)

    def _translation_backend_identity(self, target_language: str) -> dict[str, str]:
        """Argos from/to codes, so ``en→pb`` is not hashed as ``en→pt``.

        Public ``target_language`` stays ``pt-BR``. The lazy import avoids a
        module cycle with ``models``.
        """
        from .models import to_argos_language_code

        return {
            "engine": self.translation.engine,
            "argos_from": to_argos_language_code("en"),
            "argos_to": to_argos_language_code(target_language),
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
