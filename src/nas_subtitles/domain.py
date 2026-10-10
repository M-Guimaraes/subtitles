"""Shared domain contract for every pipeline stage.

This module is the single source of truth for the data that crosses stage
boundaries: enums, value objects and the ``Protocol`` definitions for the
engines. It must not import any other project module, so that every stage can
depend on it without creating cycles.

Two invariants are load bearing and are repeated on the relevant types:

* every timestamp expressed in seconds is absolute on the *video* timeline,
  never relative to an extracted chunk or to the audio stream ``start_time``;
* a chunk only owns the words whose midpoint falls inside its central
  interval, so the overlap used for context never duplicates text.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import IntEnum, StrEnum
from pathlib import Path
from typing import Protocol, runtime_checkable

__all__ = [
    "AUDIO_CHANNELS",
    "AUDIO_SAMPLE_FORMAT",
    "AUDIO_SAMPLE_RATE_HZ",
    "CHECKPOINT_SCHEMA_VERSION",
    "DB_SCHEMA_VERSION",
    "DUBBING_PLAN_SCHEMA_VERSION",
    "DUBBING_STAGE_ORDER",
    "ENGLISH",
    "EXIT_CODE_BY_ERROR",
    "FINGERPRINT_SAMPLE_BYTES",
    "HEARTBEAT_EVENT_CODE",
    "LANGUAGE_DECISION_POLICY_VERSION",
    "LIBRARY_SCAN_KNOWN_STATES",
    "MANIFEST_SCHEMA_VERSION",
    "PIPELINE_STAGE_ORDER",
    "PORTUGUESE",
    "PORTUGUESE_SUBTITLE_SUFFIXES",
    "RETRYABLE_ERROR_CODES",
    "STRUCTURAL_FLAG_CODES",
    "SUPPORTED_SOURCE_LANGUAGES",
    "TERMINAL_JOB_STATES",
    "TRANSCRIBE_WORD_DEDUPE_VERSION",
    "TRANSLATION_NORMALIZER_VERSION",
    "VIDEO_EXTENSIONS",
    "ArtifactRecord",
    "AudioChunk",
    "AudioChunkSpec",
    "AudioExtractor",
    "AudioMixer",
    "AudioStreamInfo",
    "ChunkTranscript",
    "ConfigurationError",
    "DialogueSeparator",
    "DubSegment",
    "DubSegmentReviewState",
    "DubbingProfile",
    "DubbingQualityReport",
    "ErrorCode",
    "EventLevel",
    "ExistingSubtitle",
    "ExistingSubtitlePolicy",
    "ExitCode",
    "JobClaim",
    "JobEvent",
    "JobExecutionScope",
    "JobKind",
    "JobManifest",
    "JobMetrics",
    "JobRecord",
    "JobRepository",
    "JobState",
    "LanguageDecision",
    "LanguageSample",
    "LanguageSource",
    "LockBusyError",
    "MediaFingerprint",
    "MediaProbe",
    "ModelIdentity",
    "ModelKind",
    "NasSubtitlesError",
    "PipelineStage",
    "ProbeResult",
    "PublishMode",
    "PublishOutcome",
    "PublishResult",
    "QualityFlag",
    "QualityFlagCode",
    "QualityReport",
    "QualitySeverity",
    "ScanObservation",
    "Seconds",
    "SeparatedAudio",
    "SpeechSynthesizer",
    "SubtitleCue",
    "SubtitleOrigin",
    "SubtitleRenderer",
    "SubtitleStreamInfo",
    "SynthesisArtifact",
    "TimelineRenderer",
    "Transcriber",
    "Transcript",
    "TranscriptSegment",
    "TranslatedUnit",
    "TranslationCacheEntry",
    "TranslationUnit",
    "Translator",
    "VoiceAssignment",
    "VoiceKind",
    "Word",
    "canonical_json",
    "exit_code_for",
    "infer_execution_scope",
    "infer_job_kind",
    "stable_digest",
    "stable_unit_id",
    "stage_window",
    "stages_for",
]

# --------------------------------------------------------------------------- #
# Primitive aliases and constants
# --------------------------------------------------------------------------- #

Seconds = float
"""Absolute position on the video timeline, in seconds."""

JobId = str
UnitId = str

AUDIO_SAMPLE_RATE_HZ = 16_000
AUDIO_CHANNELS = 1
AUDIO_SAMPLE_FORMAT = "pcm_s16le"

FINGERPRINT_SAMPLE_BYTES = 1 << 20
"""1 MiB is hashed from the head and 1 MiB from the tail of a media file."""

CHECKPOINT_SCHEMA_VERSION = 1
MANIFEST_SCHEMA_VERSION = 2
DB_SCHEMA_VERSION = 4
DUBBING_PLAN_SCHEMA_VERSION = 1
"""Bumping this invalidates exported dubbing plans."""
TRANSLATION_NORMALIZER_VERSION = 1
"""Bumping this invalidates every cached translation."""

LANGUAGE_DECISION_POLICY_VERSION = 1
"""Bumping this invalidates jobs when the auto language policy changes."""

TRANSCRIBE_WORD_DEDUPE_VERSION = 2
"""Bumping this invalidates transcription checkpoints after merge/dedupe changes."""

HEARTBEAT_EVENT_CODE = "worker_heartbeat"
"""Event code the worker writes periodically and ``health`` reads back."""

ENGLISH = "en"
PORTUGUESE = "pt"
SUPPORTED_SOURCE_LANGUAGES = frozenset({ENGLISH, PORTUGUESE})
"""Any other detected source language becomes ``unsupported_language``."""

VIDEO_EXTENSIONS = frozenset({".mkv", ".mp4", ".m4v", ".avi", ".mov"})
"""Compared case-insensitively."""

PORTUGUESE_SUBTITLE_SUFFIXES = frozenset({"pt", "por", "pt-br", "pt_br", "pob"})
"""Compared case-insensitively against the suffix of an external subtitle."""


# --------------------------------------------------------------------------- #
# Serialisation helpers (shared by fingerprints, config hashes and checkpoints)
# --------------------------------------------------------------------------- #


def canonical_json(payload: object) -> str:
    """Serialise ``payload`` so that equal values always produce equal bytes."""
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=_json_default,
    )


def _json_default(value: object) -> object:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, StrEnum):
        return str(value)
    if isinstance(value, frozenset | set):
        return sorted(str(item) for item in value)
    raise TypeError(f"cannot serialise {type(value).__name__} canonically")


def stable_digest(payload: object) -> str:
    """SHA-256 of the canonical JSON form of ``payload``."""
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def stable_unit_id(
    *,
    fingerprint_digest: str,
    index: int,
    start_seconds: Seconds,
    end_seconds: Seconds,
    normalized_text: str,
) -> UnitId:
    """Identifier that survives a restart and keeps the translation cache usable.

    It deliberately includes the media fingerprint, so re-encoding the video
    produces new units instead of silently reusing translations of other audio.
    """
    return stable_digest(
        {
            "fingerprint": fingerprint_digest,
            "index": index,
            "start": round(start_seconds, 3),
            "end": round(end_seconds, 3),
            "text": normalized_text,
            "normalizer": TRANSLATION_NORMALIZER_VERSION,
        }
    )[:32]


# --------------------------------------------------------------------------- #
# Enums
# --------------------------------------------------------------------------- #


class ExitCode(IntEnum):
    """Process exit codes. Every command must use these and nothing else."""

    SUCCESS = 0
    INVALID_INPUT = 2
    PREFLIGHT_FAILED = 3
    PROCESSING_FAILED = 4
    REVIEW_REQUIRED = 5
    LOCK_BUSY = 6


class ErrorCode(StrEnum):
    """Stable machine readable failure reasons, also stored in ``jobs``."""

    CONFIG_INVALID = "config_invalid"
    INVALID_MEDIA = "invalid_media"
    UNSUPPORTED_LANGUAGE = "unsupported_language"
    OUTPUT_CONFLICT = "output_conflict"
    UNSUPPORTED_ATOMIC_PUBLISH = "unsupported_atomic_publish"
    MEDIA_ROOT_MISSING = "media_root_missing"
    MEDIA_PATH_OUTSIDE_ROOTS = "media_path_outside_roots"
    MEDIA_UNSTABLE = "media_unstable"
    MEDIA_CHANGED = "media_changed"
    PERMISSION_DENIED = "permission_denied"
    INSUFFICIENT_SPACE = "insufficient_space"
    MODEL_MISSING = "model_missing"
    TRANSLATION_PAIR_MISSING = "translation_pair_missing"
    EMPTY_TRANSLATION = "empty_translation"
    NO_CUES_FOR_SPEECH = "no_cues_for_speech"
    LANGUAGE_UNDETERMINED = "language_undetermined"
    CHECKPOINT_INVALID = "checkpoint_invalid"
    QUALITY_GATE_FAILED = "quality_gate_failed"
    IO_ERROR = "io_error"
    SUBPROCESS_FAILED = "subprocess_failed"
    SUBPROCESS_TIMEOUT = "subprocess_timeout"
    INTERRUPTED = "interrupted"
    LOCK_BUSY = "lock_busy"
    JOB_NOT_FOUND = "job_not_found"
    INVALID_STATE_TRANSITION = "invalid_state_transition"
    NOT_IMPLEMENTED = "not_implemented"


EXIT_CODE_BY_ERROR: Mapping[ErrorCode, ExitCode] = {
    ErrorCode.CONFIG_INVALID: ExitCode.INVALID_INPUT,
    ErrorCode.MEDIA_PATH_OUTSIDE_ROOTS: ExitCode.INVALID_INPUT,
    ErrorCode.JOB_NOT_FOUND: ExitCode.INVALID_INPUT,
    ErrorCode.INVALID_STATE_TRANSITION: ExitCode.INVALID_INPUT,
    ErrorCode.MEDIA_ROOT_MISSING: ExitCode.PREFLIGHT_FAILED,
    ErrorCode.MODEL_MISSING: ExitCode.PREFLIGHT_FAILED,
    ErrorCode.TRANSLATION_PAIR_MISSING: ExitCode.PREFLIGHT_FAILED,
    ErrorCode.PERMISSION_DENIED: ExitCode.PREFLIGHT_FAILED,
    ErrorCode.INSUFFICIENT_SPACE: ExitCode.PREFLIGHT_FAILED,
    ErrorCode.INVALID_MEDIA: ExitCode.PROCESSING_FAILED,
    ErrorCode.UNSUPPORTED_LANGUAGE: ExitCode.PROCESSING_FAILED,
    ErrorCode.UNSUPPORTED_ATOMIC_PUBLISH: ExitCode.PROCESSING_FAILED,
    ErrorCode.MEDIA_UNSTABLE: ExitCode.PROCESSING_FAILED,
    ErrorCode.MEDIA_CHANGED: ExitCode.PROCESSING_FAILED,
    ErrorCode.EMPTY_TRANSLATION: ExitCode.PROCESSING_FAILED,
    ErrorCode.NO_CUES_FOR_SPEECH: ExitCode.PROCESSING_FAILED,
    ErrorCode.CHECKPOINT_INVALID: ExitCode.PROCESSING_FAILED,
    ErrorCode.IO_ERROR: ExitCode.PROCESSING_FAILED,
    ErrorCode.SUBPROCESS_FAILED: ExitCode.PROCESSING_FAILED,
    ErrorCode.SUBPROCESS_TIMEOUT: ExitCode.PROCESSING_FAILED,
    ErrorCode.INTERRUPTED: ExitCode.PROCESSING_FAILED,
    ErrorCode.NOT_IMPLEMENTED: ExitCode.PROCESSING_FAILED,
    ErrorCode.OUTPUT_CONFLICT: ExitCode.REVIEW_REQUIRED,
    ErrorCode.LANGUAGE_UNDETERMINED: ExitCode.REVIEW_REQUIRED,
    ErrorCode.QUALITY_GATE_FAILED: ExitCode.REVIEW_REQUIRED,
    ErrorCode.LOCK_BUSY: ExitCode.LOCK_BUSY,
}


def exit_code_for(code: ErrorCode) -> ExitCode:
    """Map a failure reason onto the documented process exit code."""
    return EXIT_CODE_BY_ERROR.get(code, ExitCode.PROCESSING_FAILED)


RETRYABLE_ERROR_CODES: frozenset[ErrorCode] = frozenset(
    {
        ErrorCode.IO_ERROR,
        ErrorCode.SUBPROCESS_FAILED,
        ErrorCode.SUBPROCESS_TIMEOUT,
        ErrorCode.INSUFFICIENT_SPACE,
    }
)
"""Transient I/O and subprocess faults. Permission, missing model or language,
invalid media and output conflicts must never be retried."""


class JobState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    RETRY_WAIT = "retry_wait"
    NEEDS_REVIEW = "needs_review"
    READY_TO_PUBLISH = "ready_to_publish"
    COMPLETED = "completed"
    SKIPPED = "skipped"
    FAILED = "failed"
    CANCELLED = "cancelled"


class JobKind(StrEnum):
    """Persistent job identity. Subtitle and dubbing work never share a row."""

    SUBTITLES = "subtitles"
    DUBBING = "dubbing"


class DubbingProfile(StrEnum):
    """Named synthesis stacks. ``cpu-fixed`` is the MVP; ``mac-clone`` is experimental."""

    CPU_FIXED = "cpu-fixed"
    MAC_CLONE = "mac-clone"


class VoiceKind(StrEnum):
    FIXED = "fixed"
    CLONE = "clone"


class DubSegmentReviewState(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class JobExecutionScope(StrEnum):
    """Persistent execution identity: preview never satisfies the library.

    ``full`` is automatic or unattended processing intended to produce
    ``<stem>.pt-BR.srt``. ``preview`` is a manual ``--preview-seconds`` run
    that writes ``.preview.srt`` only.
    """

    FULL = "full"
    PREVIEW = "preview"


def infer_execution_scope(
    *,
    stored: JobExecutionScope | str | None = None,
    preview_seconds: Seconds | None = None,
) -> JobExecutionScope:
    """Resolve scope for new jobs and for rows from older databases.

    An explicit stored value wins. Otherwise a non-null ``preview_seconds``
    means preview (legacy SQLite rows). Scanner jobs have neither.
    """
    if stored is not None and stored != "":
        return stored if isinstance(stored, JobExecutionScope) else JobExecutionScope(stored)
    if preview_seconds is not None:
        return JobExecutionScope.PREVIEW
    return JobExecutionScope.FULL


def infer_job_kind(stored: JobKind | str | None = None) -> JobKind:
    """Resolve kind for new jobs and for rows written before schema version 4."""

    if stored is None or stored == "":
        return JobKind.SUBTITLES
    return stored if isinstance(stored, JobKind) else JobKind(stored)


LIBRARY_SCAN_KNOWN_STATES: frozenset[JobState] = frozenset(JobState)
"""States of a *full* job that mean the scanner must not enqueue another.

