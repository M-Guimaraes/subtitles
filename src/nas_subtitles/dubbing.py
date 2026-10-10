"""Local pt-BR dubbing jobs. Owned by roadmap 006.

Phase 1 persists ``job_kind``, CLI commands and the speech-plan contract.
Synthesis, separation and mix engines arrive in later phases: missing engines
are ``not_implemented``, never a remote API. An existing subtitle sidecar
never prevents a dubbing job.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import subprocess
import uuid
import wave
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .config import AppConfig
from .discovery import compute_fingerprint, is_stable, observe, resolve_explicit_path
from .domain import (
    DUBBING_PLAN_SCHEMA_VERSION,
    AudioChunk,
    AudioMixer,
    AudioStreamInfo,
    DialogueSeparator,
    DubbingProfile,
    DubbingQualityReport,
    DubSegment,
    DubSegmentReviewState,
    ErrorCode,
    EventLevel,
    JobEvent,
    JobKind,
    JobMetrics,
    JobRecord,
    JobRepository,
    JobState,
    LanguageDecision,
    MediaProbe,
    ModelIdentity,
    ModelKind,
    NasSubtitlesError,
    PipelineStage,
    QualityFlag,
    QualityFlagCode,
    QualityReport,
    Seconds,
    SeparatedAudio,
    SpeechSynthesizer,
    SynthesisArtifact,
    TimelineRenderer,
    TranslatedUnit,
    TranslationUnit,
    VoiceAssignment,
    infer_job_kind,
    stage_window,
    stages_for,
)
from .language import (
    decide_source_language,
    effective_source_override,
    is_supported_source,
    translation_is_required,
)
from .logging_setup import log_event, path_token
from .media import FfprobeMediaProbe, ensure_free_space, plan_chunks, select_audio_stream
from .models import load_separation_model, load_tts_voice, read_model_manifest
from .transcription import merge_chunk_transcripts
from .translation import build_translation_units, translate_with_cache

if TYPE_CHECKING:
    from .pipeline import PipelineResult, StageContext

_LOG = logging.getLogger(__name__)

__all__ = [
    "DemucsSeparator",
    "DubEnqueueResult",
    "DubbingEngines",
    "FfmpegAudioMixer",
    "PiperSynthesizer",
    "WavTimelineRenderer",
    "apply_plan",
    "build_dubbing_engines",
    "enqueue_dubbing",
    "export_plan",
    "parse_dubbing_profile",
    "plan_payload",
    "run_dubbing_job",
]


@dataclass(frozen=True, slots=True)
class DubEnqueueResult:
    """Outcome of enqueueing one dubbing job."""

    job: JobRecord
    skipped: bool = False
    reason: str | None = None


def parse_dubbing_profile(value: str | DubbingProfile | None) -> DubbingProfile:
    """Parse a CLI/config profile name. Unknown values are invalid input."""

    if value is None:
        return DubbingProfile.CPU_FIXED
    if isinstance(value, DubbingProfile):
        return value
    try:
        return DubbingProfile(value)
    except ValueError as exc:
        raise NasSubtitlesError(
            f"unknown dubbing profile {value!r}; expected cpu-fixed or mac-clone",
            code=ErrorCode.CONFIG_INVALID,
            detail={"profile": value},
        ) from exc


def enqueue_dubbing(
    config: AppConfig,
    repository: JobRepository,
    path: Path,
    *,
    source_language: str | None = None,
    audio_stream_index: int | None = None,
    preview_seconds: float | None = None,
    preview_offset_seconds: float | None = None,
    priority: int = 0,
    profile: str | DubbingProfile | None = None,
    require_stability: bool = True,
    now: datetime | None = None,
    probe: MediaProbe | None = None,
) -> DubEnqueueResult:
    """Inspect one path and enqueue a dubbing job. Existing SRT is ignored."""

    moment = now or datetime.now(tz=UTC)
    root, resolved = resolve_explicit_path(config, path)
    if not resolved.is_file():
        raise NasSubtitlesError(
            "media path is not a file",
            code=ErrorCode.INVALID_MEDIA,
            detail={"path_token": path_token(resolved), "root_id": root.root_id},
        )
    observation = observe(repository, root=root, path=resolved, now=moment)
    if require_stability and not is_stable(config, observation, now=moment):
        raise NasSubtitlesError(
            "media is not yet stable",
            code=ErrorCode.MEDIA_UNSTABLE,
            detail={"root_id": root.root_id, "path_token": path_token(resolved)},
        )
    inspector = probe or FfprobeMediaProbe()
    probe_result = inspector.probe(resolved)
    stream = select_audio_stream(probe_result, override_index=audio_stream_index, config=config)
    fingerprint = compute_fingerprint(root=root, path=resolved, audio_stream_index=stream.index)
    resolved_profile = parse_dubbing_profile(profile or config.dubbing.profile)
    target = config.dubbing.target_language
    job = repository.enqueue(
        fingerprint=fingerprint,
        pipeline_config_hash=config.pipeline_config_hash_for(target, job_kind=JobKind.DUBBING),
        priority=priority,
        source_language_override=source_language,
        audio_stream_index_override=audio_stream_index,
        preview_seconds=preview_seconds,
        preview_offset_seconds=preview_offset_seconds,
        target_language=target,
        job_kind=JobKind.DUBBING,
        dubbing_profile=str(resolved_profile),
    )
    log_event(
        _LOG,
        "job queued",
        job_id=job.id,
        root_id=root.root_id,
        path_token=path_token(resolved),
        target_language=target,
        job_kind=str(JobKind.DUBBING),
        dubbing_profile=str(resolved_profile),
    )
    return DubEnqueueResult(job=job)


def plan_payload(job: JobRecord, segments: Sequence[DubSegment]) -> dict[str, object]:
    """JSON-serialisable speech plan. Contains no personal filesystem paths."""

    revision = segments[0].revision if segments else 0
    return {
        "schema_version": DUBBING_PLAN_SCHEMA_VERSION,
        "job_id": job.id,
        "job_kind": str(infer_job_kind(job.job_kind)),
        "target_language": job.target_language,
        "revision": revision,
        "segments": [
            {
                "id": item.segment_id,
                "start_seconds": item.start_seconds,
                "end_seconds": item.end_seconds,
                "original_text": item.original_text,
                "translated_text": item.translated_text,
                "adapted_text": item.adapted_text,
                "speaker_id": item.speaker_id,
                "review_state": str(item.review_state),
            }
            for item in segments
        ],
    }


def export_plan(
    repository: JobRepository, job_id: str, *, destination: Path
) -> tuple[JobRecord, Path]:
    """Write the latest plan next to *destination* without overwriting."""

    job = _require_dubbing_job(repository, job_id)
    payload = plan_payload(job, repository.list_dub_segments(job_id=job.id))
    written = _write_exclusive_json(destination, payload)
    return job, written


def apply_plan(
    repository: JobRepository, job_id: str, *, source: Path
) -> tuple[JobRecord, tuple[DubSegment, ...]]:
    """Replace the next revision of a plan after validating ids and base revision."""

    job = _require_dubbing_job(repository, job_id)
    raw = _read_plan_file(source)
    current = repository.list_dub_segments(job_id=job.id)
    current_revision = current[0].revision if current else 0
    declared_job = str(raw.get("job_id") or "")
    if declared_job and declared_job != job.id:
        raise NasSubtitlesError(
            "plan job_id does not match the target job",
            code=ErrorCode.CHECKPOINT_INVALID,
            detail={"job_id": job.id},
        )
    schema = raw.get("schema_version")
    if schema != DUBBING_PLAN_SCHEMA_VERSION:
        raise NasSubtitlesError(
            "plan schema_version is not supported",
            code=ErrorCode.CHECKPOINT_INVALID,
            detail={"schema_version": schema},
        )
    declared_revision = raw.get("revision")
    base_revision = (
        current_revision if declared_revision is None else _as_revision(declared_revision)
    )
    if base_revision != current_revision:
        raise NasSubtitlesError(
            "plan revision does not match the stored plan",
            code=ErrorCode.CHECKPOINT_INVALID,
            detail={"expected": current_revision, "received": base_revision},
        )
    incoming = _segments_from_payload(job.id, payload=raw, revision=current_revision + 1)
    if current:
        known = {item.segment_id for item in current}
        unknown = [item.segment_id for item in incoming if item.segment_id not in known]
        if unknown:
            raise NasSubtitlesError(
                "plan apply includes segment ids that are not in the base revision",
                code=ErrorCode.CHECKPOINT_INVALID,
                detail={"unknown_count": len(unknown)},
            )
    stored = repository.replace_dub_plan(
        job_id=job.id, revision=current_revision + 1, segments=incoming
    )
    return job, stored


def run_dubbing_job(
    context: StageContext,
    *,
    start_stage: PipelineStage | None = None,
    stop_after: PipelineStage | None = None,
    engines: DubbingEngines | None = None,
) -> PipelineResult:
    """Run the dubbing stage window.

    ``probe``, ``detect_language`` and ``extract`` are real: they reuse the
    same probe/transcriber/extractor the subtitle pipeline uses (never its
    private helpers — this stays inside roadmap-006 ownership) to pick the
    audio stream, vote on the source language and cut it into chunks.
    Every later stage (separate through publish) is real too, driven by
    ``engines`` (built from the installed models when omitted; fake engines
    in tests). Publication is staging only, never a sidecar. Resuming from a
    stage other than ``probe`` is not supported yet (no checkpoint is written
    here); see docs/roadmap/006-dubbing-completion-plan.md fase 4.
    """

    from .pipeline import PipelineResult

    config = context.config
    repo = context.repository
    job = context.job
    if infer_job_kind(job.job_kind) is not JobKind.DUBBING:
        raise NasSubtitlesError(
            "run_dubbing_job received a subtitle job",
            code=ErrorCode.INVALID_STATE_TRANSITION,
            detail={"job_id": job.id},
        )
    try:
        window = stage_window(
            stages_for(JobKind.DUBBING),
            start_stage=start_stage,
            stop_after=stop_after,
        )
    except ValueError as exc:
        raise NasSubtitlesError(str(exc), code=ErrorCode.CONFIG_INVALID) from exc
    ensure_free_space(config, config.work_dir)
    root = config.root_by_id(job.root_id)
    if root is None:
        raise NasSubtitlesError(
            "job root is no longer configured",
            code=ErrorCode.MEDIA_ROOT_MISSING,
        )
    video = root.path / job.relative_path
    target_language = config.target_language_for_job(job.target_language)

    job = repo.transition(job_id=job.id, state=JobState.RUNNING, stage=PipelineStage.PROBE)
    probe_result = context.probe.probe(video)
    stream = select_audio_stream(
        probe_result,
        override_index=job.audio_stream_index_override,
        config=config,
    )
    duration = probe_result.duration_seconds
    if job.preview_seconds is not None:
        offset = job.preview_offset_seconds or 0.0
        duration = min(duration, offset + job.preview_seconds)
    fingerprint = compute_fingerprint(root=root, path=video, audio_stream_index=stream.index)
    if not fingerprint.content_matches(job.fingerprint):
        raise NasSubtitlesError("media changed before processing", code=ErrorCode.MEDIA_CHANGED)
    if window[-1] is PipelineStage.PROBE:
        return PipelineResult(
            job_id=job.id,
            state=job.state,
            last_stage=PipelineStage.PROBE,
            quality=QualityReport(),
            media_seconds=duration,
        )

    job = repo.transition(
        job_id=job.id, state=JobState.RUNNING, stage=PipelineStage.DETECT_LANGUAGE
    )
    override = effective_source_override(config, job_override=job.source_language_override)
    decision = decide_source_language(
        config,
        stream=stream,
        override=override,
        transcriber=context.transcriber,
    )
    if override is None:
        samples = _dub_language_samples(
            context, video=video, stream_index=stream.index, duration=duration
        )
        asr_decision = context.transcriber.detect_language(samples)
        decision = decide_source_language(
            config,
            stream=stream,
            samples=asr_decision.samples,
            transcriber=context.transcriber,
        )
    _record_dub_language_decision(
        context, stream=stream, decision=decision, target_language=target_language
    )
    if not decision.confident or decision.language is None:
        job = repo.transition(
            job_id=job.id,
            state=JobState.NEEDS_REVIEW,
            error_code=ErrorCode.LANGUAGE_UNDETERMINED,
            error_detail=decision.reason,
        )
        return PipelineResult(
            job_id=job.id,
            state=job.state,
            last_stage=PipelineStage.DETECT_LANGUAGE,
            quality=QualityReport(),
            media_seconds=duration,
        )
    language = decision.language
    if not is_supported_source(language):
        job = repo.transition(
            job_id=job.id,
            state=JobState.FAILED,
            error_code=ErrorCode.UNSUPPORTED_LANGUAGE,
            error_detail=language,
        )
        return PipelineResult(
            job_id=job.id,
            state=job.state,
            last_stage=PipelineStage.DETECT_LANGUAGE,
            quality=QualityReport(),
            media_seconds=duration,
        )
    if window[-1] is PipelineStage.DETECT_LANGUAGE:
        return PipelineResult(
            job_id=job.id,
            state=job.state,
            last_stage=PipelineStage.DETECT_LANGUAGE,
            quality=QualityReport(),
            media_seconds=duration,
        )

    job = repo.transition(job_id=job.id, state=JobState.RUNNING, stage=PipelineStage.EXTRACT)
    specs = plan_chunks(
        duration_seconds=duration,
        stream_start_seconds=stream.start_time_seconds,
        chunk_seconds=float(config.asr.chunk_seconds),
        overlap_seconds=float(config.asr.overlap_seconds),
    )
    work = config.work_dir / job.id
    work.mkdir(parents=True, exist_ok=True)
    chunks: list[AudioChunk] = []
    for spec in specs:
        _check_dub_stop(context)
        ensure_free_space(config, work)
        wav = work / f"chunk-{spec.index:04d}.wav"
        chunks.append(
            context.extractor.extract(
                source=video, stream_index=stream.index, spec=spec, destination=wav
            )
        )
    if window[-1] is PipelineStage.EXTRACT:
        return PipelineResult(
            job_id=job.id,
            state=job.state,
            last_stage=PipelineStage.EXTRACT,
            quality=QualityReport(),
            media_seconds=duration,
        )

    resolved = engines if engines is not None else build_dubbing_engines(config)
    return _run_audio_stages(
        context,
        engines=resolved,
        window=window,
        job=job,
        chunks=chunks,
        language=language,
        target_language=target_language,
        fingerprint_digest=fingerprint.digest(),
        duration=duration,
        stream=stream,
    )


def _check_dub_stop(context: StageContext) -> None:
    """Local twin of ``pipeline.py``'s private ``_check_stop`` — same reason
    as ``_dub_language_samples``: never import a subtitle-pipeline internal."""
    if context.stop_event is not None and context.stop_event.is_set():
        raise NasSubtitlesError("interrupted by signal", code=ErrorCode.INTERRUPTED)


def _dub_language_samples(
    context: StageContext, *, video: Path, stream_index: int, duration: Seconds
) -> tuple[AudioChunk, ...]:
    """Two 8s samples for language detection.

    Deliberately not imported from ``pipeline.py`` (owned by stage 7):
    roadmap 006 may only read its public, shared helpers (``plan_chunks``),
    never a subtitle-pipeline private function. This mirrors that helper's
    shape exactly so the two stay easy to compare.
    """
    offsets = (0.0, max(0.0, duration / 2.0))
    chunks: list[AudioChunk] = []
    work = context.config.work_dir / context.job.id / "language"
    for index, offset in enumerate(offsets):
        spec = plan_chunks(
            duration_seconds=min(duration, offset + 8.0),
            stream_start_seconds=0.0,
            chunk_seconds=8.0,
            overlap_seconds=0.0,
        )
        if not spec:
            continue
        sample_spec = replace(
            spec[0],
            index=index,
            owned_start_seconds=offset,
            owned_end_seconds=min(duration, offset + 8.0),
            extract_start_seconds=offset,
            extract_end_seconds=min(duration, offset + 8.0),
        )
        destination = work / f"sample-{index}.wav"
        chunks.append(
            context.extractor.extract(
                source=video, stream_index=stream_index, spec=sample_spec, destination=destination
            )
        )
    return tuple(chunks)


def _record_dub_language_decision(
    context: StageContext,
    *,
    stream: AudioStreamInfo,
    decision: LanguageDecision,
    target_language: str,
) -> None:
    payload: dict[str, object] = {
        "selected_audio_stream_index": stream.index,
        "stream_language": stream.language,
        "detected_language": decision.language,
        "detection_probability": decision.probability,
        "source": str(decision.source),
        "confident": decision.confident,
        "target_language": target_language,
        "reason": decision.reason,
    }
    context.repository.append_event(
        JobEvent(
            level=EventLevel.INFO,
            code="language_decision",
            job_id=context.job.id,
            payload=payload,
        )
    )
    log_event(
        _LOG,
        "dubbing language decision",
        job_id=context.job.id,
        selected_audio_stream_index=stream.index,
        stream_language=stream.language,
        detected_language=decision.language,
        detection_probability=decision.probability,
        language_source=str(decision.source),
        confident=decision.confident,
        target_language=target_language,
        reason=decision.reason,
    )


def _require_dubbing_job(repository: JobRepository, job_id: str) -> JobRecord:
    job = repository.get_job(job_id)
    if job is None:
        raise NasSubtitlesError(
            f"job {job_id} was not found",
            code=ErrorCode.JOB_NOT_FOUND,
            detail={"job_id": job_id},
        )
    if infer_job_kind(job.job_kind) is not JobKind.DUBBING:
        raise NasSubtitlesError(
            "job is not a dubbing job",
            code=ErrorCode.INVALID_STATE_TRANSITION,
            detail={"job_id": job_id, "job_kind": str(infer_job_kind(job.job_kind))},
        )
    return job


def _read_plan_file(path: Path) -> Mapping[str, object]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise NasSubtitlesError(
            "plan file was not found",
            code=ErrorCode.INVALID_MEDIA,
            detail={"path_token": path_token(path)},
        ) from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise NasSubtitlesError(
            "plan file is not valid JSON",
            code=ErrorCode.CHECKPOINT_INVALID,
            detail={"path_token": path_token(path)},
        ) from exc
    if not isinstance(raw, dict):
        raise NasSubtitlesError(
            "plan file must contain a JSON object",
            code=ErrorCode.CHECKPOINT_INVALID,
        )
    return raw


def _segments_from_payload(
    job_id: str, *, payload: Mapping[str, object], revision: int
) -> tuple[DubSegment, ...]:
    raw_segments = payload.get("segments")
    if not isinstance(raw_segments, list):
        raise NasSubtitlesError(
            "plan segments must be a list",
            code=ErrorCode.CHECKPOINT_INVALID,
        )
    parsed: list[DubSegment] = []
    seen: set[str] = set()
    for index, item in enumerate(raw_segments):
        if not isinstance(item, dict):
            raise NasSubtitlesError(
                "plan segment is not an object",
                code=ErrorCode.CHECKPOINT_INVALID,
                detail={"index": index},
            )
        segment_id = str(item.get("id") or "").strip() or f"seg-{index:04d}"
        if segment_id in seen:
            raise NasSubtitlesError(
                "plan repeats a segment id",
                code=ErrorCode.CHECKPOINT_INVALID,
                detail={"segment_id": segment_id},
            )
        seen.add(segment_id)
        start = _as_seconds(item.get("start_seconds"), field="start_seconds")
        end = _as_seconds(item.get("end_seconds"), field="end_seconds")
        if end <= start:
            raise NasSubtitlesError(
                "plan segment end must be after start",
                code=ErrorCode.CHECKPOINT_INVALID,
                detail={"segment_id": segment_id},
            )
        review = item.get("review_state") or DubSegmentReviewState.PENDING
        try:
            review_state = DubSegmentReviewState(str(review))
        except ValueError as exc:
            raise NasSubtitlesError(
                "plan segment has an unknown review_state",
                code=ErrorCode.CHECKPOINT_INVALID,
                detail={"segment_id": segment_id},
            ) from exc
        parsed.append(
            DubSegment(
                segment_id=segment_id,
                job_id=job_id,
                revision=revision,
                start_seconds=start,
                end_seconds=end,
                original_text=str(item.get("original_text") or ""),
                translated_text=str(item.get("translated_text") or ""),
                adapted_text=str(item.get("adapted_text") or ""),
                speaker_id=_optional_str(item.get("speaker_id")),
                review_state=review_state,
            )
        )
    return tuple(parsed)


def _as_revision(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise NasSubtitlesError(
            "plan revision must be an integer",
            code=ErrorCode.CHECKPOINT_INVALID,
        )
    return value


def _as_seconds(value: object, *, field: str) -> Seconds:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise NasSubtitlesError(
            f"plan {field} must be a number of seconds",
            code=ErrorCode.CHECKPOINT_INVALID,
        )
    return float(value)


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _write_exclusive_json(destination: Path, payload: Mapping[str, object]) -> Path:
    """Create *destination* via a unique temp file and a hard link. Never overwrite."""

    destination.parent.mkdir(parents=True, exist_ok=True)
    tmp = destination.parent / f".{destination.name}.{uuid.uuid4().hex}.tmp"
    try:
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        try:
            os.link(tmp, destination)
        except FileExistsError as exc:
            raise NasSubtitlesError(
                "plan output already exists",
                code=ErrorCode.OUTPUT_CONFLICT,
                detail={"path_token": path_token(destination)},
            ) from exc
    finally:
        tmp.unlink(missing_ok=True)
    return destination


# --------------------------------------------------------------------------- #
# Local engines (roadmap 006 fase 2). Models load lazily and only from disk.
# --------------------------------------------------------------------------- #


def _read_pcm16(path: Path) -> tuple[Any, int]:
    """Read a 16-bit PCM WAV as a float tensor shaped (channels, samples)."""
    import torch

    with wave.open(str(path), "rb") as reader:
        if reader.getsampwidth() != 2:
            raise NasSubtitlesError("expected 16-bit PCM audio", code=ErrorCode.INVALID_MEDIA)
        rate = reader.getframerate()
        channels = reader.getnchannels()
        raw = reader.readframes(reader.getnframes())
    samples = torch.frombuffer(bytearray(raw), dtype=torch.int16).float() / 32768.0
    return samples.view(-1, channels).t().contiguous(), rate


def _write_pcm16(path: Path, audio: Any, rate: int) -> str:
    """Write a (channels, samples) float tensor atomically; return its sha256."""
    import torch

    clipped = torch.clamp(audio, -1.0, 1.0)
    pcm = (clipped * 32767.0).round().to(torch.int16).t().contiguous()
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with wave.open(str(temporary), "wb") as writer:
        writer.setnchannels(int(audio.shape[0]))
        writer.setsampwidth(2)
        writer.setframerate(rate)
        writer.writeframes(pcm.numpy().tobytes())
    temporary.replace(path)
    return hashlib.sha256(path.read_bytes()).hexdigest()


class DemucsSeparator:
    """``DialogueSeparator`` over the installed Demucs ``htdemucs`` bag.

    Dialogue is the ``vocals`` stem; the accompaniment is every other stem
    summed. Music separation is not cinematic-effects separation: effects and
    room tone may leak into either stem. Output keeps the chunk's sample rate
    and channel count, so accompaniment extracted as 16 kHz mono stays that.
    """

    def __init__(self, config: AppConfig, *, model_identity: ModelIdentity) -> None:
        self._config = config
        self._identity = model_identity
        self._model: Any = None

    @property
    def model_identity(self) -> ModelIdentity:
        return self._identity

    def _loaded(self) -> Any:
        if self._model is None:
            self._model = load_separation_model(self._config)
            self._model.eval()
        return self._model

    def separate(self, chunk: AudioChunk, *, destination_dir: Path) -> SeparatedAudio:
        import torch
        from demucs.apply import apply_model
        from julius.resample import resample_frac

        model = self._loaded()
        audio, rate = _read_pcm16(chunk.path)
        channels = int(audio.shape[0])
        if rate != model.samplerate:
            audio = resample_frac(audio, rate, model.samplerate)
        if audio.shape[0] != model.audio_channels:
            audio = audio.mean(dim=0, keepdim=True).repeat(model.audio_channels, 1)
        reference = audio.mean(dim=0)
        mean = reference.mean()
        std = reference.std()
        if not bool(std > 1e-8):
            std = torch.ones(())
        with torch.no_grad():
            stems = apply_model(
                model,
                ((audio - mean) / std)[None],
                device="cpu",
                split=True,
                overlap=0.25,
                progress=False,
            )[0]
        stems = stems * std + mean
        vocals = stems[list(model.sources).index("vocals")]
        accompaniment = stems.sum(dim=0) - vocals

        def finish(stem: Any) -> Any:
            if rate != model.samplerate:
                stem = resample_frac(stem, model.samplerate, rate)
            return stem.mean(dim=0, keepdim=True).repeat(channels, 1)

        destination_dir.mkdir(parents=True, exist_ok=True)
        index = chunk.spec.index
        dialogue_path = destination_dir / f"dialogue-{index:04d}.wav"
        accompaniment_path = destination_dir / f"accompaniment-{index:04d}.wav"
        digest = _write_pcm16(dialogue_path, finish(vocals), rate)
        _write_pcm16(accompaniment_path, finish(accompaniment), rate)
        return SeparatedAudio(
            chunk=chunk.spec,
            dialogue_path=dialogue_path,
            accompaniment_path=accompaniment_path,
            sha256=digest,
            model_identity=self._identity,
        )


class PiperSynthesizer:
    """``SpeechSynthesizer`` over the installed Piper voice (fixed voice only)."""

    def __init__(self, config: AppConfig, *, model_identity: ModelIdentity) -> None:
        self._config = config
        self._identity = model_identity
        self._voice: Any = None

    @property
    def model_identity(self) -> ModelIdentity:
        return self._identity

    def _loaded(self) -> Any:
        if self._voice is None:
            self._voice = load_tts_voice(self._config)
        return self._voice

    def synthesize(
        self,
        segment: DubSegment,
        *,
        voice: VoiceAssignment,
        destination: Path,
    ) -> SynthesisArtifact:
        if voice.voice_id != self._identity.name:
            raise NasSubtitlesError(
                f"voice {voice.voice_id} is not the installed voice {self._identity.name}",
                code=ErrorCode.MODEL_MISSING,
            )
        text = (segment.adapted_text or segment.translated_text).strip()
        if not text:
            raise NasSubtitlesError(
                f"segment {segment.segment_id} has no text to synthesize",
                code=ErrorCode.EMPTY_TRANSLATION,
            )
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
        with wave.open(str(temporary), "wb") as writer:
            self._loaded().synthesize_wav(text, writer)
        with wave.open(str(temporary), "rb") as reader:
            duration = reader.getnframes() / float(reader.getframerate())
        temporary.replace(destination)
        return SynthesisArtifact(
            job_id=segment.job_id,
            segment_id=segment.segment_id,
            revision=segment.revision,
            path=destination,
            duration_seconds=duration,
            model_identity=self._identity.identity_token(),
            sha256=hashlib.sha256(destination.read_bytes()).hexdigest(),
            seed=None,
        )


# --------------------------------------------------------------------------- #
# Timeline, mix and the audio stages (roadmap 006 fase 3)
# --------------------------------------------------------------------------- #

_TRUE_PEAK_PATTERN = re.compile(r"True peak:\s+Peak:\s+(-?\d+(?:\.\d+)?|-inf)\s+dBFS")
_SPEAKER_ID = "speaker-1"
_CLIPPING_THRESHOLD_DBTP = -1.0
_SUBPROCESS_TIMEOUT_SECONDS = 1800.0


def _run_ffmpeg(argv: Sequence[str], *, timeout: float = _SUBPROCESS_TIMEOUT_SECONDS) -> str:
    """Run an argv list (never a shell) and return stderr; failures are structured."""
    try:
        completed = subprocess.run(
            list(argv), capture_output=True, text=True, timeout=timeout, check=False, shell=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise NasSubtitlesError(
            f"{argv[0]} could not run to completion", code=ErrorCode.SUBPROCESS_FAILED
        ) from exc
    if completed.returncode != 0:
        raise NasSubtitlesError(
            f"{argv[0]} failed with exit code {completed.returncode}",
            code=ErrorCode.SUBPROCESS_FAILED,
        )
    return completed.stderr


def measure_true_peak_dbtp(path: Path) -> float | None:
    """True peak of an audio file in dBTP via ffmpeg's ``ebur128``; ``None`` if unparsable."""
    stderr = _run_ffmpeg(
        [
            "ffmpeg",
            "-hide_banner",
            "-nostats",
            "-i",
            str(path),
            "-af",
            "ebur128=peak=true",
            "-f",
            "null",
            "-",
        ]
    )
    found = _TRUE_PEAK_PATTERN.findall(stderr)
    if not found:
        return None
    return -math.inf if found[-1] == "-inf" else float(found[-1])


