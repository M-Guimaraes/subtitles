"""Library scanning, stability and existing-subtitle detection.

Owned by stages 3 and 7 of the plan. The scanner never follows symlinks,
never creates a missing media root and never writes inside the library.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .config import AppConfig, MediaRoot
from .domain import (
    ExistingSubtitle,
    JobRepository,
    MediaFingerprint,
    ProbeResult,
    ScanObservation,
)

__all__ = [
    "ScanSummary",
    "compute_fingerprint",
    "find_existing_subtitles",
    "has_portuguese_subtitle",
    "is_candidate_name",
    "is_stable",
    "iter_candidate_files",
    "observe",
    "resolve_explicit_path",
    "scan",
]


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
    enqueued_paths: tuple[str, ...] = ()


def is_candidate_name(path: Path) -> bool:
    """Extension and name filters only: no stat, no ffprobe.

    Rejects non-video extensions (case-insensitively), ``.part`` files,
    anything under a download/incomplete directory, and files carrying
    ``sample`` as a whole token.
    """
    raise NotImplementedError("discovery is implemented in stage 3 (media)")


def iter_candidate_files(config: AppConfig, root: MediaRoot) -> Iterator[Path]:
    """Walk one root without following symlinks, yielding candidate videos."""
    raise NotImplementedError("discovery is implemented in stage 3 (media)")


def resolve_explicit_path(config: AppConfig, path: Path) -> tuple[MediaRoot, Path]:
    """Resolve a CLI path and require it to live inside a configured root."""
    raise NotImplementedError("discovery is implemented in stage 3 (media)")


def observe(
    repository: JobRepository, *, root: MediaRoot, path: Path, now: datetime
) -> ScanObservation:
    """Record one sighting of a file's size and ``mtime_ns``."""
    raise NotImplementedError("discovery is implemented in stage 3 (media)")


def is_stable(config: AppConfig, observation: ScanObservation, *, now: datetime) -> bool:
    """Two identical observations a stability window apart, plus minimum age."""
    raise NotImplementedError("discovery is implemented in stage 3 (media)")


def compute_fingerprint(
    *, root: MediaRoot, path: Path, audio_stream_index: int | None = None
) -> MediaFingerprint:
    """Hash 1 MiB from each end; never hash the whole film."""
    raise NotImplementedError("discovery is implemented in stage 3 (media)")


def find_existing_subtitles(
    *, path: Path, probe_result: ProbeResult | None = None
) -> tuple[ExistingSubtitle, ...]:
    """External sidecars plus embedded streams, with forced flags preserved."""
    raise NotImplementedError("discovery is implemented in stage 3 (media)")


def has_portuguese_subtitle(subtitles: tuple[ExistingSubtitle, ...]) -> bool:
    """True only for a complete, non-forced Portuguese subtitle."""
    raise NotImplementedError("discovery is implemented in stage 3 (media)")


def scan(
    config: AppConfig,
    repository: JobRepository,
    *,
    dry_run: bool = False,
    now: datetime | None = None,
) -> ScanSummary:
    """One full scan pass. Refuses to run when a configured root is missing."""
    raise NotImplementedError("discovery is implemented in stage 3 (media)")
