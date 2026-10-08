"""Stage orchestration for a single job. Owned by stages 4 to 7.

Runs the stages in order, reusing any checkpoint whose identity and stage
config hash still match. A stage failure keeps the artifacts of the stages
before it.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, replace
from pathlib import Path
from threading import Event

from .config import AppConfig
from .discovery import compute_fingerprint, find_existing_subtitles, has_portuguese_subtitle
from .domain import (
    ArtifactRecord,
    AudioChunk,
    AudioExtractor,
    AudioStreamInfo,
    ErrorCode,
    EventLevel,
    JobEvent,
    JobManifest,
    JobMetrics,
    JobRecord,
    JobRepository,
    JobState,
    LanguageDecision,
    LanguageSource,
    MediaFingerprint,
    MediaProbe,
    ModelIdentity,
    ModelKind,
    NasSubtitlesError,
    PipelineStage,
    PublishMode,
    QualityReport,
    Seconds,
    SubtitleRenderer,
    Transcriber,
    TranslatedUnit,
    Translator,
)
from .language import (
    decide_source_language,
    effective_source_override,
    is_supported_source,
    translation_is_required,
)
from .logging_setup import log_event
from .media import (
    FfmpegAudioExtractor,
    FfprobeMediaProbe,
    ensure_free_space,
    plan_chunks,
    select_audio_stream,
)
from .models import asr_model_path, read_model_manifest
from .output import (
    SrtSubtitleRenderer,
    preview_path_for,
    publish_job,
    staging_path_for,
    write_manifest,
)
from .quality import evaluate_cues, evaluate_transcript, gate_state, verify_roundtrip
from .segmentation import segment_units_into_cues
from .transcription import (
    FasterWhisperTranscriber,
    chunk_checkpoint_path,
    merge_chunk_transcripts,
    read_chunk_checkpoint,
    write_chunk_checkpoint,
)
from .translation import ArgosTranslator, build_translation_units, translate_with_cache

__all__ = ["PipelineResult", "StageContext", "build_context", "run_job", "run_stage"]

_LOG = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class StageContext:
    """Everything a stage needs, with the engines injected for testability."""

    config: AppConfig
    repository: JobRepository
    job: JobRecord
    probe: MediaProbe
    extractor: AudioExtractor
    transcriber: Transcriber
    translator: Translator
    renderer: SubtitleRenderer
    stop_event: Event | None = None


@dataclass(frozen=True, slots=True)
class PipelineResult:
    job_id: str
    state: JobState
    last_stage: PipelineStage
    quality: QualityReport
    output_path: Path | None = None
    cue_count: int = 0
    media_seconds: Seconds = 0.0


def build_context(
    config: AppConfig, repository: JobRepository, job: JobRecord, *, stop_event: Event | None = None
) -> StageContext:
    """Construct the default local engines for a job."""
    asr_identity = next(
        (item for item in read_model_manifest(config) if item.kind is ModelKind.ASR),
        None,
    )
    translation_identity = next(
        (item for item in read_model_manifest(config) if item.kind is ModelKind.TRANSLATION),
        None,
    )
    transcriber = FasterWhisperTranscriber(config, model_path=asr_model_path(config))
    if asr_identity is not None:
        transcriber._identity = asr_identity
    if translation_identity is None:
        raise NasSubtitlesError(
            "translation model is not installed; run `nas-subs models install`",
            code=ErrorCode.MODEL_MISSING,
        )
    return StageContext(
        config=config,
        repository=repository,
        job=job,
        probe=FfprobeMediaProbe(),
        extractor=FfmpegAudioExtractor(),
        transcriber=transcriber,
        translator=ArgosTranslator(config, model_identity=translation_identity),
        renderer=SrtSubtitleRenderer(),
        stop_event=stop_event,
    )


def run_stage(context: StageContext, stage: PipelineStage) -> StageContext:
    """Execute one stage, reusing a valid checkpoint when one exists."""
    del stage
    return context


def run_job(
    context: StageContext,
    *,
    start_stage: PipelineStage | None = None,
    stop_after: PipelineStage | None = None,
) -> PipelineResult:
    """Run the job from its current stage, honouring cancellation and SIGTERM."""
    del start_stage, stop_after
    config = context.config
    repo = context.repository
    job = context.job
    _check_stop(context)
    ensure_free_space(config, config.work_dir)
    root = config.root_by_id(job.root_id)
    if root is None:
        raise NasSubtitlesError(
            "job root is no longer configured",
            code=ErrorCode.MEDIA_ROOT_MISSING,
        )
    video = root.path / job.relative_path
    job = repo.transition(job_id=job.id, state=JobState.RUNNING, stage=PipelineStage.PROBE)
    probe_result = context.probe.probe(video)
    existing = find_existing_subtitles(path=video, probe_result=probe_result)
    if has_portuguese_subtitle(existing) and job.preview_seconds is None:
        job = repo.transition(job_id=job.id, state=JobState.SKIPPED)
        log_event(
            _LOG,
            "media skipped",
            job_id=job.id,
            reason="existing portuguese subtitle",
        )
        return PipelineResult(
            job_id=job.id, state=job.state, last_stage=PipelineStage.PROBE, quality=QualityReport()
        )
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
        samples = _language_sample_chunks(
            context, video=video, stream_index=stream.index, duration=duration
        )
        asr_decision = context.transcriber.detect_language(samples)
        decision = decide_source_language(
            config,
            stream=stream,
            samples=asr_decision.samples,
            transcriber=context.transcriber,
        )
    _record_language_decision(context, stream=stream, decision=decision)
    if not decision.confident or decision.language is None:
        write_manifest(
            config,
            _language_manifest(
                context,
                fingerprint=fingerprint,
                stream=stream,
                decision=decision,
                source_language=None,
                translation_executed=False,
            ),
        )
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
        write_manifest(
            config,
            _language_manifest(
                context,
                fingerprint=fingerprint,
                stream=stream,
                decision=decision,
                source_language=language,
                translation_executed=False,
            ),
        )
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
    transcripts = []
    stage_hash = config.stage_config_hash(PipelineStage.TRANSCRIBE)
    job = repo.transition(job_id=job.id, state=JobState.RUNNING, stage=PipelineStage.TRANSCRIBE)
    for spec in specs:
        _check_stop(context)
        ensure_free_space(config, work)
        wav = work / f"chunk-{spec.index:04d}.wav"
        chunk = context.extractor.extract(
            source=video, stream_index=stream.index, spec=spec, destination=wav
        )
        chunks.append(chunk)
        checkpoint = chunk_checkpoint_path(config, job_id=job.id, chunk_index=spec.index)
        reused = read_chunk_checkpoint(
            checkpoint,
            job_id=job.id,
            fingerprint=fingerprint,
            stage_config_hash=stage_hash,
            model_identity=context.transcriber.model_identity,
        )
        if reused is None:
            reused = context.transcriber.transcribe(chunk, language=language)
            write_chunk_checkpoint(
                checkpoint,
                reused,
                job_id=job.id,
                fingerprint=fingerprint,
                stage_config_hash=stage_hash,
            )
            repo.record_artifact(
                ArtifactRecord(
                    job_id=job.id,
                    stage=PipelineStage.TRANSCRIBE,
                    path=checkpoint,
                    sha256=_sha256(checkpoint),
                    schema_version=1,
                    stage_config_hash=stage_hash,
                    chunk_index=spec.index,
                )
            )
        transcripts.append(reused)

    job = repo.transition(job_id=job.id, state=JobState.RUNNING, stage=PipelineStage.MERGE)
    merged = merge_chunk_transcripts(transcripts, duration_seconds=duration)
    asr_quality = evaluate_transcript(config, merged)
    if not merged.segments and not merged.words:
        # Silence is allowed: zero cues. Speech with no cues is a failure later.
        pass

    job = repo.transition(job_id=job.id, state=JobState.RUNNING, stage=PipelineStage.TRANSLATE)
    units = build_translation_units(
        config,
        merged,
        fingerprint_digest=fingerprint.digest(),
        target_language=config.target_language,
    )
    skip_translation = not translation_is_required(
        source_language=language, target_language=config.target_language
    )
    if skip_translation:
        translated = tuple(
            TranslatedUnit(
                unit_id=unit.unit_id,
                source_text=unit.source_text,
                translated_text=unit.source_text,
                source_language=unit.source_language,
                target_language=config.target_language,
                engine_identity="passthrough",
            )
            for unit in units
        )
        translation_executed = False
        translation_engine_identity = "passthrough"
        log_event(
            _LOG,
            "translation skipped",
            job_id=job.id,
            source_language=language,
            target_language=config.target_language,
        )
    elif not context.translator.supports(
        source_language=language, target_language=config.target_language
    ):
        raise NasSubtitlesError(
            "no direct translation pair is installed for this language",
            code=ErrorCode.TRANSLATION_PAIR_MISSING,
        )
    else:
        translated = translate_with_cache(units, translator=context.translator, repository=repo)
        translation_executed = True
        translation_engine_identity = context.translator.engine_identity

    job = repo.transition(job_id=job.id, state=JobState.RUNNING, stage=PipelineStage.RENDER)
    cues = segment_units_into_cues(config, translated, source_units=units)
    if not cues and (merged.words or any(segment.text.strip() for segment in merged.segments)):
        raise NasSubtitlesError(
            "recognised speech produced no cues",
            code=ErrorCode.NO_CUES_FOR_SPEECH,
        )
    content = context.renderer.render(cues)

    job = repo.transition(job_id=job.id, state=JobState.RUNNING, stage=PipelineStage.VALIDATE)
    cue_quality = evaluate_cues(config, cues, duration_seconds=duration, units=translated)
    roundtrip = verify_roundtrip(content, cues)
    quality = QualityReport(flags=asr_quality.flags + cue_quality.flags + roundtrip.flags)
    if job.preview_seconds is not None:
        destination = preview_path_for(config, job)
    else:
        destination = staging_path_for(config, job)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(content, encoding="utf-8")
    subtitle_sha = _sha256(destination)
    write_manifest(
        config,
        _language_manifest(
            context,
            fingerprint=fingerprint,
            stream=stream,
            decision=decision,
            source_language=language,
            translation_executed=translation_executed,
            translation_engine_identity=translation_engine_identity,
            quality=quality,
            subtitle_sha256=subtitle_sha,
        ),
    )
    repo.record_metrics(
        JobMetrics(
            job_id=job.id,
            media_seconds=duration,
            output_cues=len(cues),
            quality_flags=quality.flags,
        )
    )
    state = gate_state(quality)
    job = repo.transition(
        job_id=job.id,
        state=state,
        stage=PipelineStage.VALIDATE,
        output_path=destination,
    )
    published = _maybe_publish_sidecar(
        context,
        job=job,
        quality=quality,
        cue_count=len(cues),
        media_seconds=duration,
    )
    if published is not None:
        log_event(_LOG, "job completed", job_id=published.job_id, state=str(published.state))
        return published
    log_event(_LOG, "job completed", job_id=job.id, state=str(job.state))
    return PipelineResult(
        job_id=job.id,
        state=job.state,
        last_stage=PipelineStage.VALIDATE,
        quality=quality,
        output_path=destination,
        cue_count=len(cues),
        media_seconds=duration,
    )


def _maybe_publish_sidecar(
    context: StageContext,
    *,
    job: JobRecord,
    quality: QualityReport,
    cue_count: int,
    media_seconds: Seconds,
) -> PipelineResult | None:
    """Atomically publish next to the video when sidecar mode is on and gates allow it.

    Staging and previews stay in ``output_dir``. Structural errors still block
    publication. Advisory flags do not, so a running daemon can finish without
    a manual ``nas-subs publish``.
    """
    config = context.config
    if job.preview_seconds is not None:
        return None
    if config.publish_mode is not PublishMode.SIDECAR:
        return None
    if quality.blocks_publication:
        return None
    if job.state is JobState.NEEDS_REVIEW:
        job = context.repository.transition(
            job_id=job.id,
            state=JobState.READY_TO_PUBLISH,
            stage=PipelineStage.PUBLISH,
        )
    try:
        result = publish_job(config, context.repository, job)
    except NasSubtitlesError as exc:
        if exc.code is ErrorCode.OUTPUT_CONFLICT:
            updated = context.repository.transition(
                job_id=job.id,
                state=JobState.NEEDS_REVIEW,
                error_code=ErrorCode.OUTPUT_CONFLICT,
                error_detail=exc.message,
            )
            return PipelineResult(
                job_id=updated.id,
                state=updated.state,
                last_stage=PipelineStage.PUBLISH,
                quality=quality,
                output_path=job.output_path,
                cue_count=cue_count,
                media_seconds=media_seconds,
            )
        raise
    refreshed = context.repository.get_job(job.id) or job
    return PipelineResult(
        job_id=refreshed.id,
        state=refreshed.state,
        last_stage=PipelineStage.PUBLISH,
        quality=quality,
        output_path=result.target_path,
        cue_count=cue_count,
        media_seconds=media_seconds,
    )


def _language_manifest(
    context: StageContext,
    *,
    fingerprint: MediaFingerprint,
    stream: AudioStreamInfo,
    decision: LanguageDecision,
    source_language: str | None,
    translation_executed: bool | None = None,
    translation_engine_identity: str | None = None,
    quality: QualityReport | None = None,
    subtitle_sha256: str | None = None,
) -> JobManifest:
    return JobManifest(
        job_id=context.job.id,
        fingerprint=fingerprint,
        pipeline_config_hash=context.config.pipeline_config_hash,
        source_language=source_language,
        target_language=context.config.target_language,
        selected_audio_stream_index=stream.index,
        stream_language=stream.language,
        stream_language_tag=stream.raw_language_tag,
        detected_language=(
            decision.language if decision.source is LanguageSource.DETECTION else None
        ),
        detection_probability=decision.probability,
        source_language_source=decision.source,
        source_language_confident=decision.confident,
        source_language_reason=decision.reason,
        translation_executed=translation_executed,
        translation_engine_identity=translation_engine_identity,
        models=_model_identities(context),
        quality=quality or QualityReport(),
        subtitle_sha256=subtitle_sha256,
    )


def _record_language_decision(
    context: StageContext, *, stream: AudioStreamInfo, decision: LanguageDecision
) -> None:
    payload: dict[str, object] = {
        "selected_audio_stream_index": stream.index,
        "stream_language": stream.language,
        "detected_language": decision.language,
        "detection_probability": decision.probability,
        "source": str(decision.source),
        "confident": decision.confident,
        "target_language": context.config.target_language,
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
        "language decision",
        job_id=context.job.id,
        selected_audio_stream_index=stream.index,
        stream_language=stream.language,
        detected_language=decision.language,
        detection_probability=decision.probability,
        language_source=str(decision.source),
        confident=decision.confident,
        target_language=context.config.target_language,
        reason=decision.reason,
    )


def _model_identities(context: StageContext) -> tuple[ModelIdentity, ...]:
    identities = [context.transcriber.model_identity]
    translation_identity = getattr(context.translator, "_model_identity", None)
    if isinstance(translation_identity, ModelIdentity):
        identities.append(translation_identity)
    return tuple(identities)


def _language_sample_chunks(
    context: StageContext, *, video: Path, stream_index: int, duration: Seconds
) -> tuple[AudioChunk, ...]:
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


def _check_stop(context: StageContext) -> None:
    if context.stop_event is not None and context.stop_event.is_set():
        raise NasSubtitlesError("interrupted by signal", code=ErrorCode.INTERRUPTED)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()