class WavTimelineRenderer:
    """``TimelineRenderer``: places mono takes onto a silent canvas as one WAV."""

    def render(
        self,
        *,
        artifacts: Sequence[SynthesisArtifact],
        starts_seconds: Mapping[str, Seconds],
        duration_seconds: Seconds,
        destination: Path,
    ) -> Path:
        import torch
        from julius.resample import resample_frac

        takes = [(item, _read_pcm16(item.path)) for item in artifacts]
        rate = takes[0][1][1] if takes else 22_050
        canvas = torch.zeros(1, max(1, math.ceil(duration_seconds * rate)))
        for artifact, (audio, take_rate) in takes:
            mono = audio.mean(dim=0, keepdim=True)
            if take_rate != rate:
                mono = resample_frac(mono, take_rate, rate)
            offset = max(0, round(starts_seconds[artifact.segment_id] * rate))
            end = min(canvas.shape[1], offset + mono.shape[1])
            if end > offset:
                canvas[:, offset:end] += mono[:, : end - offset]
        destination.parent.mkdir(parents=True, exist_ok=True)
        _write_pcm16(destination, canvas, rate)
        return destination


class FfmpegAudioMixer:
    """``AudioMixer``: dialogue over the accompaniment, encoded as AAC in ``.m4a``.

    The accompaniment is the separated stem without the original dialogue, so
    the original speech is not doubled (up to what the separator leaks).
    """

    def __init__(self, *, ffmpeg_path: str = "ffmpeg") -> None:
        self._ffmpeg = ffmpeg_path

    def mix(self, *, dialogue: Path, accompaniment: Path | None, destination: Path) -> Path:
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.stem}.{uuid.uuid4().hex}.tmp.m4a")
        argv = [self._ffmpeg, "-y", "-v", "error", "-i", str(dialogue)]
        if accompaniment is not None:
            argv += [
                "-i",
                str(accompaniment),
                "-filter_complex",
                "amix=inputs=2:duration=longest:normalize=0",
            ]
        argv += ["-c:a", "aac", "-b:a", "192k", str(temporary)]
        try:
            _run_ffmpeg(argv)
            temporary.replace(destination)
        finally:
            temporary.unlink(missing_ok=True)
        return destination


