"""Library scanning, stability and existing-subtitle detection.

Owned by stages 3 and 7 of the plan. The scanner never follows symlinks,
never creates a missing media root and never writes inside the library.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .config import AppConfig, MediaRoot
from .domain import (
    FINGERPRINT_SAMPLE_BYTES,
    LIBRARY_SCAN_KNOWN_STATES,
    PORTUGUESE,
    PORTUGUESE_SUBTITLE_SUFFIXES,
    VIDEO_EXTENSIONS,
    ErrorCode,
    ExistingSubtitle,
    ExistingSubtitlePolicy,
    JobRecord,
    JobRepository,
    JobState,
    MediaFingerprint,
    NasSubtitlesError,
    ProbeResult,
    ScanObservation,
    SubtitleOrigin,
)
from .language import subtitle_suffix_language
from .logging_setup import log_event, path_token
from .media import FfprobeMediaProbe, select_audio_stream

__all__ = [
    "ScanSummary",
    "canonical_target_sidecar",
    "compute_fingerprint",
    "enqueue_path",
    "find_existing_subtitles",
    "has_portuguese_subtitle",
    "has_target_sidecar",
    "is_candidate_name",
    "is_stable",
    "is_temporary_name",
    "iter_candidate_files",
    "observe",
    "resolve_explicit_path",
    "scan",
]

_LOG = logging.getLogger(__name__)
_SKIP_DIR_NAMES = frozenset({"download", "incomplete", "downloads"})
_TEMPORARY_SUFFIXES = frozenset({".part", ".partial", ".tmp", ".temp", ".crdownload", ".!qb"})
_SUBTITLE_EXTENSIONS = frozenset({".srt", ".vtt", ".ass"})
_TOKEN_SPLIT = re.compile(r"[^a-z0-9]+")


@dataclass(frozen=True, slots=True)
class ScanSummary:
    """Outcome of one scan pass, also the payload of ``scan --dry-run``."""

    examined: int = 0
    enqueued: int = 0
    skipped_unstable: int = 0
    skipped_too_young: int = 0
    skipped_existing_subtitle: int = 0
    skipped_unreadable: int = 0
    already_queued: int = 0
    skipped_temporary: int = 0
    skipped_unsupported: int = 0
    enqueued_paths: tuple[str, ...] = ()


def is_temporary_name(path: Path) -> bool:
    """True for known partial/download suffixes that must never become jobs."""
    suffixes = [part.lower() for part in path.suffixes]
    if not suffixes:
        return False
    return suffixes[-1] in _TEMPORARY_SUFFIXES


def is_candidate_name(path: Path) -> bool:
    """Extension and name filters only: no stat, no ffprobe.

    Rejects non-video extensions (case-insensitively), temporary/partial
    files, anything under a download/incomplete directory, and files carrying
    ``sample`` as a whole token.
    """
    suffixes = [part.lower() for part in path.suffixes]
    if not suffixes:
        return False
    if is_temporary_name(path):
        return False
    if path.suffix.lower() not in VIDEO_EXTENSIONS:
        return False
    for parent in path.parents:
        if parent.name.lower() in _SKIP_DIR_NAMES:
            return False
    tokens = [token for token in _TOKEN_SPLIT.split(path.stem.lower()) if token]
    return "sample" not in tokens


def iter_candidate_files(config: AppConfig, root: MediaRoot) -> Iterator[Path]:
    """Walk one root without following symlinks, yielding candidate videos."""
    del config
    if not root.path.is_dir():
        return
    for dirpath, dirnames, filenames in os.walk(root.path, followlinks=False):
        current = Path(dirpath)
        if current.is_symlink():
            dirnames[:] = []
            continue
        dirnames[:] = [
            name
            for name in dirnames
            if name.lower() not in _SKIP_DIR_NAMES and not (current / name).is_symlink()
        ]
        for name in filenames:
            candidate = current / name
            if candidate.is_symlink():
                continue
            if is_candidate_name(candidate):
                yield candidate


def resolve_explicit_path(config: AppConfig, path: Path) -> tuple[MediaRoot, Path]:
    """Resolve a CLI path and require it to live inside a configured root."""
    try:
        resolved = path.expanduser().resolve()
    except OSError as exc:
        raise NasSubtitlesError(
            "media path could not be resolved",
            code=ErrorCode.INVALID_MEDIA,
            detail={"path_token": path_token(path)},
        ) from exc
    root = config.root_for(resolved)
    if root is None:
        raise NasSubtitlesError(
            "path is outside the configured media roots",
            code=ErrorCode.MEDIA_PATH_OUTSIDE_ROOTS,
            detail={"path_token": path_token(resolved)},
        )
    if not root.path.is_dir():
        raise NasSubtitlesError(
            f"media root {root.root_id} is missing",
            code=ErrorCode.MEDIA_ROOT_MISSING,
            detail={"root_id": root.root_id},
        )
    return root, resolved


def observe(
    repository: JobRepository, *, root: MediaRoot, path: Path, now: datetime
) -> ScanObservation:
    """Record one sighting of a file's size and ``mtime_ns``."""
    try:
        stat = path.stat()
    except PermissionError as exc:
        raise NasSubtitlesError(
            "permission denied reading media",
            code=ErrorCode.PERMISSION_DENIED,
            detail={"path_token": path_token(path), "root_id": root.root_id},
        ) from exc
    except OSError as exc:
        raise NasSubtitlesError(
            "media could not be stat'ed",
            code=ErrorCode.INVALID_MEDIA,
            detail={"path_token": path_token(path), "root_id": root.root_id},
        ) from exc
    relative = root.relative_path_for(path)
    previous = repository.get_scan_observation(root_id=root.root_id, relative_path=relative)
    changed = (
        previous is None
        or previous.size_bytes != stat.st_size
        or previous.mtime_ns != stat.st_mtime_ns
    )
    first_seen = now
    if not changed and previous is not None:
        first_seen = previous.first_stable_seen_at or now
    if previous is None:
        log_event(
            _LOG,
            "media discovered",
            root_id=root.root_id,
            path_token=path_token(path),
        )
    if previous is None or changed:
        log_event(
            _LOG,
            "media waiting for stability",
            root_id=root.root_id,
            path_token=path_token(path),
        )
    observation = ScanObservation(
        root_id=root.root_id,
        relative_path=relative,
        size_bytes=stat.st_size,
        mtime_ns=stat.st_mtime_ns,
        first_stable_seen_at=first_seen,
        last_seen_at=now,
    )
    return repository.upsert_scan_observation(observation)