Preview jobs are ignored regardless of state. Full jobs in queued, running,
retry_wait, needs_review, ready_to_publish, completed, skipped, failed or
cancelled all count: restart and retry reuse that row instead of duplicating.
"""


TERMINAL_JOB_STATES: frozenset[JobState] = frozenset(
    {JobState.COMPLETED, JobState.SKIPPED, JobState.FAILED, JobState.CANCELLED}
)


class PipelineStage(StrEnum):
    PROBE = "probe"
    DETECT_LANGUAGE = "detect_language"
    EXTRACT = "extract"
    TRANSCRIBE = "transcribe"
    MERGE = "merge"
    TRANSLATE = "translate"
    RENDER = "render"
    VALIDATE = "validate"
    PUBLISH = "publish"
    SEPARATE = "separate"
    ADAPT = "adapt"
    SYNTHESIZE = "synthesize"
    SYNC = "sync"
    MIX = "mix"
    VALIDATE_AUDIO = "validate_audio"


PIPELINE_STAGE_ORDER: tuple[PipelineStage, ...] = (
    PipelineStage.PROBE,
    PipelineStage.DETECT_LANGUAGE,
    PipelineStage.EXTRACT,
    PipelineStage.TRANSCRIBE,
    PipelineStage.MERGE,
    PipelineStage.TRANSLATE,
    PipelineStage.RENDER,
    PipelineStage.VALIDATE,
    PipelineStage.PUBLISH,
)
"""Subtitle pipeline. Dubbing uses :data:`DUBBING_STAGE_ORDER`."""

DUBBING_STAGE_ORDER: tuple[PipelineStage, ...] = (
    PipelineStage.PROBE,
    PipelineStage.DETECT_LANGUAGE,
    PipelineStage.EXTRACT,
    PipelineStage.SEPARATE,
    PipelineStage.TRANSCRIBE,
    PipelineStage.MERGE,
    PipelineStage.TRANSLATE,
    PipelineStage.ADAPT,
    PipelineStage.SYNTHESIZE,
    PipelineStage.SYNC,
    PipelineStage.MIX,
    PipelineStage.VALIDATE_AUDIO,
    PipelineStage.PUBLISH,
)
"""Dubbing pipeline. Does not use SRT ``render``/``validate`` stages."""


def stages_for(job_kind: JobKind | str | None) -> tuple[PipelineStage, ...]:
    """Stage sequence for a job kind. Subtitle order is unchanged."""

    if infer_job_kind(job_kind) is JobKind.DUBBING:
        return DUBBING_STAGE_ORDER
    return PIPELINE_STAGE_ORDER


def stage_window(
    stages: Sequence[PipelineStage],
    *,
    start_stage: PipelineStage | None = None,
    stop_after: PipelineStage | None = None,
) -> tuple[PipelineStage, ...]:
    """Inclusive slice of ``stages`` honoured by ``run_job``.

    ``start_stage`` and ``stop_after`` must belong to ``stages``. An inverted
    window is rejected rather than silently running the whole pipeline.
    """

    ordered = tuple(stages)
    if not ordered:
        raise ValueError("stage sequence must not be empty")
    start_index = 0 if start_stage is None else ordered.index(start_stage)
    end_index = len(ordered) - 1 if stop_after is None else ordered.index(stop_after)
    if end_index < start_index:
        raise ValueError("stop_after precedes start_stage")
    return ordered[start_index : end_index + 1]


class PublishMode(StrEnum):
    STAGING = "staging"
    SIDECAR = "sidecar"


class ExistingSubtitlePolicy(StrEnum):
    """What automatic processing does when the canonical target sidecar exists."""

    SKIP = "skip"


class EventLevel(StrEnum):
    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class ModelKind(StrEnum):
    ASR = "asr"
    TRANSLATION = "translation"
    TTS = "tts"
    SEPARATION = "separation"


class LanguageSource(StrEnum):
    METADATA = "metadata"
    DETECTION = "detection"
    OVERRIDE = "override"


class SubtitleOrigin(StrEnum):
    EXTERNAL_FILE = "external_file"
    EMBEDDED_STREAM = "embedded_stream"


class PublishOutcome(StrEnum):
    PUBLISHED = "published"
    CONFLICT = "conflict"
    UNSUPPORTED = "unsupported"


class QualitySeverity(StrEnum):
    STRUCTURAL = "structural"
    """Blocks publication outright; ``approve`` cannot override it."""

    ADVISORY = "advisory"
    """Leaves the result in staging as ``needs_review``."""


class QualityFlagCode(StrEnum):
    # Structural: the subtitle file itself is wrong.
    EMPTY_CUE_TEXT = "empty_cue_text"
    NON_MONOTONIC_TIMESTAMPS = "non_monotonic_timestamps"
    TIMESTAMP_OUT_OF_RANGE = "timestamp_out_of_range"
    INDEX_NOT_SEQUENTIAL = "index_not_sequential"
    TRANSLATION_MISSING = "translation_missing"
    SRT_ROUNDTRIP_FAILED = "srt_roundtrip_failed"
    CUE_OVERLAP = "cue_overlap"
    # Advisory: readable but worth a human look.
    READING_SPEED_EXCEEDED = "reading_speed_exceeded"
    LINE_WIDTH_EXCEEDED = "line_width_exceeded"
    LINE_COUNT_EXCEEDED = "line_count_exceeded"
    DURATION_BELOW_MINIMUM = "duration_below_minimum"
    DURATION_ABOVE_MAXIMUM = "duration_above_maximum"
    LOW_CONFIDENCE = "low_confidence"
    REPEATED_TEXT = "repeated_text"
    TEXT_WITHOUT_SPEECH = "text_without_speech"
    LANGUAGE_UNCERTAIN = "language_uncertain"
    # Dubbing advisories and audio integrity (roadmap 006).
    DIALOGUE_LEAK = "dialogue_leak"
    SPEED_LIMIT_EXCEEDED = "speed_limit_exceeded"
    OVERLAP_UNRESOLVED = "overlap_unresolved"
    CLIPPING_DETECTED = "clipping_detected"
    WORD_TRUNCATED = "word_truncated"


STRUCTURAL_FLAG_CODES: frozenset[QualityFlagCode] = frozenset(
    {
        QualityFlagCode.EMPTY_CUE_TEXT,
        QualityFlagCode.NON_MONOTONIC_TIMESTAMPS,
        QualityFlagCode.TIMESTAMP_OUT_OF_RANGE,
        QualityFlagCode.INDEX_NOT_SEQUENTIAL,
        QualityFlagCode.TRANSLATION_MISSING,
        QualityFlagCode.SRT_ROUNDTRIP_FAILED,
        QualityFlagCode.CUE_OVERLAP,
    }
)


# --------------------------------------------------------------------------- #
# Exceptions
# --------------------------------------------------------------------------- #


class NasSubtitlesError(Exception):
    """Base error carrying a stable code and the matching process exit code."""

    default_code: ErrorCode = ErrorCode.IO_ERROR

    def __init__(
        self,
        message: str,
        *,
        code: ErrorCode | None = None,
        detail: Mapping[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code if code is not None else self.default_code
        self.detail: Mapping[str, object] = dict(detail or {})

    @property
    def exit_code(self) -> ExitCode:
        return exit_code_for(self.code)


class ConfigurationError(NasSubtitlesError):
    default_code = ErrorCode.CONFIG_INVALID


class LockBusyError(NasSubtitlesError):
    default_code = ErrorCode.LOCK_BUSY


# --------------------------------------------------------------------------- #
# Media inspection
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class AudioStreamInfo:
    """One audio stream as reported by ``ffprobe``.

    ``index`` is the *global* stream index (``ffprobe`` ``index`` field, used as
    ``-map 0:<index>``). It is never the relative ``a:N`` ordinal.
    """

    index: int
    codec_name: str | None = None
    language: str | None = None
    """Normalised two-letter code, or ``None`` when the tag is absent."""
    raw_language_tag: str | None = None
    channels: int | None = None
    sample_rate: int | None = None
    start_time_seconds: Seconds = 0.0
    """Offset of the stream relative to the video timeline; often non-zero."""
    duration_seconds: Seconds | None = None
    title: str | None = None
    is_default: bool = False
    is_forced: bool = False
    is_commentary: bool = False
    """Heuristic from disposition and title tags, never a certainty."""


@dataclass(frozen=True, slots=True)
class SubtitleStreamInfo:
    """One embedded subtitle stream as reported by ``ffprobe``."""

    index: int
    codec_name: str | None = None
    language: str | None = None
    raw_language_tag: str | None = None
    title: str | None = None
    is_default: bool = False
    is_forced: bool = False
    """A forced stream never satisfies the "already has Portuguese" test."""


@dataclass(frozen=True, slots=True)
class ProbeResult:
    path: Path
    duration_seconds: Seconds
    size_bytes: int
    container_format: str | None = None
    audio_streams: tuple[AudioStreamInfo, ...] = ()
    subtitle_streams: tuple[SubtitleStreamInfo, ...] = ()

    def stream_by_index(self, index: int) -> AudioStreamInfo | None:
        return next((s for s in self.audio_streams if s.index == index), None)


@dataclass(frozen=True, slots=True)
class MediaFingerprint:
    """Cheap change detector for a media file.

    Sampling 1 MiB from each end is not cryptographic proof that two files are
    identical; it exists to catch the usual kinds of change (re-encode, resume
    of a partial download, replacement). Renaming a file produces a different
    ``relative_path`` and therefore a different job.
    """

    root_id: str
    relative_path: str
    size_bytes: int
    mtime_ns: int
    head_sha256: str
    tail_sha256: str
    audio_stream_index: int | None = None

    def digest(self) -> str:
        """Stable identity including the selected audio stream."""
        return stable_digest(
            {
                "root_id": self.root_id,
                "relative_path": self.relative_path,
                "size_bytes": self.size_bytes,
                "mtime_ns": self.mtime_ns,
                "head_sha256": self.head_sha256,
                "tail_sha256": self.tail_sha256,
                "audio_stream_index": self.audio_stream_index,
            }
        )

    def content_digest(self) -> str:
        """Identity of the bytes only, ignoring which audio stream was chosen."""
        return stable_digest(
            {
                "root_id": self.root_id,
                "relative_path": self.relative_path,
                "size_bytes": self.size_bytes,
                "mtime_ns": self.mtime_ns,
                "head_sha256": self.head_sha256,
                "tail_sha256": self.tail_sha256,
            }
        )

    def content_matches(self, other: MediaFingerprint) -> bool:
        """True when the file looks unchanged, re-checked just before publish."""
        return self.content_digest() == other.content_digest()


@dataclass(frozen=True, slots=True)
class ExistingSubtitle:
    """A subtitle already present, found on disk or inside the file."""

    origin: SubtitleOrigin
    language: str | None
    is_forced: bool = False
    path: Path | None = None
    stream_index: int | None = None
    satisfies_target: bool = False
    """``True`` only for a complete, non-forced Portuguese subtitle.

    Per-target skip uses :func:`nas_subtitles.discovery.subtitle_satisfies_language`
    so an English sidecar does not satisfy a ``pt-BR`` job and vice versa.
    """
    uncertain: bool = False
    """Missing metadata does not prove the absence of a matching subtitle."""
    reason: str = ""


# --------------------------------------------------------------------------- #
# Chunking and transcription
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class AudioChunkSpec:
    """Planned extraction window for chunk ``index``.

    Chunk ``k`` *owns* ``[k * chunk_seconds, min((k + 1) * chunk_seconds,
    duration))``. The extraction window is wider by the configured overlap so
    the decoder has context, but words are only kept when their midpoint falls
    inside the owned interval. All four values are absolute video timestamps.
    """

    index: int
    owned_start_seconds: Seconds
    owned_end_seconds: Seconds
    extract_start_seconds: Seconds
    extract_end_seconds: Seconds

    @property
    def owned_duration_seconds(self) -> Seconds:
        return self.owned_end_seconds - self.owned_start_seconds

    @property
    def extract_duration_seconds(self) -> Seconds:
        return self.extract_end_seconds - self.extract_start_seconds

    def owns(self, timestamp: Seconds) -> bool:
        """Half-open ownership test, so no timestamp belongs to two chunks."""
        return self.owned_start_seconds <= timestamp < self.owned_end_seconds


@dataclass(frozen=True, slots=True)
class AudioChunk:
    """An extracted PCM file on disk together with the window it came from."""

    spec: AudioChunkSpec
    path: Path
    sample_rate: int = AUDIO_SAMPLE_RATE_HZ
    channels: int = AUDIO_CHANNELS
    sha256: str | None = None


@dataclass(frozen=True, slots=True)
class Word:
    """A single ASR token with absolute video timestamps."""

    text: str
    start_seconds: Seconds
    end_seconds: Seconds
    probability: float | None = None
    chunk_index: int | None = None

    @property
    def midpoint_seconds(self) -> Seconds:
        return (self.start_seconds + self.end_seconds) / 2.0


@dataclass(frozen=True, slots=True)
class TranscriptSegment:
    """An ASR segment with absolute video timestamps and its raw scores."""

    index: int
    start_seconds: Seconds
    end_seconds: Seconds
    text: str
    words: tuple[Word, ...] = ()
    avg_logprob: float | None = None
    no_speech_probability: float | None = None
    compression_ratio: float | None = None
    temperature: float | None = None
    chunk_index: int | None = None


@dataclass(frozen=True, slots=True)
class ChunkTranscript:
    """Result of transcribing one chunk, already converted to absolute time."""

    chunk: AudioChunkSpec
    language: str
    segments: tuple[TranscriptSegment, ...] = ()
    language_probability: float | None = None
    model_identity: ModelIdentity | None = None


@dataclass(frozen=True, slots=True)
class Transcript:
    """Merged, globally ordered transcript for the whole media file."""

    language: str
    duration_seconds: Seconds
    segments: tuple[TranscriptSegment, ...] = ()
    words: tuple[Word, ...] = ()


@dataclass(frozen=True, slots=True)
class LanguageSample:
    """One probe window used for ASR language detection."""

    offset_seconds: Seconds
    duration_seconds: Seconds
    language: str | None
    probability: float | None
    has_speech: bool
    """Samples without speech are ignored rather than counted as disagreement."""


@dataclass(frozen=True, slots=True)
class LanguageDecision:
    """Outcome of deciding the source language, never asked interactively."""

    language: str | None
    source: LanguageSource
    confident: bool
    probability: float | None = None
    samples: tuple[LanguageSample, ...] = ()
    reason: str = ""


# --------------------------------------------------------------------------- #
# Translation and rendering
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class TranslationUnit:
    """A sentence-like group of words sent to the translator as one piece."""

    unit_id: UnitId
    source_text: str
    start_seconds: Seconds
    end_seconds: Seconds
    source_language: str
    target_language: str
    word_count: int = 0


@dataclass(frozen=True, slots=True)
class TranslatedUnit:
    """Translation of exactly one :class:`TranslationUnit`.

    The mapping is one to one on purpose: concatenating many cues and splitting
    the output by line count would destroy the time correspondence.
    """

    unit_id: UnitId
    source_text: str
    translated_text: str
    source_language: str
    target_language: str
    engine_identity: str
    from_cache: bool = False


@dataclass(frozen=True, slots=True)
class SubtitleCue:
    """One rendered cue. ``index`` is 1-based and consecutive in the file."""

    index: int
    start_seconds: Seconds
    end_seconds: Seconds
    lines: tuple[str, ...]
    unit_ids: tuple[UnitId, ...] = ()

    @property
    def text(self) -> str:
        return "\n".join(self.lines)

    @property
    def duration_seconds(self) -> Seconds:
        return self.end_seconds - self.start_seconds

    @property
    def characters_per_second(self) -> float:
        duration = self.duration_seconds
        if duration <= 0:
            return float("inf")
        return len(self.text.replace("\n", " ")) / duration


@dataclass(frozen=True, slots=True)
class QualityFlag:
    """A single observation from the quality gates, never a text deletion."""

    code: QualityFlagCode
    message: str
    severity: QualitySeverity = QualitySeverity.ADVISORY
    cue_index: int | None = None
    unit_id: UnitId | None = None
    observed: float | None = None
    threshold: float | None = None


@dataclass(frozen=True, slots=True)
class QualityReport:
    flags: tuple[QualityFlag, ...] = ()

    @property
    def structural_errors(self) -> tuple[QualityFlag, ...]:
        return tuple(f for f in self.flags if f.severity is QualitySeverity.STRUCTURAL)

    @property
    def advisories(self) -> tuple[QualityFlag, ...]:
        return tuple(f for f in self.flags if f.severity is QualitySeverity.ADVISORY)

    @property
    def blocks_publication(self) -> bool:
        return bool(self.structural_errors)

    @property
    def requires_review(self) -> bool:
        return bool(self.flags)


# --------------------------------------------------------------------------- #
# Dubbing (roadmap 006)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class VoiceAssignment:
    """Fixed Piper voice or a clone reference, scoped to one speaker."""

    speaker_id: str
    voice_id: str
    kind: VoiceKind = VoiceKind.FIXED
    reference_sha256: str | None = None


@dataclass(frozen=True, slots=True)
class DubSegment:
    """One speech window on the video timeline, versioned for plan apply."""

    segment_id: str
    job_id: JobId
    revision: int
    start_seconds: Seconds
    end_seconds: Seconds
    original_text: str = ""
    translated_text: str = ""
    adapted_text: str = ""
    speaker_id: str | None = None
    review_state: DubSegmentReviewState = DubSegmentReviewState.PENDING


@dataclass(frozen=True, slots=True)
class SynthesisArtifact:
    """On-disk synthesis of one :class:`DubSegment` revision."""

    job_id: JobId
    segment_id: str
    revision: int
    path: Path
    duration_seconds: Seconds
    model_identity: str
    sha256: str
    seed: int | None = None


@dataclass(frozen=True, slots=True)
class DubbingQualityReport:
    """Audio gates for a dubbing job. Distinct from subtitle :class:`QualityReport`."""

    flags: tuple[QualityFlag, ...] = ()
    coverage_complete: bool = True
    peak_dbtp: float | None = None

    @property
    def structural_errors(self) -> tuple[QualityFlag, ...]:
        return tuple(flag for flag in self.flags if flag.severity is QualitySeverity.STRUCTURAL)

    @property
    def blocks_publication(self) -> bool:
        return bool(self.structural_errors)

    @property
    def requires_review(self) -> bool:
        return bool(self.flags) or not self.coverage_complete


@dataclass(frozen=True, slots=True)
class SeparatedAudio:
    """Dialogue and optional accompaniment for one owned chunk."""

    chunk: AudioChunkSpec
    dialogue_path: Path
    accompaniment_path: Path | None = None
    sha256: str | None = None
    model_identity: ModelIdentity | None = None


# --------------------------------------------------------------------------- #
# Models, records and manifests
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ModelIdentity:
    """Provenance of a local model, recorded in the manifest and checkpoints."""

    kind: ModelKind
    name: str
    path: Path
    """Absolute local path actually loaded; never a hub alias that hits the network."""
    version: str | None = None
    revision: str | None = None
    sha256: str | None = None
    source: str | None = None
    license: str | None = None

    def identity_token(self) -> str:
        """Stable token used to invalidate checkpoints and the translation cache."""
        return stable_digest(
            {
                "kind": self.kind,
                "name": self.name,
                "version": self.version,
                "revision": self.revision,
                "sha256": self.sha256,
            }
        )[:32]


@dataclass(frozen=True, slots=True)
class ArtifactRecord:
    job_id: JobId
    stage: PipelineStage
    path: Path
    sha256: str
    schema_version: int
    stage_config_hash: str
    chunk_index: int | None = None
    segment_id: str | None = None
    revision: int | None = None
    created_at: datetime | None = None
    id: int | None = None


@dataclass(frozen=True, slots=True)
class JobEvent:
    level: EventLevel
    code: str
    job_id: JobId | None = None
    payload: Mapping[str, object] = field(default_factory=dict)
    created_at: datetime | None = None
    id: int | None = None


@dataclass(frozen=True, slots=True)
class JobMetrics:
    job_id: JobId
    media_seconds: Seconds = 0.0
    extraction_seconds: Seconds = 0.0
    asr_seconds: Seconds = 0.0
    translation_seconds: Seconds = 0.0
    total_seconds: Seconds = 0.0
    peak_rss_bytes: int | None = None
    output_cues: int = 0
    quality_flags: tuple[QualityFlag, ...] = ()

    @property
    def realtime_factor(self) -> float | None:
        """Processing seconds per second of audio; ``None`` when unmeasured."""
        if self.media_seconds <= 0:
            return None
        return self.total_seconds / self.media_seconds


@dataclass(frozen=True, slots=True)
class JobRecord:
    id: JobId
    root_id: str
    relative_path: str
    fingerprint: MediaFingerprint
    pipeline_config_hash: str
    state: JobState
    created_at: datetime
    updated_at: datetime
    current_stage: PipelineStage | None = None
    priority: int = 0
    attempt_count: int = 0
    next_attempt_at: datetime | None = None
    lease_owner: str | None = None
    lease_expires_at: datetime | None = None
    error_code: ErrorCode | None = None
    error_detail: str | None = None
    output_path: Path | None = None
    source_language_override: str | None = None
    audio_stream_index_override: int | None = None
    """Global ffprobe stream index supplied on the CLI, never an ``a:N`` ordinal."""
    preview_seconds: Seconds | None = None
    preview_offset_seconds: Seconds | None = None
    approved_at: datetime | None = None
    execution_scope: JobExecutionScope | None = None
    target_language: str | None = None
    """Public destination tag for this job (``pt-BR``, ``en``). Never Argos ``pb``.

    Shared contract change for roadmap 005: each configured target is its own
    job identity. ``None`` means “use the configured primary target”, which
    keeps rows written before schema version 3 readable.
    """
    job_kind: JobKind | None = None
    """``subtitles`` or ``dubbing``. ``None`` on read becomes ``subtitles``.

    Shared contract change for roadmap 006: an existing `.pt-BR.srt` does not
    satisfy a dubbing job, and the two kinds never share a uniqueness row.
    """
    dubbing_profile: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "execution_scope",
            infer_execution_scope(
                stored=self.execution_scope, preview_seconds=self.preview_seconds
            ),
        )
        object.__setattr__(self, "job_kind", infer_job_kind(self.job_kind))

    def is_library_job(self) -> bool:
        """True when this job is the scanner's library-satisfying identity."""
        return (
            infer_execution_scope(stored=self.execution_scope, preview_seconds=self.preview_seconds)
            is JobExecutionScope.FULL
        )


