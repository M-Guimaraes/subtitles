"""Job state machine. Owned by stage 2 of the plan.

Every state change in the application must go through this module so the
allowed transitions stay testable in one place. ``needs_review`` is never
reopened by the worker, and cancellation blocks publication while keeping the
checkpoints on disk.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from .domain import (
    PIPELINE_STAGE_ORDER,
    RETRYABLE_ERROR_CODES,
    TERMINAL_JOB_STATES,
    ErrorCode,
    JobState,
    NasSubtitlesError,
    PipelineStage,
)

__all__ = [
    "ALLOWED_TRANSITIONS",
    "can_transition",
    "ensure_transition",
    "is_retryable",
    "is_terminal",
    "next_stage",
    "retry_delay_seconds",
    "should_retry",
]

ALLOWED_TRANSITIONS: Mapping[JobState, frozenset[JobState]] = {
    JobState.QUEUED: frozenset({JobState.RUNNING, JobState.CANCELLED, JobState.SKIPPED}),
    JobState.RUNNING: frozenset(
        {
            JobState.QUEUED,
            JobState.RETRY_WAIT,
            JobState.NEEDS_REVIEW,
            JobState.READY_TO_PUBLISH,
            JobState.COMPLETED,
            JobState.SKIPPED,
            JobState.FAILED,
            JobState.CANCELLED,
        }
    ),
    JobState.RETRY_WAIT: frozenset(
        {JobState.RUNNING, JobState.QUEUED, JobState.CANCELLED, JobState.FAILED}
    ),
    JobState.NEEDS_REVIEW: frozenset(
        {JobState.READY_TO_PUBLISH, JobState.CANCELLED, JobState.FAILED}
    ),
    JobState.READY_TO_PUBLISH: frozenset(
        {JobState.COMPLETED, JobState.CANCELLED, JobState.NEEDS_REVIEW, JobState.FAILED}
    ),
    JobState.COMPLETED: frozenset(),
    JobState.SKIPPED: frozenset(),
    JobState.FAILED: frozenset({JobState.QUEUED, JobState.CANCELLED}),
    JobState.CANCELLED: frozenset(),
}


def can_transition(current: JobState, target: JobState) -> bool:
    """Whether ``current -> target`` is one of the allowed edges."""
    if current is target:
        return True
    return target in ALLOWED_TRANSITIONS[current]


def ensure_transition(current: JobState, target: JobState) -> None:
    """Raise ``NasSubtitlesError(INVALID_STATE_TRANSITION)`` for a forbidden edge."""
    if can_transition(current, target):
        return
    raise NasSubtitlesError(
        f"cannot move a job from {current} to {target}",
        code=ErrorCode.INVALID_STATE_TRANSITION,
        detail={"from": str(current), "to": str(target)},
    )


def is_terminal(state: JobState) -> bool:
    """Whether the job will not move again without an explicit command."""
    return state in TERMINAL_JOB_STATES


def next_stage(stage: PipelineStage) -> PipelineStage | None:
    """The stage that follows ``stage``, or ``None`` after ``publish``."""
    index = PIPELINE_STAGE_ORDER.index(stage)
    if index + 1 >= len(PIPELINE_STAGE_ORDER):
        return None
    return PIPELINE_STAGE_ORDER[index + 1]


def is_retryable(error_code: ErrorCode) -> bool:
    """Only transient I/O and subprocess faults may be retried."""
    return error_code in RETRYABLE_ERROR_CODES


def should_retry(*, error_code: ErrorCode, attempt_count: int, max_attempts: int) -> bool:
    """Whether a failed attempt goes to ``retry_wait`` instead of ``failed``."""
    if not is_retryable(error_code):
        return False
    return attempt_count < max_attempts


def retry_delay_seconds(delays: Sequence[int], attempt_count: int) -> int:
    """Backoff for the next attempt, clamped to the last configured delay."""
    if not delays:
        raise ValueError("retry delays must not be empty")
    index = max(0, min(attempt_count - 1, len(delays) - 1))
    return delays[index]