def is_stable(config: AppConfig, observation: ScanObservation, *, now: datetime) -> bool:
    """Two identical observations a stability window apart, plus minimum age."""
    first = observation.first_stable_seen_at
    last = observation.last_seen_at
    if first is None or last is None:
        return False
    if last <= first:
        return False
    if (last - first).total_seconds() < config.stability_window_seconds:
        return False
    age_seconds = now.timestamp() - (observation.mtime_ns / 1_000_000_000)
    return age_seconds >= config.minimum_file_age_seconds


def compute_fingerprint(
    *, root: MediaRoot, path: Path, audio_stream_index: int | None = None
) -> MediaFingerprint:
    """Hash 1 MiB from each end; never hash the whole film."""
    try:
        stat = path.stat()
    except OSError as exc:
        raise NasSubtitlesError(
            "media could not be read for fingerprinting",
            code=ErrorCode.INVALID_MEDIA,
            detail={"path_token": path_token(path), "root_id": root.root_id},
        ) from exc
    head, tail = _head_and_tail_hashes(path, size=stat.st_size)
    return MediaFingerprint(
        root_id=root.root_id,
        relative_path=root.relative_path_for(path),
        size_bytes=stat.st_size,
        mtime_ns=stat.st_mtime_ns,
        head_sha256=head,
        tail_sha256=tail,
        audio_stream_index=audio_stream_index,
    )


