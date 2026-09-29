"""Shared terminal-path writes for executions (§9.1, §9.4).

Both the worker and the reconciler finish runs through here, so "the outcome is decided once and
never rewritten by a late message" holds whichever of them gets there first.
"""

from __future__ import annotations

from typing import Any

from ..db.base import utcnow
from ..db.models import TestExecution
from ..domain.enums import AnalysisStatus, ArtifactStatus, ExecutionStatus, Outcome, StepStatus
from ..domain.errors import ErrorCode
from .events import ANALYSIS_REQUEST

#: A failed verdict, an infrastructure fault and a run the platform cut short all need explaining;
#: a pass and an operator's cancel do not.
ANALYSABLE_OUTCOMES = (Outcome.FAILED.value, Outcome.ERROR.value, Outcome.TIMED_OUT.value)


def finalize(
    session,
    execution: TestExecution,
    *,
    outcome: str,
    error_code: str | None = None,
    error_detail: dict[str, Any] | None = None,
    artifact_status: str | None = None,
    extra: dict[str, Any] | None = None,
    close_human_tasks: bool = True,
    enqueue_analysis: bool = True,
) -> TestExecution:
    """Drive an execution to FINISHED with a decided outcome. Idempotent, safe to re-run."""
    from ..repositories.executions import ExecutionRepository
    from ..repositories.outbox import OutboxRepository

    repo = ExecutionRepository(session, execution.tenant_id)
    locked = repo.load(execution.id, for_update=True)
    if locked.status == ExecutionStatus.FINISHED.value:
        return locked
    # A decided outcome is never rewritten: a late worker must not outvote the reconciler (§9.1).
    if locked.outcome and locked.outcome != outcome:
        outcome = str(locked.outcome)

    steps = repo.steps(locked.id)
    for step in steps:
        if step.status == StepStatus.RUNNING.value:
            repo.finish_step(
                locked.id,
                step.step_id,
                status=StepStatus.CANCELLED.value if outcome == Outcome.CANCELLED.value else StepStatus.ERROR.value,
                error_code=error_code or ErrorCode.SESSION_LOST.value,
                error_detail={"reason": "the execution was finalised while this step was in flight"},
            )
    pending = [step for step in repo.steps(locked.id) if step.status == StepStatus.PENDING.value]
    if pending:
        # Any step that never started is skipped, including the first one in the list.
        repo.skip_pending_after(locked.id, from_step_no=min(step.step_no for step in pending) - 1)

    if close_human_tasks:
        close_human_tasks_for(session, locked, outcome)

    if locked.status != ExecutionStatus.FINALIZING.value:
        locked = repo.transition(
            locked.id,
            to_status=ExecutionStatus.FINALIZING.value,
            outcome=outcome,
            error_code=error_code,
            error_detail=error_detail,
            extra=extra,
        )

    values: dict[str, Any] = {
        "artifact_status": artifact_status or locked.artifact_status or ArtifactStatus.COMPLETE.value,
        "ended_at": utcnow(),
    }
    if enqueue_analysis and outcome in ANALYSABLE_OUTCOMES:
        values["analysis_status"] = AnalysisStatus.PENDING.value
    final = repo.transition(
        locked.id,
        to_status=ExecutionStatus.FINISHED.value,
        outcome=outcome,
        error_code=locked.error_code or error_code,
        extra=values,
        event_payload={"artifact_status": values["artifact_status"]},
    )
    if values.get("analysis_status") == AnalysisStatus.PENDING.value:
        OutboxRepository(session, final.tenant_id).enqueue(
            aggregate_id=final.id,
            event_type=ANALYSIS_REQUEST,
            payload={"execution_id": final.id, "tenant_id": final.tenant_id, "project_id": final.project_id},
            discriminator=f"{ANALYSIS_REQUEST}:{final.id}:{final.state_version}",
        )
    return final


def close_human_tasks_for(session, execution: TestExecution, outcome: str) -> None:
    """A finishing execution never leaves an open human task behind (§10.3)."""
    from ..domain.enums import HumanTaskStatus
    from ..repositories.human import HumanTaskRepository

    repo = HumanTaskRepository(session, execution.tenant_id)
    task = repo.active_for(execution.id)
    if task is None:
        return
    repo.finish(
        task.id,
        status=HumanTaskStatus.CANCELLED.value if outcome == Outcome.CANCELLED.value else HumanTaskStatus.EXPIRED.value,
        note="execution finalised while the task was open",
    )