@dataclass(frozen=True, slots=True)
class DubbingEngines:
    """The injected audio engines; tests pass fakes, the CLI builds local ones."""

    separator: DialogueSeparator
    synthesizer: SpeechSynthesizer
    renderer: TimelineRenderer
    mixer: AudioMixer
    peak_meter: Callable[[Path], float | None] = measure_true_peak_dbtp


def build_dubbing_engines(config: AppConfig) -> DubbingEngines:
    """Local engines over the installed models; a missing model is ``model_missing``."""
    manifest = read_model_manifest(config)

    def identity(kind: ModelKind) -> ModelIdentity:
        found = next((item for item in manifest if item.kind is kind), None)
        if found is None:
            raise NasSubtitlesError(
                f"no {kind} model is installed; run `nas-subs models install`",
                code=ErrorCode.MODEL_MISSING,
            )
        return found

    return DubbingEngines(
        separator=DemucsSeparator(config, model_identity=identity(ModelKind.SEPARATION)),
        synthesizer=PiperSynthesizer(config, model_identity=identity(ModelKind.TTS)),
        renderer=WavTimelineRenderer(),
        mixer=FfmpegAudioMixer(),
    )


def _stage(context: StageContext, job: JobRecord, stage: PipelineStage) -> JobRecord:
    _check_dub_stop(context)
    return context.repository.transition(job_id=job.id, state=JobState.RUNNING, stage=stage)