def find_existing_subtitles(
    *, path: Path, probe_result: ProbeResult | None = None
) -> tuple[ExistingSubtitle, ...]:
    """External sidecars plus embedded streams, with forced flags preserved."""
    found: list[ExistingSubtitle] = []
    parent = path.parent
    stem = path.stem
    if parent.is_dir():
        try:
            entries = list(parent.iterdir())
        except OSError:
            entries = []
        for candidate in entries:
            if candidate.is_symlink() or not candidate.is_file():
                continue
            if candidate.suffix.lower() not in _SUBTITLE_EXTENSIONS:
                continue
            if not _sidecar_belongs_to(stem, candidate.name):
                continue
            tokens = [token.lower() for token in Path(candidate.name).stem.split(".")]
            forced = "forced" in tokens
            language = subtitle_suffix_language(candidate.name)
            portuguese = language == PORTUGUESE or (
                language is None and any(token in PORTUGUESE_SUBTITLE_SUFFIXES for token in tokens)
            )
            found.append(
                ExistingSubtitle(
                    origin=SubtitleOrigin.EXTERNAL_FILE,
                    language=language,
                    is_forced=forced,
                    path=candidate,
                    satisfies_target=bool(portuguese and not forced),
                    uncertain=language is None,
                    reason=_sidecar_reason(language, forced),
                )
            )
    if probe_result is not None:
        for stream in probe_result.subtitle_streams:
            portuguese = stream.language == PORTUGUESE
            found.append(
                ExistingSubtitle(
                    origin=SubtitleOrigin.EMBEDDED_STREAM,
                    language=stream.language,
                    is_forced=stream.is_forced,
                    stream_index=stream.index,
                    satisfies_target=bool(portuguese and not stream.is_forced),
                    uncertain=stream.language is None,
                    reason=_embedded_reason(stream.language, stream.is_forced),
                )
            )
    return tuple(found)


def has_portuguese_subtitle(subtitles: tuple[ExistingSubtitle, ...]) -> bool:
    """True only for a complete, non-forced Portuguese subtitle."""
    return any(item.satisfies_target for item in subtitles)


def canonical_target_sidecar(path: Path, target_language: str) -> Path:
    """``<stem>.<logical-target-language>.srt`` beside the video, never a backend code."""
    return path.with_suffix(f".{target_language}.srt")


def has_target_sidecar(path: Path, target_language: str) -> bool:
    """Whether the canonical generated sidecar for ``target_language`` already exists."""
    sidecar = canonical_target_sidecar(path, target_language)
    return sidecar.is_file() and not sidecar.is_symlink()


