"""Job state machine. Owned by stage 2 of the plan.

Every state change in the application must go through this module so the
allowed transitions stay testable in one place. ``needs_review`` is never
reopened by the worker, and cancellation blocks publication while keeping the
checkpoints on disk.
"""

from __future__ import annotations

from collections.abc import Sequence

from .domain import ErrorCode, JobState, PipelineStage

__all__ = [
    "can_transition",
    "ensure_transition",
    "is_retryable",
    "is_terminal",
    "next_stage",
    "retry_delay_seconds",
    "should_retry",
]


def can_transition(current: JobState, target: JobState) -> bool:
    """Whether ``current -> target`` is one of the allowed edges."""
    raise NotImplementedError("state transitions are implemented in stage 2 (state)")


def ensure_transition(current: JobState, target: JobState) -> None:
    """Raise ``NasSubtitlesError(INVALID_STATE_TRANSITION)`` for a forbidden edge."""
    raise NotImplementedError("state transitions are implemented in stage 2 (state)")


def is_terminal(state: JobState) -> bool:
    """Whether the job will not move again without an explicit command."""
    raise NotImplementedError("state transitions are implemented in stage 2 (state)")


def next_stage(stage: PipelineStage) -> PipelineStage | None:
    """The stage that follows ``stage``, or ``None`` after ``publish``."""
    raise NotImplementedError("state transitions are implemented in stage 2 (state)")


def is_retryable(error_code: ErrorCode) -> bool:
    """Only transient I/O and subprocess faults may be retried."""
    raise NotImplementedError("retry policy is implemented in stage 2 (state)")


def should_retry(*, error_code: ErrorCode, attempt_count: int, max_attempts: int) -> bool:
    """Whether a failed attempt goes to ``retry_wait`` instead of ``failed``."""
    raise NotImplementedError("retry policy is implemented in stage 2 (state)")


def retry_delay_seconds(delays: Sequence[int], attempt_count: int) -> int:
    """Backoff for the next attempt, clamped to the last configured delay."""
    raise NotImplementedError("retry policy is implemented in stage 2 (state)")