def _result(
    job: JobRecord,
    stage: PipelineStage,
    *,
    duration: Seconds,
    quality: QualityReport | None = None,
    output_path: Path | None = None,
    cue_count: int = 0,
) -> PipelineResult:
    from .pipeline import PipelineResult

    return PipelineResult(
        job_id=job.id,
        state=job.state,
        last_stage=stage,
        quality=quality or QualityReport(),
        output_path=output_path,
        cue_count=cue_count,
        media_seconds=duration,
    )


def _run_audio_stages(
    context: StageContext,
    *,
    engines: DubbingEngines,
    window: Sequence[PipelineStage],
    job: JobRecord,
    chunks: Sequence[AudioChunk],
    language: str,
    target_language: str,
    fingerprint_digest: str,
    duration: Seconds,
    stream: AudioStreamInfo,
) -> PipelineResult:
    config = context.config
    repo = context.repository
    work = config.work_dir / job.id
    last = window[-1]

    job = _stage(context, job, PipelineStage.SEPARATE)
    separated = []
    for chunk in chunks:
        _check_dub_stop(context)
        separated.append(engines.separator.separate(chunk, destination_dir=work / "separated"))
    if last is PipelineStage.SEPARATE:
        return _result(job, PipelineStage.SEPARATE, duration=duration)

    job = _stage(context, job, PipelineStage.TRANSCRIBE)
    transcripts = []
    for item in separated:
        _check_dub_stop(context)
        dialogue = AudioChunk(spec=item.chunk, path=item.dialogue_path, sha256=item.sha256)
        transcripts.append(context.transcriber.transcribe(dialogue, language=language))
    if last is PipelineStage.TRANSCRIBE:
        return _result(job, PipelineStage.TRANSCRIBE, duration=duration)

    job = _stage(context, job, PipelineStage.MERGE)
    merged = merge_chunk_transcripts(transcripts, duration_seconds=duration)
    if last is PipelineStage.MERGE:
        return _result(job, PipelineStage.MERGE, duration=duration)

    job = _stage(context, job, PipelineStage.TRANSLATE)
    units = build_translation_units(
        config, merged, fingerprint_digest=fingerprint_digest, target_language=target_language
    )
    if not translation_is_required(source_language=language, target_language=target_language):
        translated = tuple(
            TranslatedUnit(
                unit_id=unit.unit_id,
                source_text=unit.source_text,
                translated_text=unit.source_text,
                source_language=unit.source_language,
                target_language=target_language,
                engine_identity="passthrough",
            )
            for unit in units
        )
    elif not context.translator.supports(source_language=language, target_language=target_language):
        raise NasSubtitlesError(
            "no direct translation pair is installed for this language",
            code=ErrorCode.TRANSLATION_PAIR_MISSING,
            detail={"source": language, "target": target_language},
        )
    else:
        translated = translate_with_cache(units, translator=context.translator, repository=repo)
    if last is PipelineStage.TRANSLATE:
        return _result(job, PipelineStage.TRANSLATE, duration=duration)

    job = _stage(context, job, PipelineStage.ADAPT)
    segments = _plan_segments(repo, job, units=units, translated=translated)
    if last is PipelineStage.ADAPT:
        return _result(job, PipelineStage.ADAPT, duration=duration, cue_count=len(segments))

    speakable = tuple(
        segment
        for segment in segments
        if segment.review_state is not DubSegmentReviewState.REJECTED
        and (segment.adapted_text or segment.translated_text).strip()
    )
    job = _stage(context, job, PipelineStage.SYNTHESIZE)
    voice = VoiceAssignment(_SPEAKER_ID, config.dubbing.voice)
    takes: dict[str, SynthesisArtifact] = {}
    for segment in speakable:
        _check_dub_stop(context)
        takes[segment.segment_id] = engines.synthesizer.synthesize(
            segment,
            voice=voice,
            destination=work / "synthesis" / f"{segment.segment_id}-r{segment.revision}.wav",
        )
    if last is PipelineStage.SYNTHESIZE:
        return _result(job, PipelineStage.SYNTHESIZE, duration=duration, cue_count=len(takes))

    job = _stage(context, job, PipelineStage.SYNC)
    flags: list[QualityFlag] = []
    fitted = _fit_takes(config, speakable, takes, work=work / "synced", flags=flags)
    if last is PipelineStage.SYNC:
        return _result(job, PipelineStage.SYNC, duration=duration, cue_count=len(fitted))

    job = _stage(context, job, PipelineStage.MIX)
    starts = {segment.segment_id: segment.start_seconds for segment in speakable}
    dialogue_wav = engines.renderer.render(
        artifacts=[fitted[segment.segment_id] for segment in speakable],
        starts_seconds=starts,
        duration_seconds=duration,
        destination=work / "dialogue.pt-BR.wav",
    )
    accompaniment_wav = _assemble_accompaniment(separated, duration=duration, work=work)
    dubbed = engines.mixer.mix(
        dialogue=dialogue_wav,
        accompaniment=accompaniment_wav,
        destination=work / "dubbed.pt-BR.m4a",
    )
    if last is PipelineStage.MIX:
        return _result(job, PipelineStage.MIX, duration=duration, cue_count=len(fitted))

    job = _stage(context, job, PipelineStage.VALIDATE_AUDIO)
    peak = engines.peak_meter(dubbed)
    if peak is not None and peak > _CLIPPING_THRESHOLD_DBTP:
        flags.append(
            QualityFlag(
                code=QualityFlagCode.CLIPPING_DETECTED,
                message="mixed audio true peak is above the headroom threshold",
                observed=peak,
                threshold=_CLIPPING_THRESHOLD_DBTP,
            )
        )
    report = DubbingQualityReport(
        flags=tuple(flags),
        coverage_complete=len(fitted) == len(speakable),
        peak_dbtp=peak,
    )
    if last is PipelineStage.VALIDATE_AUDIO:
        return _result(job, PipelineStage.VALIDATE_AUDIO, duration=duration, cue_count=len(fitted))

    job = _stage(context, job, PipelineStage.PUBLISH)
    output_dir = _publish_staging(
        config,
        job,
        segments=segments,
        report=report,
        files={"dialogue.pt-BR.wav": dialogue_wav, "dubbed.pt-BR.m4a": dubbed},
        manifest=_dub_manifest(
            job,
            engines=engines,
            stream=stream,
            language=language,
            target_language=target_language,
            duration=duration,
            segment_count=len(segments),
            take_count=len(fitted),
        ),
    )
    repo.record_metrics(
        JobMetrics(
            job_id=job.id,
            media_seconds=duration,
            output_cues=len(fitted),
            quality_flags=report.flags,
        )
    )
    state = JobState.NEEDS_REVIEW if report.requires_review else JobState.READY_TO_PUBLISH
    job = repo.transition(
        job_id=job.id, state=state, stage=PipelineStage.PUBLISH, output_path=output_dir
    )
    log_event(_LOG, "dubbing job staged", job_id=job.id, state=str(job.state))
    return _result(
        job,
        PipelineStage.PUBLISH,
        duration=duration,
        quality=QualityReport(flags=report.flags),
        output_path=output_dir,
        cue_count=len(fitted),
    )


