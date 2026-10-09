"""SRT rendering, staging layout and exclusive publication. Owned by stage 6.

Publication writes a uniquely named temporary file in the *target* directory,
verifies and fsyncs it, then creates the final name with ``os.link``. An
existing name is never overwritten: ``EEXIST`` becomes ``output_conflict``,
and a filesystem without hard links becomes ``unsupported_atomic_publish``.
"""

from __future__ import annotations

import contextlib
import errno
import json
import logging
import os
import uuid
from collections.abc import Sequence
from datetime import timedelta
from pathlib import Path

import srt

from .config import AppConfig, MediaRoot
from .domain import (
    ErrorCode,
    JobManifest,
    JobRecord,
    JobRepository,
    JobState,
    NasSubtitlesError,
    PublishMode,
    PublishOutcome,
    PublishResult,
    SubtitleCue,
    canonical_json,
)
from .logging_setup import log_event, path_token

__all__ = [
    "PREVIEW_MARKER",
    "SrtSubtitleRenderer",
    "manifest_path_for",
    "preview_path_for",
    "publish_exclusive",
    "publish_job",
    "read_manifest_payload",
    "sidecar_path_for",
    "staging_path_for",
    "supports_atomic_publish",
    "write_manifest",
]

PREVIEW_MARKER = ".preview"
"""A preview is written to staging only and can never become a sidecar."""

_LOG = logging.getLogger(__name__)
_SRT_MODE = 0o644


class SrtSubtitleRenderer:
    """``SubtitleRenderer`` implementation over the ``srt`` library."""

    def render(self, cues: Sequence[SubtitleCue]) -> str:
        """UTF-8 SRT, indices from 1, ``HH:MM:SS,mmm`` timestamps."""
        subtitles = [
            srt.Subtitle(
                index=cue.index,
                start=timedelta(seconds=max(cue.start_seconds, 0.0)),
                end=timedelta(seconds=max(cue.end_seconds, cue.start_seconds)),
                content=cue.text,
            )
            for cue in cues
        ]
        composed: str = srt.compose(subtitles)
        return composed

    def parse(self, content: str) -> tuple[SubtitleCue, ...]:
        parsed = tuple(srt.parse(content))
        cues: list[SubtitleCue] = []
        for item in parsed:
            lines = tuple(item.content.splitlines()) or (item.content,)
            cues.append(
                SubtitleCue(
                    index=int(item.index),
                    start_seconds=item.start.total_seconds(),
                    end_seconds=item.end.total_seconds(),
                    lines=lines,
                )
            )
        return tuple(cues)


def staging_path_for(config: AppConfig, job: JobRecord) -> Path:
    """``output_dir/<root_id>/<relative tree>/<stem>.<target>.srt``."""
    relative = Path(job.relative_path)
    target = config.target_language_for_job(job.target_language)
    return config.output_dir / job.root_id / relative.with_suffix(f".{target}.srt")


def sidecar_path_for(
    config: AppConfig,
    root: MediaRoot,
    relative_path: str,
    *,
    target_language: str | None = None,
) -> Path:
    """``<stem>.<target>.srt`` beside the video; requires ``publish_mode: sidecar``.

    Uses the public target language, never an Argos backend code such as ``pb``.
    """
    video = root.path / relative_path
    target = config.target_language_for_job(target_language)
    return video.with_suffix(f".{target}.srt")


def preview_path_for(config: AppConfig, job: JobRecord) -> Path:
    """Staging path carrying :data:`PREVIEW_MARKER` in the name."""
    staged = staging_path_for(config, job)
    return staged.with_name(f"{staged.stem}{PREVIEW_MARKER}{staged.suffix}")


def supports_atomic_publish(directory: Path) -> bool:
    """Probe hard-link support with the caller's own temporary file."""
    directory.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex
    source = directory / f".nas-subs-link-probe-{token}.a"
    target = directory / f".nas-subs-link-probe-{token}.b"
    try:
        source.write_text("probe\n", encoding="utf-8")
        os.link(source, target)
    except OSError:
        return False
    finally:
        for path in (source, target):
            with contextlib.suppress(OSError):
                path.unlink()
    return True


