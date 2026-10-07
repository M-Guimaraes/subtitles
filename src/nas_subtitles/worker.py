"""The single worker process: scan, claim, run, heartbeat. Owned by stage 7.

There is exactly one daemon. Scanning is a periodic task inside it, not a
second process. A ``flock`` on ``state_dir`` keeps two workers from sharing a
queue, and the heartbeat runs on its own thread so a blocking inference still
looks alive.
"""

from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path

from .config import AppConfig
from .domain import JobRepository

__all__ = [
    "CleanupSummary",
    "Worker",
    "backup_state",
    "cleanup_work_dir",
    "state_lock",
]


@dataclass(frozen=True, slots=True)
class CleanupSummary:
    """What ``cleanup`` removed, or would remove under ``--dry-run``."""

    inspected_jobs: int = 0
    removed_paths: tuple[str, ...] = ()
    reclaimed_bytes: int = 0
    dry_run: bool = True


class Worker:
    """Serial worker: one job at a time, SIGTERM-aware."""

    def __init__(self, config: AppConfig, repository: JobRepository, *, owner: str) -> None:
        self.config = config
        self.repository = repository
        self.owner = owner

    def run(self, *, once: bool = False) -> int:
        """Loop over scan and queue until stopped; returns a process exit code."""
        raise NotImplementedError("the worker is implemented in stage 7 (worker)")

    def run_once(self) -> bool:
        """Claim and process at most one job. ``False`` when the queue is empty."""
        raise NotImplementedError("the worker is implemented in stage 7 (worker)")

    def request_stop(self) -> None:
        """Called from the SIGTERM handler; aborts the in-flight checkpoint."""
        raise NotImplementedError("the worker is implemented in stage 7 (worker)")

    def heartbeat(self) -> None:
        """Record one ``worker_heartbeat`` event; runs on its own thread."""
        raise NotImplementedError("the worker is implemented in stage 7 (worker)")


def state_lock(config: AppConfig) -> AbstractContextManager[None]:
    """``flock`` on ``state_dir``; raises ``LockBusyError`` (exit 6) if taken."""
    raise NotImplementedError("the worker is implemented in stage 7 (worker)")


def cleanup_work_dir(
    config: AppConfig,
    repository: JobRepository,
    *,
    older_than_days: int = 30,
    dry_run: bool = True,
) -> CleanupSummary:
    """Delete only inside ``work_dir``, only for validated job IDs, never by glob.

    A successful job loses its WAV files immediately; transcripts, translations
    and manifests survive for ``older_than_days``. Failed and under-review jobs
    keep their checkpoints.
    """
    raise NotImplementedError("cleanup is implemented in stage 7 (worker)")


def backup_state(config: AppConfig, destination: Path) -> Path:
    """Back up config, lock, model manifest and the SQLite database."""
    raise NotImplementedError("backup is implemented in stage 7 (worker)")