@dataclass(frozen=True, slots=True)
class JobClaim:
    """A job leased to exactly one worker by a ``BEGIN IMMEDIATE`` transaction."""

    job: JobRecord
    lease_owner: str
    lease_expires_at: datetime


@dataclass(frozen=True, slots=True)
class ScanObservation:
    """One sighting of a file, used to require two stable observations."""

    root_id: str
    relative_path: str
    size_bytes: int
    mtime_ns: int
    first_stable_seen_at: datetime | None = None
    last_seen_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class TranslationCacheEntry:
    cache_key: str
    translated_text: str
    engine_identity: str
    created_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class PublishResult:
    """Outcome of an exclusive hard-link publish attempt."""

    outcome: PublishOutcome
    target_path: Path
    sha256: str | None = None
    conflict_path: Path | None = None
    message: str = ""


@dataclass(frozen=True, slots=True)
class JobManifest:
    """Provenance written to ``state``/staging, never next to the video."""

    job_id: JobId
    fingerprint: MediaFingerprint
    pipeline_config_hash: str
    source_language: str | None
    target_language: str
    selected_audio_stream_index: int | None = None
    stream_language: str | None = None
    stream_language_tag: str | None = None
    detected_language: str | None = None
    detection_probability: float | None = None
    source_language_source: LanguageSource | None = None
    source_language_confident: bool | None = None
    source_language_reason: str = ""
    translation_executed: bool | None = None
    translation_engine_identity: str | None = None
    models: tuple[ModelIdentity, ...] = ()
    metrics: JobMetrics | None = None
    quality: QualityReport = field(default_factory=QualityReport)
    artifacts: tuple[ArtifactRecord, ...] = ()
    subtitle_sha256: str | None = None
    created_at: datetime | None = None
    schema_version: int = MANIFEST_SCHEMA_VERSION


