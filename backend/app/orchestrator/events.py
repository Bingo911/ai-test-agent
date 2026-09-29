"""Outbox event names and the task each one turns into (§9.3, §9.4).

Keeping the mapping in one place means the API, the scheduler and the dispatcher cannot drift into
publishing work items nobody consumes.
"""

from __future__ import annotations

from .queue import ANALYZE_TASK, COMPILE_TASK, EXECUTE_TASK

#: Written by the create-execution transaction: it only wakes the scheduler, never a worker.
EXECUTION_QUEUED = "execution.queued"
EXECUTION_EXECUTE = "execution.execute"
COMPILE_REQUEST = "compile.request"
ANALYSIS_REQUEST = "analysis.request"

TASK_FOR_EVENT: dict[str, str] = {
    EXECUTION_QUEUED: "",
    EXECUTION_EXECUTE: EXECUTE_TASK,
    COMPILE_REQUEST: COMPILE_TASK,
    ANALYSIS_REQUEST: ANALYZE_TASK,
}

#: Events with no task behind them: publishing them is just "wake the loop".
WAKE_UP_EVENTS = frozenset({EXECUTION_QUEUED})


def task_for_event(event_type: str) -> str:
    return TASK_FOR_EVENT.get(event_type, "")


def append_event(tenant_id: str, execution_id: str, event_type: str, payload: dict) -> int:
    """Write one row into the per-execution journal the SSE stream replays from (§13.4).

    A journal write is best-effort by design: the state transition it describes is already durable,
    so a subscriber missing one event must be able to catch up by re-reading the execution.
    """
    from ..db.base import get_database
    from ..repositories.executions import ExecutionRepository

    try:
        with get_database().session(tenant_id) as session:
            repo = ExecutionRepository(session, tenant_id)
            execution = repo.load(execution_id)
            seq = repo.append_event(execution, event_type, payload)
            session.commit()
        return seq
    except Exception:
        return 0
