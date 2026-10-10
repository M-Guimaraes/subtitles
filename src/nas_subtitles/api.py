"""Application service used by the dashboard (and reusable by the CLI).

The HTTP UI must not contain pipeline logic. Every read or action goes through
this module, which calls the existing repository, discovery, health and
transition helpers. The worker does not import this module, so stopping the
dashboard cannot stop background processing.
"""

from __future__ import annotations

import os
import re
import shutil
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from pydantic import ValidationError

from .config import (
    AppConfig,
    load_config,
    read_runtime_settings,
    write_runtime_media_roots,
)
from .discovery import ScanSummary, scan
from .domain import (
    ArtifactRecord,
    ErrorCode,
    JobEvent,
    JobKind,
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
from .output import manifest_path_for, read_manifest_payload
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
    """``JobRepository`` plus the read helpers the dashboard needs.

    ``list_jobs`` is redeclared here (not in the shared ``JobRepository``
    contract in ``domain.py``) with the Dashboard v2 Fase 1 filters: every
    other caller of the real repository keeps using the narrower signature
    unchanged. See docs/dashboard-v2-audit.md §9.1.
    """

    def require_job(self, job_id: str) -> JobRecord: ...

    def delete_job(self, job_id: str) -> None: ...

    def list_jobs(
        self,
        *,
        state: JobState | None = None,
        states: Sequence[JobState] | None = None,
        search: str | None = None,
        job_kind: JobKind | None = None,
        target_language: str | None = None,
        sort: str = "updated_desc",
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[JobRecord, ...]: ...

    def count_jobs(
        self,
        *,
        state: JobState | None = None,
        states: Sequence[JobState] | None = None,
        search: str | None = None,
        job_kind: JobKind | None = None,
        target_language: str | None = None,
    ) -> int: ...

    def count_by_state(self) -> dict[JobState, int]: ...

    def list_recent_job_events(self, *, limit: int = 10) -> tuple[JobEvent, ...]: ...

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

_OVERVIEW_LIST_LIMIT = 5
"""Dashboard v2 §6.3: active_jobs/attention_jobs cap."""
_OVERVIEW_ACTIVITY_LIMIT = 10
"""Dashboard v2 §6.3: recent_activity cap."""


@dataclass(slots=True)
class DashboardService:
    """Config plus the shared queue. Never takes the worker lock.

    ``config`` is replaced when the media roots are changed from the dashboard;
    ``settings_writable`` is decided by the HTTP layer (loopback or token).
    """

    config: AppConfig
    repository: OperatorRepository
    config_path: Path | None = None
    settings_writable: bool = False

    def overview(self) -> dict[str, object]:
        """Counts by state plus worker liveness, for the queue landing page."""
        counts_by_state = self.repository.count_by_state()
        counts = {str(state): counts_by_state.get(state, 0) for state in JobState}
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

    def dashboard_overview(self) -> dict[str, object]:
        """Dashboard v2 Fase 1 §6: worker health, stat buckets, active/attention
        jobs and recent activity — all from aggregated queries, never a full
        table scan. Buckets are mutually exclusive by construction (§6.2);
        ``skipped``/``cancelled`` jobs are counted in none of them."""
        health = check_health(self.config)
        counts = self.repository.count_by_state()
        stats = {
            "running": counts.get(JobState.RUNNING, 0),
            "waiting": counts.get(JobState.QUEUED, 0) + counts.get(JobState.RETRY_WAIT, 0),
            "completed": counts.get(JobState.COMPLETED, 0),
            "attention": counts.get(JobState.FAILED, 0) + counts.get(JobState.NEEDS_REVIEW, 0),
            "ready_to_publish": counts.get(JobState.READY_TO_PUBLISH, 0),
        }
        active = self.repository.list_jobs(state=JobState.RUNNING, limit=_OVERVIEW_LIST_LIMIT)
        attention = self.repository.list_jobs(
            states=(JobState.FAILED, JobState.NEEDS_REVIEW), limit=_OVERVIEW_LIST_LIMIT
        )
        events = self.repository.list_recent_job_events(limit=_OVERVIEW_ACTIVITY_LIMIT)
        return {
            "ok": True,
            "worker": {"online": health.healthy, "reason": health.reason},
            "stats": stats,
            "active_jobs": [self._job_payload(job) for job in active],
            "attention_jobs": [self._job_payload(job) for job in attention],
            "recent_activity": [self._activity_payload(event) for event in events],
        }

    def list_jobs(
        self,
        *,
        view: JobView = "all",
        states: Sequence[JobState] | None = None,
        search: str | None = None,
        job_kind: JobKind | None = None,
        target_language: str | None = None,
        sort: str = "updated_desc",
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, object]:
        allowed = _states_for_view(view)
        if states is not None:
            invalid = [item for item in states if item not in allowed]
            if invalid:
                raise NasSubtitlesError(
                    f"state {invalid[0]} is not part of the {view} view",
                    code=ErrorCode.INVALID_STATE_TRANSITION,
                    detail={"state": str(invalid[0]), "view": view},
                )
        effective_states = states
        if effective_states is None and view != "all":
            effective_states = tuple(allowed)
        limit = max(limit, 1)
        offset = max(offset, 0)
        records = self.repository.list_jobs(
            states=effective_states,
            search=search,
            job_kind=job_kind,
            target_language=target_language,
            sort=sort,
            limit=limit,
            offset=offset,
        )
        total = self.repository.count_jobs(
            states=effective_states,
            search=search,
            job_kind=job_kind,
            target_language=target_language,
        )
        jobs = [self._job_payload(job) for job in records]
        return {
            "ok": True,
            "view": view,
            "jobs": jobs,
            "pagination": {
                "limit": limit,
                "offset": offset,
                "total": total,
                "has_next": offset + len(jobs) < total,
            },
        }

    def get_job(self, job_id: str) -> dict[str, object]:
        job = self.repository.require_job(job_id)
        artifacts = self.repository.list_artifacts(job_id=job_id)
        metrics = self.repository.get_metrics(job_id)
        events = self.repository.list_events(job_id=job_id, limit=50)
        manifest = read_manifest_payload(self.config, job_id)
        language = _language_from_manifest(manifest) or _language_from_events(events)
        stages = _stage_progress(job)
        return {
            "ok": True,
            "job": self._job_payload(job, language=language, manifest=manifest),
            "stages": stages,
            "progress": _progress_payload(stages),
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

    def delete_jobs(self, ids: object) -> dict[str, object]:
        """Remove finished jobs from the queue database and their scratch files.

        Never touches the library, a published ``.srt`` or a staged output; only
        the job's rows, its manifest and its own directory inside ``work_dir``.
        A running or queued job is skipped: cancel it first.
        """
        if not isinstance(ids, list) or not ids or len(ids) > _DELETE_LIMIT:
            raise ValueError(f"send between 1 and {_DELETE_LIMIT} job ids")
        deleted: list[str] = []
        skipped: list[dict[str, str]] = []
        for raw in ids:
            job_id = raw if isinstance(raw, str) else ""
            try:
                job = self.repository.require_job(job_id)
            except NasSubtitlesError:
                skipped.append({"id": str(raw), "reason": "not_found"})
                continue
            if job.state not in HISTORY_STATES:
                skipped.append({"id": job_id, "reason": "not_finished"})
                continue
            self.repository.delete_job(job_id)
            _remove_job_files(self.config, job_id)
            deleted.append(job_id)
        return {"ok": True, "deleted": deleted, "skipped": skipped}

    def rescan(self) -> dict[str, object]:
        """One library walk. Does not take the worker lock."""
        summary = scan(self.config, self.repository)
        return {"ok": True, "scan": _scan_payload(summary)}

    def update_media_roots(self, paths: object, *, reset: bool = False) -> dict[str, object]:
        """Change the library roots. Saved in ``state_dir``, never in ``config.yaml``.

        The worker keeps the roots it loaded at startup, so it must be
        restarted before it can process files under a new root.
        """
        if not self.settings_writable:
            raise NasSubtitlesError(
                "changing settings needs a loopback bind or a dashboard token",
                code=ErrorCode.PERMISSION_DENIED,
            )
        if reset:
            if self.config_path is None:
                raise NasSubtitlesError(
                    "the config file path is unknown; cannot restore its media roots",
                    code=ErrorCode.CONFIG_INVALID,
                )
            write_runtime_media_roots(self.config.state_dir, None)
            updated = load_config(self.config_path)
        else:
            updated = self._validated_roots(paths)
            blocked = self._active_jobs_on_removed_roots(updated)
            if blocked:
                raise NasSubtitlesError(
                    f"{blocked} unfinished job(s) still use a library you removed; "
                    "finish or cancel them first",
                    code=ErrorCode.CONFIG_INVALID,
                )
            write_runtime_media_roots(self.config.state_dir, updated.media_roots)
        self.config = updated
        return {**self.settings(), "restart_required": True}

    def _validated_roots(self, paths: object) -> AppConfig:
        if not isinstance(paths, list) or not paths:
            raise ValueError("send a non-empty list of library paths")
        normalised: list[Path] = []
        for item in paths:
            text = item.strip() if isinstance(item, str) else ""
            if not text or not Path(text).is_absolute():
                raise ValueError(f"{item!r} must be an absolute path")
            candidate = Path(os.path.normpath(text))
            if not candidate.is_dir():
                raise ValueError(f"{candidate} is not a directory this service can see")
            if not os.access(candidate, os.R_OK | os.X_OK):
                raise ValueError(f"{candidate} is not readable by this service")
            normalised.append(candidate)
        try:
            return AppConfig.model_validate(
                {**self.config.model_dump(mode="python"), "media_roots": tuple(normalised)}
            )
        except ValidationError as exc:
            problems = "; ".join(
                f"{'.'.join(str(part) for part in issue['loc'])}: {issue['msg']}"
                for issue in exc.errors()
            )
            raise ValueError(problems) from exc

    def _active_jobs_on_removed_roots(self, updated: AppConfig) -> int:
        removed = {root.root_id for root in self.config.roots} - {
            root.root_id for root in updated.roots
        }
        if not removed:
            return 0
        jobs = self.repository.list_jobs(states=tuple(QUEUE_STATES), limit=100_000)
        return sum(1 for job in jobs if job.root_id in removed)

    def settings(self) -> dict[str, object]:
        """Safe operational settings. Only the media roots can be changed here."""
        health = check_health(self.config)
        return {
            "ok": True,
            "writable": self.settings_writable,
            "media_roots_overridden": bool(
                read_runtime_settings(self.config.state_dir).get("media_roots")
            ),
            "can_reset_media_roots": self.config_path is not None,
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
            "asr_chunk_seconds": self.config.asr.chunk_seconds,
            "translation_engine": str(self.config.translation.engine),
            "retry": {
                "max_attempts": self.config.worker.max_attempts,
                "delays_seconds": list(self.config.worker.retry_delays_seconds),
            },
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

    def _activity_payload(self, event: JobEvent) -> dict[str, object]:
        """Dashboard v2 §7.5: a recent-activity row. Only ``language_decision``
        exists today (see docs/dashboard-v2-audit.md §4) — ``code`` is passed
        through as-is rather than inventing a friendly label for events that
        do not exist yet."""
        job = self.repository.get_job(event.job_id) if event.job_id else None
        return {
            "job_id": event.job_id,
            "title": Path(job.relative_path).name if job is not None else None,
            "code": event.code,
            "level": str(event.level),
            "created_at": event.created_at.isoformat() if event.created_at else None,
        }


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


_DELETE_LIMIT = 200
_JOB_DIR_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE
)


def _remove_job_files(config: AppConfig, job_id: str) -> None:
    """Delete one job's manifest and its own ``work_dir/<id>``; never by glob."""
    manifest_path_for(config, job_id).unlink(missing_ok=True)
    if not _JOB_DIR_RE.match(job_id):
        return
    directory = config.work_dir / job_id
    if directory.is_dir() and not directory.is_symlink():
        shutil.rmtree(directory, ignore_errors=True)


def available_actions(job: JobRecord) -> list[str]:
    """Actions the operator may take from the current state."""
    actions: list[str] = []
    if job.state is JobState.FAILED and can_transition(job.state, JobState.QUEUED):
        actions.append("retry")
    if job.state is not JobState.CANCELLED and can_transition(job.state, JobState.CANCELLED):
        actions.append("cancel")
    if job.state in _REPROCESS_STATES and can_transition(job.state, JobState.QUEUED):
        actions.append("reprocess")
    if job.state in HISTORY_STATES:
        actions.append("delete")
    return actions


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


def _progress_payload(stages: list[dict[str, object]]) -> dict[str, object]:
    """Dashboard v2 §13.2's ``stage`` mode, built from ``_stage_progress``
    alone: a 1-based position in the pipeline, never a fabricated percent.
    ``measured`` needs the worker to persist a chunk total, which it does
    not do today (docs/dashboard-v2-audit.md §5) — until then this is the
    only honest mode."""
    stage_index = 0
    for position, row in enumerate(stages, start=1):
        if row["status"] in ("current", "done"):
            stage_index = position
    return {
        "mode": "stage",
        "stage_index": stage_index,
        "stage_total": len(stages),
        "stage_percent": None,
        "overall_percent": None,
    }


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