def _plan_segments(
    repo: JobRepository,
    job: JobRecord,
    *,
    units: Sequence[TranslationUnit],
    translated: Sequence[TranslatedUnit],
) -> tuple[DubSegment, ...]:
    """Reuse the stored plan (so manual edits survive) or create revision 1."""
    existing = repo.list_dub_segments(job_id=job.id)
    if existing:
        return existing
    by_id = {unit.unit_id: unit for unit in units}
    segments = tuple(
        DubSegment(
            segment_id=item.unit_id,
            job_id=job.id,
            revision=1,
            start_seconds=by_id[item.unit_id].start_seconds,
            end_seconds=by_id[item.unit_id].end_seconds,
            original_text=item.source_text,
            translated_text=item.translated_text,
            adapted_text=" ".join(item.translated_text.split()),
        )
        for item in translated
        if item.unit_id in by_id
    )
    return repo.replace_dub_plan(job_id=job.id, revision=1, segments=segments)


def _fit_takes(
    config: AppConfig,
    segments: Sequence[DubSegment],
    takes: Mapping[str, SynthesisArtifact],
    *,
    work: Path,
    flags: list[QualityFlag],
) -> dict[str, SynthesisArtifact]:
    """Speed a take up (never slow it down) when it overruns its speech slot.

    The speed-up is capped at ``dubbing.max_speed``; beyond that the take keeps
    the capped speed and the segment is flagged. A take that still reaches into
    the next segment is flagged ``overlap_unresolved`` rather than cut.
    """
    ordered = sorted(segments, key=lambda item: item.start_seconds)
    fitted: dict[str, SynthesisArtifact] = {}
    for segment in ordered:
        take = takes[segment.segment_id]
        slot = max(segment.end_seconds - segment.start_seconds, 0.01)
        if take.duration_seconds > slot:
            needed = take.duration_seconds / slot
            speed = min(needed, config.dubbing.max_speed)
            if needed > config.dubbing.max_speed:
                flags.append(
                    QualityFlag(
                        code=QualityFlagCode.SPEED_LIMIT_EXCEEDED,
                        message=f"segment {segment.segment_id} needs more speed-up than allowed",
                        observed=needed,
                        threshold=config.dubbing.max_speed,
                    )
                )
            take = _speed_up(take, speed=speed, work=work)
        fitted[segment.segment_id] = take
    for current, following in pairwise(ordered):
        end = current.start_seconds + fitted[current.segment_id].duration_seconds
        if end > following.start_seconds:
            flags.append(
                QualityFlag(
                    code=QualityFlagCode.OVERLAP_UNRESOLVED,
                    message=f"segment {current.segment_id} runs into the next segment",
                    observed=end - following.start_seconds,
                    threshold=0.0,
                )
            )
    return fitted


