"""Quality roll-ups (§12.4).

The pass rate is deliberately `PASSED / (PASSED + FAILED)`: an infrastructure error, a queue timeout
or a cancellation is *listed next to* the rate together with the total, never folded into it, so a
broken browser cannot be read as a passing or a failing product.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from ..db.base import coerce_utc, utcnow
from ..db.models import FailureAnalysis, StepExecution, TestExecution
from ..domain.enums import AnalysisStatus, ExecutionStatus, Outcome

#: Outcomes that say something about the platform rather than the product under test.
INFRA_OUTCOMES = (Outcome.ERROR.value, Outcome.TIMED_OUT.value, Outcome.CANCELLED.value)


def scope_conditions(
    tenant_id: str,
    *,
    project_id: str | None = None,
    environment_id: str | None = None,
    case_id: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    finished_only: bool = True,
) -> list[Any]:
    conditions: list[Any] = [TestExecution.tenant_id == tenant_id]
    if finished_only:
        conditions.append(TestExecution.status == ExecutionStatus.FINISHED.value)
        conditions.append(TestExecution.outcome.isnot(None))
    if project_id:
        conditions.append(TestExecution.project_id == project_id)
    if environment_id:
        conditions.append(TestExecution.snapshot["environment_id"].as_string() == environment_id)
    if case_id:
        conditions.append(TestExecution.case_id == case_id)
    if since is not None:
        conditions.append(TestExecution.created_at >= since)
    if until is not None:
        conditions.append(TestExecution.created_at <= until)
    return conditions


def execution_stats(
    session: Session,
    *,
    tenant_id: str,
    project_id: str | None = None,
    environment_id: str | None = None,
    case_id: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
) -> dict[str, Any]:
    conditions = scope_conditions(
        tenant_id, project_id=project_id, environment_id=environment_id, case_id=case_id, since=since, until=until
    )
    rows = session.execute(
        select(
            TestExecution.outcome,
            func.count(),
            func.avg(TestExecution.active_ms),
            func.coalesce(func.sum(TestExecution.human_ms), 0),
            func.coalesce(func.sum(TestExecution.human_tasks_used), 0),
        )
        .where(*conditions)
        .group_by(TestExecution.outcome)
    ).all()
    counts = {str(outcome): int(total) for outcome, total, _avg, _human_ms, _human_tasks in rows}
    total = sum(counts.values())
    passed = counts.get(Outcome.PASSED.value, 0)
    failed = counts.get(Outcome.FAILED.value, 0)
    active_times = [
        int(value)
        for value in session.execute(
            select(TestExecution.active_ms)
            .where(*conditions, TestExecution.active_ms.isnot(None))
            .order_by(TestExecution.active_ms.asc())
        )
        .scalars()
        .all()
    ]
    human_runs = int(
        session.execute(
            select(func.count()).select_from(TestExecution).where(*conditions, TestExecution.human_tasks_used > 0)
        ).scalar()
        or 0
    )
    return {
        "window": {"since": _iso(since), "until": _iso(until or utcnow()), "generated_at": _iso(utcnow())},
        "scope": {"project_id": project_id, "environment_id": environment_id, "case_id": case_id},
        "total": total,
        "by_outcome": counts,
        "passed": passed,
        "failed": failed,
        # Fixed denominator: only a verdict against the product counts (§12.4).
        "pass_rate": (passed / (passed + failed)) if (passed + failed) else None,
        "pass_rate_denominator": passed + failed,
        "infrastructure": {outcome: counts.get(outcome, 0) for outcome in INFRA_OUTCOMES},
        "avg_active_ms": _avg(active_times),
        "p95_active_ms": _p95(active_times),
        "human_interventions": human_runs,
        "human_intervention_rate": (human_runs / total) if total else None,
        "human_ms_total": int(sum((row[3] or 0) for row in rows)),
        "locator": locator_stats(session, conditions=conditions),
        "failure_types": failure_type_stats(session, tenant_id=tenant_id, project_id=project_id, since=since),
    }


def locator_stats(session: Session, *, conditions: list[Any]) -> dict[str, Any]:
    """Degradation means the first candidate did not resolve the target (§12.4).

    Every candidate the engine validated is recorded, so two selectors that both resolve the same
    element is the normal explicit path rather than a fallback. Only a candidate that failed to
    resolve, or a winner remembered from an earlier run or found by the vision pass, is degradation.
    """
    scoped = and_(StepExecution.tenant_id == TestExecution.tenant_id, StepExecution.execution_id == TestExecution.id)
    attempts_rows = (
        session.execute(
            select(StepExecution.locator_attempts)
            .join(TestExecution, scoped)
            .where(*conditions, StepExecution.locator_strategy.isnot(None))
        )
        .scalars()
        .all()
    )
    located = len(attempts_rows)
    degraded = sum(1 for attempts in attempts_rows if _is_degraded(list(attempts or [])))
    return {
        "located_steps": located,
        "degraded_steps": degraded,
        "degradation_rate": (degraded / located) if located else None,
    }


#: Attempt outcomes that mean a higher-priority candidate was not usable.
FAILED_ATTEMPTS = frozenset({"no_match", "ambiguous", "not_visible", "wrong_type", "rejected"})


def _is_degraded(attempts: list[Any]) -> bool:
    for attempt in attempts:
        if not isinstance(attempt, dict):
            continue
        if str(attempt.get("outcome")) in FAILED_ATTEMPTS:
            return True
        if str(attempt.get("source") or "explicit") in {"memory", "vision"}:
            return True
    return False


def failure_type_stats(
    session: Session, *, tenant_id: str, project_id: str | None = None, since: datetime | None = None
) -> dict[str, int]:
    """Which diagnoses the runs carried, kept separate from the pass rate (§12.3, §12.4)."""
    conditions = [FailureAnalysis.tenant_id == tenant_id, FailureAnalysis.status == AnalysisStatus.SUCCEEDED.value]
    if project_id:
        conditions.append(FailureAnalysis.project_id == project_id)
    if since is not None:
        conditions.append(FailureAnalysis.created_at >= since)
    rows = session.execute(
        select(FailureAnalysis.failure_type, func.count()).where(*conditions).group_by(FailureAnalysis.failure_type)
    ).all()
    return {str(kind or "UNKNOWN"): int(total) for kind, total in rows}


def _avg(values: list[int]) -> int:
    return int(sum(values) / len(values)) if values else 0


def _p95(values: list[int]) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    position = max(0, min(len(ordered) - 1, round(0.95 * len(ordered)) - 1))
    return int(ordered[position])


def _iso(value: Any) -> str | None:
    if value is None or not hasattr(value, "isoformat"):
        return None
    return coerce_utc(value).isoformat()