# --------------------------------------------------------------------------- #
# Engine protocols. Tests inject fakes; swapping an engine must not touch the
# queue or the output code.
# --------------------------------------------------------------------------- #


@runtime_checkable
class MediaProbe(Protocol):
    """Reads container metadata without decoding or modifying the media."""

    def probe(self, path: Path) -> ProbeResult: ...


@runtime_checkable
class AudioExtractor(Protocol):
    """Plans and extracts PCM chunks, preserving the absolute video timeline."""

    def plan_chunks(
        self,
        *,
        duration_seconds: Seconds,
        stream_start_seconds: Seconds,
        chunk_seconds: Seconds,
        overlap_seconds: Seconds,
    ) -> tuple[AudioChunkSpec, ...]: ...

    def extract(
        self,
        *,
        source: Path,
        stream_index: int,
        spec: AudioChunkSpec,
        destination: Path,
    ) -> AudioChunk: ...


@runtime_checkable
class Transcriber(Protocol):
    """Local ASR engine. Must never download anything at call time."""

    @property
    def model_identity(self) -> ModelIdentity: ...

    def detect_language(self, samples: Sequence[AudioChunk]) -> LanguageDecision: ...

    def transcribe(self, chunk: AudioChunk, *, language: str) -> ChunkTranscript: ...


