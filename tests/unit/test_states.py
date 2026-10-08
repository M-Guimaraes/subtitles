"""State machine transitions and retry policy."""

from __future__ import annotations

import pytest

from nas_subtitles.domain import ErrorCode, JobState, NasSubtitlesError, PipelineStage
from nas_subtitles.states import (
    can_transition,
    ensure_transition,
    is_retryable,
    is_terminal,
    next_stage,
    retry_delay_seconds,
    should_retry,
)


def test_worker_cannot_reopen_needs_review() -> None:
    assert not can_transition(JobState.NEEDS_REVIEW, JobState.RUNNING)
    assert not can_transition(JobState.NEEDS_REVIEW, JobState.QUEUED)


def test_approve_moves_review_to_ready() -> None:
    assert can_transition(JobState.NEEDS_REVIEW, JobState.READY_TO_PUBLISH)


def test_cancel_blocks_publication_from_ready() -> None:
    assert can_transition(JobState.READY_TO_PUBLISH, JobState.CANCELLED)
    assert not can_transition(JobState.CANCELLED, JobState.COMPLETED)


def test_failed_job_can_be_retried_to_queued() -> None:
    assert can_transition(JobState.FAILED, JobState.QUEUED)


def test_terminal_states_have_no_automatic_exit() -> None:
    for state in (JobState.COMPLETED, JobState.SKIPPED, JobState.CANCELLED):
        assert is_terminal(state)
        assert can_transition(state, state)
        assert not can_transition(state, JobState.RUNNING)


def test_operator_can_reprocess_terminal_jobs() -> None:
    """Human reprocess may re-queue; the worker still cannot reopen them as running."""
    for state in (JobState.COMPLETED, JobState.SKIPPED, JobState.CANCELLED):
        assert can_transition(state, JobState.QUEUED)
        assert not can_transition(state, JobState.RUNNING)


def test_forbidden_transition_raises_invalid_state() -> None:
    with pytest.raises(NasSubtitlesError) as raised:
        ensure_transition(JobState.COMPLETED, JobState.RUNNING)
    assert raised.value.code is ErrorCode.INVALID_STATE_TRANSITION


def test_next_stage_walks_the_pipeline() -> None:
    assert next_stage(PipelineStage.PROBE) is PipelineStage.DETECT_LANGUAGE
    assert next_stage(PipelineStage.PUBLISH) is None


def test_permission_and_conflict_are_not_retryable() -> None:
    assert not is_retryable(ErrorCode.PERMISSION_DENIED)
    assert not is_retryable(ErrorCode.OUTPUT_CONFLICT)
    assert not is_retryable(ErrorCode.INVALID_MEDIA)
    assert not is_retryable(ErrorCode.MODEL_MISSING)
    assert is_retryable(ErrorCode.IO_ERROR)
    assert is_retryable(ErrorCode.SUBPROCESS_TIMEOUT)


def test_retry_stops_after_configured_attempts() -> None:
    assert should_retry(error_code=ErrorCode.IO_ERROR, attempt_count=1, max_attempts=4)
    assert should_retry(error_code=ErrorCode.IO_ERROR, attempt_count=3, max_attempts=4)
    assert not should_retry(error_code=ErrorCode.IO_ERROR, attempt_count=4, max_attempts=4)
    assert not should_retry(error_code=ErrorCode.INVALID_MEDIA, attempt_count=1, max_attempts=4)


def test_retry_delays_match_the_configured_backoff() -> None:
    delays = (300, 1800, 7200)
    assert retry_delay_seconds(delays, 1) == 300
    assert retry_delay_seconds(delays, 2) == 1800
    assert retry_delay_seconds(delays, 3) == 7200
    assert retry_delay_seconds(delays, 10) == 7200