def _speed_up(take: SynthesisArtifact, *, speed: float, work: Path) -> SynthesisArtifact:
    if speed <= 1.0:
        return take
    work.mkdir(parents=True, exist_ok=True)
    destination = work / take.path.name
    temporary = destination.with_name(f".{destination.stem}.{uuid.uuid4().hex}.tmp.wav")
    filters = []
    remaining = speed
    while remaining > 2.0:
        filters.append("atempo=2.0")
        remaining /= 2.0
    filters.append(f"atempo={remaining:.6f}")
    try:
        _run_ffmpeg(
            [
                "ffmpeg",
                "-y",
                "-v",
                "error",
                "-i",
                str(take.path),
                "-filter:a",
                ",".join(filters),
                "-c:a",
                "pcm_s16le",
                str(temporary),
            ]
        )
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    with wave.open(str(destination), "rb") as reader:
        duration = reader.getnframes() / float(reader.getframerate())
    return replace(
        take,
        path=destination,
        duration_seconds=duration,
        sha256=hashlib.sha256(destination.read_bytes()).hexdigest(),
    )


def _assemble_accompaniment(
    separated: Sequence[SeparatedAudio], *, duration: Seconds, work: Path
) -> Path | None:
    """Join the owned part of every accompaniment chunk into one track."""
    import torch

    pieces = [item for item in separated if item.accompaniment_path is not None]
    if not pieces:
        return None
    first, rate = _read_pcm16(pieces[0].accompaniment_path)  # type: ignore[arg-type]
    canvas = torch.zeros(int(first.shape[0]), max(1, math.ceil(duration * rate)))
    for item in pieces:
        audio, item_rate = _read_pcm16(item.accompaniment_path)  # type: ignore[arg-type]
        spec = item.chunk
        begin = max(0, round((spec.owned_start_seconds - spec.extract_start_seconds) * item_rate))
        length = round(spec.owned_duration_seconds * item_rate)
        piece = audio[:, begin : begin + length]
        at = max(0, round(spec.owned_start_seconds * item_rate))
        end = min(canvas.shape[1], at + piece.shape[1])
        if end > at:
            canvas[:, at:end] = piece[:, : end - at]
    destination = work / "accompaniment.pt-BR.wav"
    _write_pcm16(destination, canvas, rate)
    return destination


