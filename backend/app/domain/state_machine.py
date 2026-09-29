"""Execution state machine (§9.1). Transitions are advisory here; the repository
applies them as conditional UPDATEs guarded by status, state_version and lease_epoch."""

from __future__ import annotations

from .enums import ExecutionStatus, Outcome, StepStatus

ALLOWED_TRANSITIONS: dict[ExecutionStatus, frozenset[ExecutionStatus]] = {
    ExecutionStatus.CREATED: frozenset({ExecutionStatus.QUEUED, ExecutionStatus.FINALIZING}),
    ExecutionStatus.QUEUED: frozenset({ExecutionStatus.RUNNING, ExecutionStatus.FINALIZING}),
    ExecutionStatus.RUNNING: frozenset({ExecutionStatus.WAIT_HUMAN, ExecutionStatus.FINALIZING}),
    ExecutionStatus.WAIT_HUMAN: frozenset({ExecutionStatus.RUNNING, ExecutionStatus.FINALIZING}),
    ExecutionStatus.FINALIZING: frozenset({ExecutionStatus.FINISHED}),
    ExecutionStatus.FINISHED: frozenset(),
}

#: Statuses in which an outcome must not be set.
ACTIVE_WITHOUT_OUTCOME = frozenset(
    {
        ExecutionStatus.CREATED,
        ExecutionStatus.QUEUED,
        ExecutionStatus.RUNNING,
        ExecutionStatus.WAIT_HUMAN,
    }
)

TERMINAL_STEP_STATUSES = frozenset(
    {
        StepStatus.PASSED,
        StepStatus.FAILED,
        StepStatus.SKIPPED,
        StepStatus.CANCELLED,
        StepStatus.ERROR,
    }
)


def can_transition(current: ExecutionStatus, target: ExecutionStatus) -> bool:
    return target in ALLOWED_TRANSITIONS[current]


def outcome_for_status(status: ExecutionStatus, outcome: Outcome | None) -> Outcome | None:
    """FINISHED keeps its conclusion; active statuses never carry one (§1.2 status/outcome split)."""
    if status in ACTIVE_WITHOUT_OUTCOME:
        return None
    return outcome


def derive_outcome(step_statuses: list[StepStatus], *, cancelled: bool, timed_out: bool, error_code: str | None):
    """First-failure-wins rule for a finishing execution."""
    from .enums import Outcome as _Outcome

    if cancelled:
        return _Outcome.CANCELLED
    if timed_out:
        return _Outcome.TIMED_OUT
    if any(status in (StepStatus.FAILED,) for status in step_statuses):
        return _Outcome.FAILED
    if any(status in (StepStatus.ERROR,) for status in step_statuses):
        return _Outcome.ERROR
    if error_code:
        return _Outcome.ERROR
    if all(status is StepStatus.PASSED for status in step_statuses):
        return _Outcome.PASSED
    return _Outcome.ERROR