def publish_exclusive(*, content: str, target: Path) -> PublishResult:
    """Create ``target`` exclusively; never overwrite, never partially write."""
    target.parent.mkdir(parents=True, exist_ok=True)
    if not supports_atomic_publish(target.parent):
        return PublishResult(
            outcome=PublishOutcome.UNSUPPORTED,
            target_path=target,
            message="filesystem does not support exclusive hard-link publish",
        )
    token = uuid.uuid4().hex
    temporary = target.parent / f".{target.name}.{token}.tmp"
    encoded = content.encode("utf-8")
    try:
        with temporary.open("wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(_SRT_MODE)
        os.link(temporary, target)
    except FileExistsError:
        conflict = target.parent / f"{target.name}.conflict-{token}"
        try:
            os.link(temporary, conflict)
        except OSError:
            conflict.write_bytes(encoded)
        return PublishResult(
            outcome=PublishOutcome.CONFLICT,
            target_path=target,
            sha256=_sha256_bytes(encoded),
            conflict_path=conflict,
            message="target already exists; both copies were kept",
        )
    except OSError as exc:
        if exc.errno in {errno.EEXIST}:
            conflict = target.parent / f"{target.name}.conflict-{token}"
            conflict.write_bytes(encoded)
            return PublishResult(
                outcome=PublishOutcome.CONFLICT,
                target_path=target,
                sha256=_sha256_bytes(encoded),
                conflict_path=conflict,
                message="target already exists; both copies were kept",
            )
        unsupported = {errno.EXDEV, errno.EPERM, errno.ENOTSUP}
        if hasattr(errno, "EOPNOTSUPP"):
            unsupported.add(errno.EOPNOTSUPP)
        if exc.errno in unsupported:
            raise NasSubtitlesError(
                "exclusive hard-link publish is not supported here",
                code=ErrorCode.UNSUPPORTED_ATOMIC_PUBLISH,
                detail={"path_token": path_token(target)},
            ) from exc
        raise NasSubtitlesError(
            "failed to publish subtitle",
            code=ErrorCode.IO_ERROR,
            detail={"path_token": path_token(target)},
        ) from exc
    finally:
        with contextlib.suppress(OSError):
            temporary.unlink()
    log_event(
        _LOG,
        "sidecar published",
        path_token=path_token(target),
        target_name=target.name,
    )
    return PublishResult(
        outcome=PublishOutcome.PUBLISHED,
        target_path=target,
        sha256=_sha256_bytes(encoded),
        message="published",
    )


def publish_job(config: AppConfig, repository: JobRepository, job: JobRecord) -> PublishResult:
    """Publish a ready job without re-running ASR or translation."""
    from .discovery import compute_fingerprint, find_existing_subtitles, has_subtitle_for_target
    from .media import FfprobeMediaProbe
    from .states import ensure_transition

    ensure_transition(job.state, JobState.COMPLETED)

    if job.preview_seconds is not None:
        raise NasSubtitlesError(
            "a preview cannot be published as a library sidecar",
            code=ErrorCode.OUTPUT_CONFLICT,
        )
    root = config.root_by_id(job.root_id)
    if root is None:
        raise NasSubtitlesError(
            "job root is no longer configured",
            code=ErrorCode.MEDIA_ROOT_MISSING,
        )
    video = root.path / job.relative_path
    current = compute_fingerprint(
        root=root, path=video, audio_stream_index=job.fingerprint.audio_stream_index
    )
    if not current.content_matches(job.fingerprint):
        raise NasSubtitlesError(
            "media changed since the job was created",
            code=ErrorCode.MEDIA_CHANGED,
        )
    probe_result = FfprobeMediaProbe().probe(video)
    existing = find_existing_subtitles(path=video, probe_result=probe_result)
    source = preview_path_for(config, job) if job.preview_seconds else staging_path_for(config, job)
    if not source.is_file():
        raise NasSubtitlesError(
            "staged subtitle is missing",
            code=ErrorCode.IO_ERROR,
            detail={"path_token": path_token(source)},
        )
    content = source.read_text(encoding="utf-8")
    target_language = config.target_language_for_job(job.target_language)
    if config.publish_mode is PublishMode.SIDECAR:
        if has_subtitle_for_target(existing, target_language):
            raise NasSubtitlesError(
                f"a {target_language} subtitle already exists beside the video",
                code=ErrorCode.OUTPUT_CONFLICT,
            )
        target = sidecar_path_for(
            config, root, job.relative_path, target_language=target_language
        )
        result = publish_exclusive(content=content, target=target)
    else:
        result = PublishResult(
            outcome=PublishOutcome.PUBLISHED,
            target_path=source,
            sha256=_sha256_bytes(content.encode("utf-8")),
            message="already in staging",
        )
    if result.outcome is PublishOutcome.CONFLICT:
        repository.transition(
            job_id=job.id,
            state=JobState.NEEDS_REVIEW,
            error_code=ErrorCode.OUTPUT_CONFLICT,
            error_detail=result.message,
        )
        return result
    if result.outcome is PublishOutcome.UNSUPPORTED:
        raise NasSubtitlesError(result.message, code=ErrorCode.UNSUPPORTED_ATOMIC_PUBLISH)
    repository.transition(
        job_id=job.id,
        state=JobState.COMPLETED,
        output_path=result.target_path,
    )
    return result


def manifest_path_for(config: AppConfig, job_id: str) -> Path:
    """Manifests live under ``state_dir``, never beside the video."""
    return config.manifests_dir / f"{job_id}.json"


def read_manifest_payload(config: AppConfig, job_id: str) -> dict[str, object] | None:
    """Load a job manifest as JSON, or ``None`` when it has not been written."""
    path = manifest_path_for(config, job_id)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def write_manifest(config: AppConfig, manifest: JobManifest) -> Path:
    """Write the manifest under ``state_dir``, never beside the video."""
    config.manifests_dir.mkdir(parents=True, exist_ok=True)
    path = manifest_path_for(config, manifest.job_id)
    payload: dict[str, object] = {
        "schema_version": manifest.schema_version,
        "job_id": manifest.job_id,
        "fingerprint": {
            "root_id": manifest.fingerprint.root_id,
            "relative_path": manifest.fingerprint.relative_path,
            "size_bytes": manifest.fingerprint.size_bytes,
            "mtime_ns": manifest.fingerprint.mtime_ns,
            "head_sha256": manifest.fingerprint.head_sha256,
            "tail_sha256": manifest.fingerprint.tail_sha256,
            "audio_stream_index": manifest.fingerprint.audio_stream_index,
        },
        "pipeline_config_hash": manifest.pipeline_config_hash,
        "source_language": manifest.source_language,
        "target_language": manifest.target_language,
        "selected_audio_stream_index": manifest.selected_audio_stream_index,
        "stream_language": manifest.stream_language,
        "stream_language_tag": manifest.stream_language_tag,
        "detected_language": manifest.detected_language,
        "detection_probability": manifest.detection_probability,
        "source_language_source": (
            str(manifest.source_language_source) if manifest.source_language_source else None
        ),
        "source_language_confident": manifest.source_language_confident,
        "source_language_reason": manifest.source_language_reason,
        "translation_executed": manifest.translation_executed,
        "translation_engine_identity": manifest.translation_engine_identity,
        "models": [
            {
                "kind": str(model.kind),
                "name": model.name,
                "version": model.version,
                "identity": model.identity_token(),
            }
            for model in manifest.models
        ],
        "subtitle_sha256": manifest.subtitle_sha256,
        "quality_flags": [str(flag.code) for flag in manifest.quality.flags],
    }
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(canonical_json(payload) + "\n", encoding="utf-8")
    temporary.replace(path)
    return path


def _sha256_bytes(payload: bytes) -> str:
    import hashlib

    return hashlib.sha256(payload).hexdigest()