@runtime_checkable
class Translator(Protocol):
    """Local translation engine, direct pair only, no pivot."""

    @property
    def engine_identity(self) -> str: ...

    def supports(self, *, source_language: str, target_language: str) -> bool: ...

    def translate(self, units: Sequence[TranslationUnit]) -> tuple[TranslatedUnit, ...]: ...


@runtime_checkable
class SubtitleRenderer(Protocol):
    """Serialises and re-parses cues so a round-trip can be asserted."""

    def render(self, cues: Sequence[SubtitleCue]) -> str: ...

    def parse(self, content: str) -> tuple[SubtitleCue, ...]: ...


@runtime_checkable
class DialogueSeparator(Protocol):
    """Local dialogue/accompaniment splitter. Must never download at call time."""

    @property
    def model_identity(self) -> ModelIdentity: ...

    def separate(self, chunk: AudioChunk, *, destination_dir: Path) -> SeparatedAudio: ...


@runtime_checkable
class SpeechSynthesizer(Protocol):
    """Local TTS. Must never download at call time."""

    @property
    def model_identity(self) -> ModelIdentity: ...

    def synthesize(
        self,
        segment: DubSegment,
        *,
        voice: VoiceAssignment,
        destination: Path,
    ) -> SynthesisArtifact: ...


