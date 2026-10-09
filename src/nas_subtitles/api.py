"""Application service used by the dashboard (and reusable by the CLI).

The HTTP UI must not contain pipeline logic. Every read or action goes through
this module, which calls the existing repository, discovery, health and
transition helpers. The worker does not import this module, so stopping the
dashboard cannot stop background processing.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from .config import AppConfig
from .discovery import ScanSummary, scan
from .domain import (
    ArtifactRecord,
    ErrorCode,
    JobEvent,
    JobMetrics,
    JobRecord,
    JobRepository,
    JobState,
    NasSubtitlesError,
    PipelineStage,
    infer_job_kind,
    stages_for,
)
from .health import HealthReport, check_health
from .output import read_manifest_payload
from .states import can_transition

__all__ = [
    "HISTORY_STATES",
    "QUEUE_STATES",
    "DashboardService",
    "JobView",
    "OperatorRepository",
    "available_actions",
    "job_summary",
]

JobView = Literal["queue", "history", "all"]

QUEUE_STATES: frozenset[JobState] = frozenset(
    {
        JobState.QUEUED,
        JobState.RUNNING,
        JobState.RETRY_WAIT,
        JobState.NEEDS_REVIEW,
        JobState.READY_TO_PUBLISH,
    }
)
HISTORY_STATES: frozenset[JobState] = frozenset(
    {
        JobState.COMPLETED,
        JobState.SKIPPED,
        JobState.FAILED,
        JobState.CANCELLED,
    }
)


class OperatorRepository(JobRepository, Protocol):
    """``JobRepository`` plus the read helpers the dashboard needs."""

    def require_job(self, job_id: str) -> JobRecord: ...

    def list_artifacts(
        self, *, job_id: str, stage: PipelineStage | None = None
    ) -> tuple[ArtifactRecord, ...]: ...

    def list_events(
        self, *, job_id: str | None = None, limit: int = 100
    ) -> tuple[JobEvent, ...]: ...

    def get_metrics(self, job_id: str) -> JobMetrics | None: ...


_REPROCESS_STATES: frozenset[JobState] = frozenset(
    {
        JobState.COMPLETED,
        JobState.SKIPPED,
        JobState.FAILED,
        JobState.CANCELLED,
    }
)


@dataclass(frozen=True, slots=True)
class DashboardService:
    """Read-only config plus the shared queue. Never takes the worker lock."""

    config: AppConfig
    repository: OperatorRepository

    def overview(self) -> dict[str, object]:
        """Counts by state plus worker liveness, for the queue landing page."""
        jobs = self.repository.list_jobs(limit=10_000)
        counts = {str(state): 0 for state in JobState}
        for job in jobs:
            counts[str(job.state)] += 1
        health = check_health(self.config)
        return {
            "ok": True,
            "counts": counts,
            "queue_count": sum(counts[str(state)] for state in QUEUE_STATES),
            "history_count": sum(counts[str(state)] for state in HISTORY_STATES),
            "worker": _health_payload(health),
            "target_language": self.config.target_language,
            "target_languages": list(self.config.target_languages),
        }

    def list_jobs(
        self,
        *,
        view: JobView = "all",
        state: JobState | None = None,
        limit: int = 100,
    ) -> dict[str, object]:
        allowed = _states_for_view(view)
        if state is not None and state not in allowed:
            raise NasSubtitlesError(
                f"state {state} is not part of the {view} view",
                code=ErrorCode.INVALID_STATE_TRANSITION,
                detail={"state": str(state), "view": view},
            )
        records = _collect_jobs(self.repository, view=view, state=state, limit=max(limit, 1))
        return {
            "ok": True,
            "view": view,
            "jobs": [self._job_payload(job) for job in records],
        }

    def get_job(self, job_id: str) -> dict[str, object]:
        job = self.repository.require_job(job_id)
        artifacts = self.repository.list_artifacts(job_id=job_id)
        metrics = self.repository.get_metrics(job_id)
        events = self.repository.list_events(job_id=job_id, limit=50)
        manifest = read_manifest_payload(self.config, job_id)
        language = _language_from_manifest(manifest) or _language_from_events(events)
        return {
            "ok": True,
            "job": self._job_payload(job, language=language, manifest=manifest),
            "stages": _stage_progress(job),
            "language": language,
            "models": _models_from_manifest(manifest),
            "artifacts": [
                {
                    "stage": str(item.stage),
                    "chunk_index": item.chunk_index,
                    "sha256": item.sha256,
                }
                for item in artifacts
            ],
            "metrics": None
            if metrics is None
            else {
                "output_cues": metrics.output_cues,
                "total_seconds": metrics.total_seconds,
                "media_seconds": metrics.media_seconds,
                "quality_flags": [str(flag.code) for flag in metrics.quality_flags],
            },
            "events": [_event_payload(event) for event in events],
            "actions": available_actions(job),
        }

    def retry_job(self, job_id: str) -> dict[str, object]:
        job = self.repository.require_job(job_id)
        if job.state is not JobState.FAILED:
            raise NasSubtitlesError(
                f"only a failed job can be retried; {job_id} is {job.state}",
                code=ErrorCode.INVALID_STATE_TRANSITION,
                detail={"from": str(job.state), "to": str(JobState.QUEUED)},
            )
        updated = self.repository.transition(job_id=job_id, state=JobState.QUEUED)
        return {"ok": True, "job": self._job_payload(updated), "action": "retry"}

    def cancel_job(self, job_id: str) -> dict[str, object]:
        job = self.repository.require_job(job_id)
        if not can_transition(job.state, JobState.CANCELLED) or job.state is JobState.CANCELLED:
            raise NasSubtitlesError(
                f"cannot cancel a job in state {job.state}",
                code=ErrorCode.INVALID_STATE_TRANSITION,
                detail={"from": str(job.state), "to": str(JobState.CANCELLED)},
            )
        updated = self.repository.transition(job_id=job_id, state=JobState.CANCELLED)
        return {"ok": True, "job": self._job_payload(updated), "action": "cancel"}

    def reprocess_job(self, job_id: str) -> dict[str, object]:
        """Operator request to run the same job again, keeping checkpoints."""
        job = self.repository.require_job(job_id)
        if job.state not in _REPROCESS_STATES:
            raise NasSubtitlesError(
                f"cannot reprocess a job in state {job.state}",
                code=ErrorCode.INVALID_STATE_TRANSITION,
                detail={"from": str(job.state), "to": str(JobState.QUEUED)},
            )
        updated = self.repository.transition(job_id=job_id, state=JobState.QUEUED)
        return {"ok": True, "job": self._job_payload(updated), "action": "reprocess"}

    def rescan(self) -> dict[str, object]:
        """One library walk. Does not take the worker lock."""
        summary = scan(self.config, self.repository)
        return {"ok": True, "scan": _scan_payload(summary)}

    def settings(self) -> dict[str, object]:
        """Safe operational settings. The dashboard does not write config.yaml."""
        health = check_health(self.config)
        return {
            "ok": True,
            "writable": False,
            "automatic_processing": health.healthy,
            "worker": _health_payload(health),
            "media_roots": [
                {"root_id": root.root_id, "path": str(root.path)} for root in self.config.roots
            ],
            "languages": {
                "source": self.config.languages.source,
                "target": self.config.target_language,
                "targets": list(self.config.target_languages),
                "low_confidence": self.config.languages.low_confidence,
            },
            "audio": {
                "stream": self.config.audio.stream,
                "preferred_languages": list(self.config.audio.preferred_languages),
            },
            "scan_interval_seconds": self.config.scan_interval_seconds,
            "existing_subtitle_policy": str(self.config.existing_subtitle_policy),
            "publish_mode": str(self.config.publish_mode),
            "asr_model": self.config.asr.model,
            "dashboard": {
                "bind": self.config.dashboard.bind,
                "port": self.config.dashboard.port,
                "token_configured": bool(self.config.dashboard.token),
            },
            "webhooks": {
                "bind": self.config.webhooks.bind,
                "port": self.config.webhooks.port,
                "token_configured": bool(self.config.webhooks.token),
                "path_maps": [
                    {
                        "host_prefix": str(item.host_prefix),
                        "container_prefix": str(item.container_prefix),
                    }
                    for item in self.config.webhooks.path_maps
                ],
            },
        }

    def _job_payload(
        self,
        job: JobRecord,
        *,
        language: Mapping[str, object] | None = None,
        manifest: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        if language is None:
            stored = read_manifest_payload(self.config, job.id) if manifest is None else manifest
            language = _language_from_manifest(stored)
        return job_summary(job, config=self.config, language=language)


def job_summary(
    job: JobRecord,
    *,
    config: AppConfig,
    language: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Queue/history row: title, state, stage, languages, times, error."""
    resolved = dict(language or {})
    source = resolved.get("source_language") or job.source_language_override
    detected = resolved.get("detected_language")
    probability = resolved.get("detection_probability")
    target = resolved.get("target_language") or job.target_language or config.target_language
    return {
        "id": job.id,
        "title": Path(job.relative_path).name,
        "root_id": job.root_id,
        "relative_path": job.relative_path,
        "state": str(job.state),
        "current_stage": str(job.current_stage) if job.current_stage else None,
        "attempt_count": job.attempt_count,
        "priority": job.priority,
        "error_code": str(job.error_code) if job.error_code else None,
        "error_detail": job.error_detail,
        "output_path": str(job.output_path) if job.output_path else None,
        "created_at": job.created_at.isoformat(),
        "updated_at": job.updated_at.isoformat(),
        "approved_at": job.approved_at.isoformat() if job.approved_at else None,
        "execution_scope": str(job.execution_scope) if job.execution_scope else None,
        "source_language": source,
        "detected_language": detected,
        "detection_probability": probability,
        "source_language_confident": resolved.get("source_language_confident"),
        "target_language": target,
        "job_kind": str(infer_job_kind(job.job_kind)),
        "dubbing_profile": job.dubbing_profile,
        "selected_audio_stream_index": resolved.get("selected_audio_stream_index")
        or job.audio_stream_index_override
        or job.fingerprint.audio_stream_index,
        "actions": available_actions(job),
    }


