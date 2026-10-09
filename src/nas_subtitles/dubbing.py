"""Local pt-BR dubbing jobs. Owned by roadmap 006.

Phase 1 persists ``job_kind``, CLI commands and the speech-plan contract.
Synthesis, separation and mix engines arrive in later phases: missing engines
are ``not_implemented``, never a remote API. An existing subtitle sidecar
never prevents a dubbing job.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from .config import AppConfig
from .discovery import compute_fingerprint, is_stable, observe, resolve_explicit_path
from .domain import (
    DUBBING_PLAN_SCHEMA_VERSION,
    DubbingProfile,
    DubSegment,
    DubSegmentReviewState,
    ErrorCode,
    JobKind,
    JobRecord,
    JobRepository,
    JobState,
    MediaProbe,
    NasSubtitlesError,
    PipelineStage,
    QualityReport,
    Seconds,
    infer_job_kind,
    stage_window,
    stages_for,
)
from .logging_setup import log_event, path_token
from .media import FfprobeMediaProbe, ensure_free_space, select_audio_stream

if TYPE_CHECKING:
    from .pipeline import PipelineResult, StageContext

_LOG = logging.getLogger(__name__)

__all__ = [
    "DubEnqueueResult",
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
    """Run the dubbing stage window. Engines after probe arrive in later phases."""

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
    job = repo.transition(job_id=job.id, state=JobState.RUNNING, stage=PipelineStage.PROBE)
    if PipelineStage.PROBE in window:
        context.probe.probe(video)
        if window[-1] is PipelineStage.PROBE:
            return PipelineResult(
                job_id=job.id,
                state=job.state,
                last_stage=PipelineStage.PROBE,
                quality=QualityReport(),
            )
    next_stage = next((stage for stage in window if stage is not PipelineStage.PROBE), None)
    raise NasSubtitlesError(
        "dubbing engines after probe are not implemented yet (roadmap 006)",
        code=ErrorCode.NOT_IMPLEMENTED,
        detail={"stage": str(next_stage) if next_stage else None},
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