def scan(
    config: AppConfig,
    repository: JobRepository,
    *,
    dry_run: bool = False,
    now: datetime | None = None,
    probe: FfprobeMediaProbe | None = None,
) -> ScanSummary:
    """One full scan pass. Refuses to run when a configured root is missing."""
    moment = now or datetime.now(tz=UTC)
    inspector = probe or FfprobeMediaProbe()
    for root in config.roots:
        if not root.path.is_dir():
            raise NasSubtitlesError(
                f"media root {root.root_id} is missing",
                code=ErrorCode.MEDIA_ROOT_MISSING,
                detail={"root_id": root.root_id},
            )

    examined = 0
    enqueued = 0
    skipped_unstable = 0
    skipped_too_young = 0
    skipped_existing = 0
    skipped_unreadable = 0
    already_queued = 0
    skipped_temporary = 0
    skipped_unsupported = 0
    enqueued_paths: list[str] = []

    for root in config.roots:
        skipped_temporary, skipped_unsupported = _count_ignored_names(
            root, skipped_temporary, skipped_unsupported
        )
        for path in iter_candidate_files(config, root):
            examined += 1
            try:
                observation = observe(repository, root=root, path=path, now=moment)
            except NasSubtitlesError:
                skipped_unreadable += 1
                continue
            age_seconds = moment.timestamp() - (observation.mtime_ns / 1_000_000_000)
            if age_seconds < config.minimum_file_age_seconds:
                skipped_too_young += 1
                continue
            if not is_stable(config, observation, now=moment):
                skipped_unstable += 1
                continue
            try:
                probe_result = inspector.probe(path)
                stream = select_audio_stream(probe_result, config=config)
                fingerprint = compute_fingerprint(
                    root=root, path=path, audio_stream_index=stream.index
                )
            except NasSubtitlesError:
                skipped_unreadable += 1
                continue
            skip_reason = _existing_subtitle_skip_reason(config, path, probe_result)
            queued_already = _already_queued(repository, fingerprint, config.pipeline_config_hash)
            if skip_reason is not None:
                skipped_existing += 1
                if not queued_already:
                    log_event(
                        _LOG,
                        "media became stable",
                        root_id=root.root_id,
                        path_token=path_token(path),
                    )
                    log_event(
                        _LOG,
                        "existing target subtitle found",
                        root_id=root.root_id,
                        path_token=path_token(path),
                        reason=skip_reason,
                    )
                    log_event(
                        _LOG,
                        "media skipped",
                        root_id=root.root_id,
                        path_token=path_token(path),
                        reason=skip_reason,
                    )
                    if not dry_run:
                        skipped_job = repository.enqueue(
                            fingerprint=fingerprint,
                            pipeline_config_hash=config.pipeline_config_hash,
                        )
                        if skipped_job.state is JobState.QUEUED:
                            repository.transition(
                                job_id=skipped_job.id,
                                state=JobState.SKIPPED,
                                error_detail=skip_reason,
                            )
                continue
            if queued_already:
                already_queued += 1
                continue
            log_event(
                _LOG,
                "media became stable",
                root_id=root.root_id,
                path_token=path_token(path),
            )
            if not dry_run:
                job = repository.enqueue(
                    fingerprint=fingerprint,
                    pipeline_config_hash=config.pipeline_config_hash,
                )
                log_event(
                    _LOG,
                    "job queued",
                    job_id=job.id,
                    root_id=root.root_id,
                    path_token=path_token(path),
                )
            enqueued += 1
            enqueued_paths.append(root.relative_path_for(path))

    log_event(
        _LOG,
        "scan complete",
        examined=examined,
        enqueued=enqueued,
        skipped_unstable=skipped_unstable,
        skipped_existing_subtitle=skipped_existing,
        already_queued=already_queued,
    )
    return ScanSummary(
        examined=examined,
        enqueued=enqueued,
        skipped_unstable=skipped_unstable,
        skipped_too_young=skipped_too_young,
        skipped_existing_subtitle=skipped_existing,
        skipped_unreadable=skipped_unreadable,
        already_queued=already_queued,
        skipped_temporary=skipped_temporary,
        skipped_unsupported=skipped_unsupported,
        enqueued_paths=tuple(enqueued_paths),
    )


def enqueue_path(
    config: AppConfig,
    repository: JobRepository,
    path: Path,
    *,
    source_language: str | None = None,
    audio_stream_index: int | None = None,
    priority: int = 0,
    preview_seconds: float | None = None,
    preview_offset_seconds: float | None = None,
    require_stability: bool = True,
    now: datetime | None = None,
    probe: FfprobeMediaProbe | None = None,
) -> tuple[JobRecord | None, str | None]:
    """Inspect one explicit path and enqueue it when it is eligible.

    Returns ``(job, skip_reason)``. ``skip_reason`` is set when an existing
    Portuguese subtitle means the file must not enter the queue.
    """
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
    skip_reason = _existing_subtitle_skip_reason(config, resolved, probe_result)
    if skip_reason is not None:
        log_event(
            _LOG,
            "existing target subtitle found",
            root_id=root.root_id,
            path_token=path_token(resolved),
            reason=skip_reason,
        )
        log_event(
            _LOG,
            "media skipped",
            root_id=root.root_id,
            path_token=path_token(resolved),
            reason=skip_reason,
        )
        return None, skip_reason
    fingerprint = compute_fingerprint(root=root, path=resolved, audio_stream_index=stream.index)
    job = repository.enqueue(
        fingerprint=fingerprint,
        pipeline_config_hash=config.pipeline_config_hash,
        priority=priority,
        source_language_override=source_language,
        audio_stream_index_override=audio_stream_index,
        preview_seconds=preview_seconds,
        preview_offset_seconds=preview_offset_seconds,
    )
    log_event(
        _LOG,
        "job queued",
        job_id=job.id,
        root_id=root.root_id,
        path_token=path_token(resolved),
    )
    return job, None