def available_actions(job: JobRecord) -> list[str]:
    """Actions the operator may take from the current state."""
    actions: list[str] = []
    if job.state is JobState.FAILED and can_transition(job.state, JobState.QUEUED):
        actions.append("retry")
    if job.state is not JobState.CANCELLED and can_transition(job.state, JobState.CANCELLED):
        actions.append("cancel")
    if job.state in _REPROCESS_STATES and can_transition(job.state, JobState.QUEUED):
        actions.append("reprocess")
    return actions


def _collect_jobs(
    repository: OperatorRepository,
    *,
    view: JobView,
    state: JobState | None,
    limit: int,
) -> tuple[JobRecord, ...]:
    if state is not None:
        return repository.list_jobs(state=state, limit=limit)
    if view == "all":
        return repository.list_jobs(limit=limit)
    collected: list[JobRecord] = []
    for item in _states_for_view(view):
        collected.extend(repository.list_jobs(state=item, limit=limit))
    collected.sort(key=lambda job: job.updated_at, reverse=True)
    return tuple(collected[:limit])


def _states_for_view(view: JobView) -> frozenset[JobState]:
    if view == "queue":
        return QUEUE_STATES
    if view == "history":
        return HISTORY_STATES
    return frozenset(JobState)


def _stage_progress(job: JobRecord) -> list[dict[str, object]]:
    current = job.current_stage
    reached = False
    rows: list[dict[str, object]] = []
    for stage in stages_for(job.job_kind):
        status: Literal["pending", "current", "done"]
        if current is None:
            status = "pending"
        elif stage is current:
            status = "current"
            reached = True
        elif not reached:
            status = "done"
        else:
            status = "pending"
        rows.append({"stage": str(stage), "status": status})
    if current is None and job.state is JobState.QUEUED:
        return rows
    if job.state in {JobState.COMPLETED, JobState.SKIPPED} and current is PipelineStage.PUBLISH:
        for row in rows:
            if row["status"] == "current":
                row["status"] = "done"
    return rows


