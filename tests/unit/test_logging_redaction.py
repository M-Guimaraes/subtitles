"""Logs must stay exportable: no speech, no personal paths."""

from __future__ import annotations

import io
import json
import logging

from nas_subtitles.domain import ErrorCode, PipelineStage
from nas_subtitles.logging_setup import REDACTED, configure_logging, log_event, path_token


def _capture(**fields: object) -> dict[str, object]:
    stream = io.StringIO()
    configure_logging(level=logging.INFO, stream=stream)
    logger = logging.getLogger("test")
    log_event(logger, "stage_finished", **fields)  # type: ignore[arg-type]
    payload: dict[str, object] = json.loads(stream.getvalue().strip())
    return payload


def test_event_carries_the_conventional_fields() -> None:
    payload = _capture(
        job_id="job-1",
        stage=PipelineStage.TRANSCRIBE,
        chunk_index=2,
        duration_ms=1234.5678,
        error_code=ErrorCode.IO_ERROR,
    )
    assert payload["event"] == "stage_finished"
    assert payload["level"] == "info"
    assert payload["job_id"] == "job-1"
    assert payload["stage"] == "transcribe"
    assert payload["chunk_index"] == 2
    assert payload["duration_ms"] == 1234.568
    assert payload["error_code"] == "io_error"
    assert str(payload["timestamp"]).endswith("+00:00")


def test_speech_and_paths_are_never_logged_verbatim() -> None:
    payload = _capture(
        text="the entire spoken sentence",
        path="/Users/someone/Movies/Private.mkv",
        token="super-secret-token",
    )
    assert payload["text"] == REDACTED
    assert payload["path"] == REDACTED
    assert payload["token"] == REDACTED
    assert "Private" not in json.dumps(payload)
    assert "super-secret-token" not in json.dumps(payload)


def test_path_token_is_stable_and_not_reversible() -> None:
    token = path_token("/Users/someone/Movies/Private.mkv")
    assert token == path_token("/Users/someone/Movies/Private.mkv")
    assert token != path_token("/Users/someone/Movies/Other.mkv")
    assert "Private" not in token
    assert len(token) == 12


def test_output_is_one_json_object_per_line() -> None:
    stream = io.StringIO()
    configure_logging(level=logging.INFO, stream=stream)
    logger = logging.getLogger("test")
    log_event(logger, "first")
    log_event(logger, "second")
    lines = [line for line in stream.getvalue().splitlines() if line]
    assert len(lines) == 2
    assert [json.loads(line)["event"] for line in lines] == ["first", "second"]
