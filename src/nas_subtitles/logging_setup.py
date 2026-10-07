"""Structured JSON logging on stdout.

Logs are an exportable artifact, so two things never appear in them: the
transcribed speech itself and personal filesystem paths. Call sites pass a
``root_id`` plus :func:`path_token` instead of a path; the formatter also
drops a small set of known-sensitive keys as a backstop.

Detailed text stays in the local artifacts under ``work_dir`` and ``state_dir``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sys
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any

from .domain import ErrorCode, PipelineStage

__all__ = [
    "REDACTED",
    "JsonLogFormatter",
    "configure_logging",
    "event_payload",
    "log_event",
    "path_token",
]

REDACTED = "[redacted]"

_SENSITIVE_KEYS = frozenset({"text", "speech", "transcript", "translation", "path", "filename"})
"""Keys that would leak speech or personal paths if logged verbatim."""

_RESERVED_RECORD_KEYS = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "module",
        "msecs",
        "message",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "taskName",
        "thread",
        "threadName",
    }
)

_ORDERED_FIELDS = (
    "job_id",
    "stage",
    "chunk_index",
    "duration_ms",
    "error_code",
)


def path_token(path: Path | str) -> str:
    """Stable, non-reversible reference to a path, safe to export."""
    return hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:12]


class JsonLogFormatter(logging.Formatter):
    """Renders one JSON object per line."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(
                timespec="milliseconds"
            ),
            "level": record.levelname.lower(),
            "event": record.getMessage(),
            "logger": record.name,
        }

        extras = {
            key: value
            for key, value in record.__dict__.items()
            if key not in _RESERVED_RECORD_KEYS and not key.startswith("_")
        }

        for name in _ORDERED_FIELDS:
            if name in extras:
                payload[name] = _coerce(name, extras.pop(name))

        for key, value in sorted(extras.items()):
            payload[key] = _coerce(key, value)

        if record.exc_info is not None:
            payload["exception"] = self.formatException(record.exc_info).splitlines()[-1]

        return json.dumps(payload, ensure_ascii=False, default=str)


def _coerce(key: str, value: object) -> object:
    if key in _SENSITIVE_KEYS:
        return REDACTED
    if isinstance(value, Path):
        return path_token(value)
    if isinstance(value, PipelineStage | ErrorCode):
        return str(value)
    if isinstance(value, bool | int | float | str) or value is None:
        return value
    return str(value)


def configure_logging(*, level: int = logging.INFO, stream: IO[str] | None = None) -> None:
    """Install the JSON handler on the root logger, replacing any previous one."""
    handler = logging.StreamHandler(stream if stream is not None else sys.stdout)
    handler.setFormatter(JsonLogFormatter())

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
        existing.close()
    root.addHandler(handler)
    root.setLevel(level)


def log_event(
    logger: logging.Logger,
    event: str,
    *,
    level: int = logging.INFO,
    job_id: str | None = None,
    stage: PipelineStage | None = None,
    chunk_index: int | None = None,
    duration_ms: float | None = None,
    error_code: ErrorCode | None = None,
    **fields: object,
) -> None:
    """Emit one structured event with the conventional field names."""
    extra: dict[str, object] = dict(fields)
    if job_id is not None:
        extra["job_id"] = job_id
    if stage is not None:
        extra["stage"] = str(stage)
    if chunk_index is not None:
        extra["chunk_index"] = chunk_index
    if duration_ms is not None:
        extra["duration_ms"] = round(duration_ms, 3)
    if error_code is not None:
        extra["error_code"] = str(error_code)
    logger.log(level, event, extra=extra)


def event_payload(fields: Mapping[str, object]) -> dict[str, object]:
    """Apply the same redaction rules to a payload stored in the ``events`` table."""
    return {key: _coerce(key, value) for key, value in fields.items()}