@runtime_checkable
class TimelineRenderer(Protocol):
    """Places synthesised takes onto the absolute video timeline."""

    def render(
        self,
        *,
        artifacts: Sequence[SynthesisArtifact],
        starts_seconds: Mapping[str, Seconds],
        duration_seconds: Seconds,
        destination: Path,
    ) -> Path:
        """``starts_seconds`` maps ``segment_id`` to its absolute start on the video."""
        ...


@runtime_checkable
class AudioMixer(Protocol):
    """Mixes dubbed dialogue with accompaniment without doubling the original."""

    def mix(
        self,
        *,
        dialogue: Path,
        accompaniment: Path | None,
        destination: Path,
    ) -> Path: ...


@runtime_checkable
class JobRepository(Protocol):
    """Persistence boundary for the queue, artifacts, events and caches."""

    def initialise(self) -> None: ...

    def close(self) -> None: ...

    def enqueue(
        self,
        *,
        fingerprint: MediaFingerprint,
        pipeline_config_hash: str,
        priority: int = 0,
        source_language_override: str | None = None,
        audio_stream_index_override: int | None = None,
        preview_seconds: Seconds | None = None,
        preview_offset_seconds: Seconds | None = None,
        target_language: str | None = None,
        job_kind: JobKind | None = None,
        dubbing_profile: str | None = None,
    ) -> JobRecord: ...

    def list_dub_segments(
        self, *, job_id: JobId, revision: int | None = None
    ) -> tuple[DubSegment, ...]: ...

    def replace_dub_plan(
        self, *, job_id: JobId, revision: int, segments: Sequence[DubSegment]
    ) -> tuple[DubSegment, ...]: ...

    def get_job(self, job_id: JobId) -> JobRecord | None: ...

    def list_jobs(
        self, *, state: JobState | None = None, limit: int = 100
    ) -> tuple[JobRecord, ...]: ...

    def claim_next_job(self, *, owner: str, lease_seconds: int) -> JobClaim | None: ...

    def renew_lease(self, *, job_id: JobId, owner: str, lease_seconds: int) -> bool: ...

    def transition(
        self,
        *,
        job_id: JobId,
        state: JobState,
        stage: PipelineStage | None = None,
        error_code: ErrorCode | None = None,
        error_detail: str | None = None,
        next_attempt_at: datetime | None = None,
        output_path: Path | None = None,
    ) -> JobRecord: ...

    def approve_job(self, *, job_id: JobId) -> JobRecord: ...

    def record_artifact(self, artifact: ArtifactRecord) -> ArtifactRecord: ...

    def list_artifacts(
        self, *, job_id: JobId, stage: PipelineStage | None = None
    ) -> tuple[ArtifactRecord, ...]: ...

    def append_event(self, event: JobEvent) -> None: ...

    def latest_event_at(self, *, code: str) -> datetime | None: ...

    def record_metrics(self, metrics: JobMetrics) -> None: ...

    def get_translation(self, cache_key: str) -> TranslationCacheEntry | None: ...

    def put_translation(self, entry: TranslationCacheEntry) -> None: ...

    def get_scan_observation(
        self, *, root_id: str, relative_path: str
    ) -> ScanObservation | None: ...

    def upsert_scan_observation(self, observation: ScanObservation) -> ScanObservation: ...

    def backup_to(self, destination: Path) -> None:
        """Must use ``sqlite3.Connection.backup``; copying the file is unsafe."""
        ...
