"""Liveness check used by the container healthcheck.

Deliberately cheap and narrow: it opens the queue database read-only and looks
at the most recent worker heartbeat. It never loads a model, never spawns
FFmpeg and never touches the media library, so it stays fast while a long
transcription holds the CPU.

``doctor`` is the broad environment check; this is not that.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .config import AppConfig
from .domain import HEARTBEAT_EVENT_CODE

__all__ = ["HealthReport", "check_health"]


@dataclass(frozen=True, slots=True)
class HealthReport:
    """Result of the heartbeat and database checks."""

    healthy: bool
    reason: str
    database_path: Path
    database_reachable: bool
    heartbeat_at: datetime | None
    heartbeat_age_seconds: float | None
    max_heartbeat_age_seconds: int

    def to_dict(self) -> dict[str, object]:
        return {
            "healthy": self.healthy,
            "reason": self.reason,
            "database_reachable": self.database_reachable,
            "heartbeat_at": self.heartbeat_at.isoformat() if self.heartbeat_at else None,
            "heartbeat_age_seconds": (
                round(self.heartbeat_age_seconds, 3)
                if self.heartbeat_age_seconds is not None
                else None
            ),
            "max_heartbeat_age_seconds": self.max_heartbeat_age_seconds,
        }


def check_health(config: AppConfig, *, now: datetime | None = None) -> HealthReport:
    """Report whether the queue database is readable and the worker is alive."""
    moment = now or datetime.now(tz=UTC)
    database_path = config.database_path
    max_age = config.worker.stale_lease_seconds

    if not database_path.exists():
        return HealthReport(
            healthy=False,
            reason=f"queue database is missing at {database_path}",
            database_path=database_path,
            database_reachable=False,
            heartbeat_at=None,
            heartbeat_age_seconds=None,
            max_heartbeat_age_seconds=max_age,
        )

    try:
        heartbeat = _read_latest_heartbeat(database_path)
    except (sqlite3.Error, ValueError) as exc:
        return HealthReport(
            healthy=False,
            reason=f"queue database is not readable: {exc}",
            database_path=database_path,
            database_reachable=False,
            heartbeat_at=None,
            heartbeat_age_seconds=None,
            max_heartbeat_age_seconds=max_age,
        )

    if heartbeat is None:
        return HealthReport(
            healthy=False,
            reason="no worker heartbeat recorded yet",
            database_path=database_path,
            database_reachable=True,
            heartbeat_at=None,
            heartbeat_age_seconds=None,
            max_heartbeat_age_seconds=max_age,
        )

    age = (moment - heartbeat).total_seconds()
    if age > max_age:
        return HealthReport(
            healthy=False,
            reason=f"last worker heartbeat is {age:.0f}s old (limit {max_age}s)",
            database_path=database_path,
            database_reachable=True,
            heartbeat_at=heartbeat,
            heartbeat_age_seconds=age,
            max_heartbeat_age_seconds=max_age,
        )

    return HealthReport(
        healthy=True,
        reason="worker heartbeat is recent and the queue database is readable",
        database_path=database_path,
        database_reachable=True,
        heartbeat_at=heartbeat,
        heartbeat_age_seconds=age,
        max_heartbeat_age_seconds=max_age,
    )


def _read_latest_heartbeat(database_path: Path) -> datetime | None:
    """Most recent ``worker_heartbeat`` event, or ``None`` if there is none.

    Opened read-only so a healthcheck can never create or migrate the file.
    """
    uri = f"file:{database_path}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, timeout=5.0)) as connection:
        table = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'events'"
        ).fetchone()
        if table is None:
            raise sqlite3.DatabaseError("schema not initialised: table 'events' is missing")

        row = connection.execute(
            "SELECT max(created_at) FROM events WHERE code = ?",
            (HEARTBEAT_EVENT_CODE,),
        ).fetchone()

    if row is None or row[0] is None:
        return None
    return _parse_timestamp(str(row[0]))


def _parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)