def _language_from_manifest(manifest: Mapping[str, object] | None) -> dict[str, object] | None:
    if manifest is None:
        return None
    return {
        "selected_audio_stream_index": manifest.get("selected_audio_stream_index"),
        "stream_language": manifest.get("stream_language"),
        "stream_language_tag": manifest.get("stream_language_tag"),
        "detected_language": manifest.get("detected_language"),
        "detection_probability": manifest.get("detection_probability"),
        "source_language": manifest.get("source_language"),
        "source_language_source": manifest.get("source_language_source"),
        "source_language_confident": manifest.get("source_language_confident"),
        "source_language_reason": manifest.get("source_language_reason"),
        "target_language": manifest.get("target_language"),
        "translation_executed": manifest.get("translation_executed"),
    }


def _language_from_events(events: tuple[JobEvent, ...]) -> dict[str, object] | None:
    for event in events:
        if event.code != "language_decision":
            continue
        payload = event.payload
        return {
            "selected_audio_stream_index": payload.get("selected_audio_stream_index"),
            "stream_language": payload.get("stream_language"),
            "detected_language": payload.get("detected_language"),
            "detection_probability": payload.get("detection_probability"),
            "source_language": payload.get("detected_language"),
            "source_language_source": payload.get("source"),
            "source_language_confident": payload.get("confident"),
            "source_language_reason": payload.get("reason"),
            "target_language": payload.get("target_language"),
            "translation_executed": None,
        }
    return None


def _models_from_manifest(manifest: Mapping[str, object] | None) -> list[dict[str, object]]:
    if manifest is None:
        return []
    raw = manifest.get("models")
    if not isinstance(raw, list):
        return []
    models: list[dict[str, object]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        models.append(
            {
                "kind": item.get("kind"),
                "name": item.get("name"),
                "version": item.get("version"),
                "identity": item.get("identity"),
            }
        )
    return models


def _event_payload(event: JobEvent) -> dict[str, object]:
    return {
        "code": event.code,
        "level": str(event.level),
        "created_at": event.created_at.isoformat() if event.created_at else None,
        "payload": dict(event.payload),
    }


def _health_payload(report: HealthReport) -> dict[str, object]:
    return {
        "healthy": report.healthy,
        "reason": report.reason,
        "database_reachable": report.database_reachable,
        "heartbeat_age_seconds": report.heartbeat_age_seconds,
    }


def _scan_payload(summary: ScanSummary) -> dict[str, object]:
    return {
        "examined": summary.examined,
        "enqueued": summary.enqueued,
        "skipped_unstable": summary.skipped_unstable,
        "skipped_too_young": summary.skipped_too_young,
        "skipped_existing_subtitle": summary.skipped_existing_subtitle,
        "skipped_unreadable": summary.skipped_unreadable,
        "already_queued": summary.already_queued,
        "skipped_temporary": summary.skipped_temporary,
        "skipped_unsupported": summary.skipped_unsupported,
    }