def _dub_manifest(
    job: JobRecord,
    *,
    engines: DubbingEngines,
    stream: AudioStreamInfo,
    language: str,
    target_language: str,
    duration: Seconds,
    segment_count: int,
    take_count: int,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "job_id": job.id,
        "job_kind": str(JobKind.DUBBING),
        "source_language": language,
        "target_language": target_language,
        "audio_stream_index": stream.index,
        "media_seconds": duration,
        "segments": segment_count,
        "synthesized_segments": take_count,
        "voice": engines.synthesizer.model_identity.name,
        "separator": engines.separator.model_identity.name,
        "synthesizer_identity": engines.synthesizer.model_identity.identity_token(),
        "separator_identity": engines.separator.model_identity.identity_token(),
        "voice_kind": "fixed",
    }


def _publish_staging(
    config: AppConfig,
    job: JobRecord,
    *,
    segments: Sequence[DubSegment],
    report: DubbingQualityReport,
    files: Mapping[str, Path],
    manifest: Mapping[str, object],
) -> Path:
    """Write the staging bundle exclusively; an existing bundle is a conflict."""
    output_dir = config.output_dir / job.root_id / "dubbing" / job.id
    if output_dir.exists() and any(output_dir.iterdir()):
        raise NasSubtitlesError(
            "a dubbing staging bundle already exists for this job; remove it to rerun",
            code=ErrorCode.OUTPUT_CONFLICT,
            detail={"path_token": path_token(output_dir)},
        )
    output_dir.mkdir(parents=True, exist_ok=True)
    for name, source in files.items():
        temporary = output_dir / f".{name}.{uuid.uuid4().hex}.tmp"
        temporary.write_bytes(source.read_bytes())
        try:
            os.link(temporary, output_dir / name)
        except FileExistsError as exc:
            raise NasSubtitlesError(
                f"{name} already exists in the staging bundle", code=ErrorCode.OUTPUT_CONFLICT
            ) from exc
        finally:
            temporary.unlink(missing_ok=True)
    _write_exclusive_json(
        output_dir / "dubbing-plan.json",
        plan_payload(job, segments),
    )
    _write_exclusive_json(output_dir / "manifest.json", manifest)
    _write_exclusive_json(
        output_dir / "quality-report.json",
        {
            "coverage_complete": report.coverage_complete,
            "peak_dbtp": report.peak_dbtp,
            "flags": [
                {
                    "code": str(flag.code),
                    "severity": str(flag.severity),
                    "message": flag.message,
                    "observed": flag.observed,
                    "threshold": flag.threshold,
                }
                for flag in report.flags
            ],
        },
    )
    return output_dir
