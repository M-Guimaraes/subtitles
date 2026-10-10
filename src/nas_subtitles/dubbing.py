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
import os
import uuid
import wave
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .config import AppConfig
from .discovery import compute_fingerprint, is_stable, observe, resolve_explicit_path
from .domain import (
    DUBBING_PLAN_SCHEMA_VERSION,
    AudioChunk,
    AudioStreamInfo,
    DubbingProfile,
    DubSegment,
    DubSegmentReviewState,
    ErrorCode,
    EventLevel,
    JobEvent,
    JobKind,
    JobRecord,
    JobRepository,
    JobState,
    LanguageDecision,
    MediaProbe,
    ModelIdentity,
    NasSubtitlesError,
    PipelineStage,
    QualityReport,
    Seconds,
    SeparatedAudio,
    SynthesisArtifact,
    VoiceAssignment,
    infer_job_kind,
    stage_window,
    stages_for,
)
from .language import decide_source_language, effective_source_override, is_supported_source
from .logging_setup import log_event, path_token
from .media import FfprobeMediaProbe, ensure_free_space, plan_chunks, select_audio_stream
from .models import load_separation_model, load_tts_voice

if TYPE_CHECKING:
    from .pipeline import PipelineResult, StageContext

_LOG = logging.getLogger(__name__)

__all__ = [
    "DemucsSeparator",
    "DubEnqueueResult",
    "PiperSynthesizer",
    "apply_plan",
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
) -> PipelineResult:
    """Run the dubbing stage window.

    ``probe``, ``detect_language`` and ``extract`` are real: they reuse the
    same probe/transcriber/extractor the subtitle pipeline uses (never its
    private helpers — this stays inside roadmap-006 ownership) to pick the
    audio stream, vote on the source language and cut it into chunks.
    Separation, synthesis, sync, mix and publish are still unimplemented
    Protocol stubs (domain.py), so anything past ``extract`` is
    ``not_implemented``. Resuming from a stage other than ``probe`` is not
    supported yet (no checkpoint is written here); see
    docs/roadmap/006-dubbing-completion-plan.md fase 4.
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
    for spec in specs:
        _check_dub_stop(context)
        ensure_free_space(config, work)
        wav = work / f"chunk-{spec.index:04d}.wav"
        context.extractor.extract(
            source=video, stream_index=stream.index, spec=spec, destination=wav
        )
    if window[-1] is PipelineStage.EXTRACT:
        return PipelineResult(
            job_id=job.id,
            state=job.state,
            last_stage=PipelineStage.EXTRACT,
            quality=QualityReport(),
            media_seconds=duration,
        )

    excluded = (PipelineStage.PROBE, PipelineStage.DETECT_LANGUAGE, PipelineStage.EXTRACT)
    next_stage = next((stage for stage in window if stage not in excluded), None)
    raise NasSubtitlesError(
        "dubbing engines after extract are not implemented yet (roadmap 006)",
        code=ErrorCode.NOT_IMPLEMENTED,
        detail={"stage": str(next_stage) if next_stage else None},
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