def _existing_subtitle_skip_reason(
    config: AppConfig, path: Path, probe_result: ProbeResult | None
) -> str | None:
    """Return a skip reason when policy forbids generating over an existing target."""
    assert config.existing_subtitle_policy is ExistingSubtitlePolicy.SKIP
    if has_target_sidecar(path, config.target_language):
        return f"existing target sidecar ({config.target_language})"
    existing = find_existing_subtitles(path=path, probe_result=probe_result)
    if has_portuguese_subtitle(existing):
        matching = next(item for item in existing if item.satisfies_target)
        return matching.reason or "existing portuguese subtitle"
    return None


def _count_ignored_names(
    root: MediaRoot, skipped_temporary: int, skipped_unsupported: int
) -> tuple[int, int]:
    """Count non-candidate files once per scan for observability, without enqueueing them."""
    if not root.path.is_dir():
        return skipped_temporary, skipped_unsupported
    for dirpath, dirnames, filenames in os.walk(root.path, followlinks=False):
        current = Path(dirpath)
        if current.is_symlink():
            dirnames[:] = []
            continue
        dirnames[:] = [
            name
            for name in dirnames
            if name.lower() not in _SKIP_DIR_NAMES and not (current / name).is_symlink()
        ]
        for name in filenames:
            candidate = current / name
            if candidate.is_symlink() or is_candidate_name(candidate):
                continue
            if is_temporary_name(candidate):
                skipped_temporary += 1
                log_event(
                    _LOG,
                    "temporary file ignored",
                    root_id=root.root_id,
                    path_token=path_token(candidate),
                    level=logging.DEBUG,
                )
            elif candidate.suffix.lower() in _SUBTITLE_EXTENSIONS:
                skipped_unsupported += 1
            else:
                skipped_unsupported += 1
                log_event(
                    _LOG,
                    "unsupported media ignored",
                    root_id=root.root_id,
                    path_token=path_token(candidate),
                    level=logging.DEBUG,
                )
    return skipped_temporary, skipped_unsupported


def _already_queued(
    repository: JobRepository, fingerprint: MediaFingerprint, pipeline_config_hash: str
) -> bool:
    """True when a *full* library job already exists for this execution identity.

    Preview jobs are ignored: ``--preview-seconds`` never produces the library
    sidecar and must not block automatic processing. Any full-job state in
    ``LIBRARY_SCAN_KNOWN_STATES`` counts so scans and restarts reuse that row
    instead of enqueueing a duplicate.
    """
    digest = fingerprint.digest()
    for job in repository.list_jobs(limit=10_000):
        if not job.is_library_job():
            continue
        if job.state not in LIBRARY_SCAN_KNOWN_STATES:
            continue
        if job.fingerprint.digest() == digest and job.pipeline_config_hash == pipeline_config_hash:
            return True
    return False


def _sidecar_belongs_to(video_stem: str, subtitle_name: str) -> bool:
    lowered = subtitle_name.lower()
    prefix = f"{video_stem.lower()}."
    return lowered.startswith(prefix)


def _sidecar_reason(language: str | None, forced: bool) -> str:
    if language is None:
        return "external subtitle has no language suffix"
    if forced:
        return "external subtitle is forced"
    if language == PORTUGUESE:
        return "external portuguese subtitle"
    return f"external subtitle language={language}"


def _embedded_reason(language: str | None, forced: bool) -> str:
    if language is None:
        return "embedded subtitle has no language tag"
    if forced:
        return "embedded subtitle is forced"
    if language == PORTUGUESE:
        return "embedded portuguese subtitle"
    return f"embedded subtitle language={language}"


def _head_and_tail_hashes(path: Path, *, size: int) -> tuple[str, str]:
    sample = FINGERPRINT_SAMPLE_BYTES
    with path.open("rb") as handle:
        head = handle.read(sample)
        if size <= sample:
            tail = head
        else:
            handle.seek(max(0, size - sample))
            tail = handle.read(sample)
    return hashlib.sha256(head).hexdigest(), hashlib.sha256(tail).hexdigest()
