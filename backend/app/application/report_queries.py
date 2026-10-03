"""Execution, step and report reads that never load a whole run (§8.2, §11).

`build_report` reads every step, artifact, human task and analysis row a run produced and assembles the
document the console renders. That is precisely what §8.2 forbids on this path: a run of 200 steps with
thousands of evidence rows would be loaded to answer a question about twenty of them, and trimming the
output afterwards bounds the *answer* without bounding the *cost*. So every read here is its own bounded
statement - counts come back from a SQL aggregate, the newest analysis is a `LIMIT 1`, failure references
are `LIMIT 5`, evidence references are limited to the steps on the page being answered, and a step page
selects the columns it is actually going to project.

The last point is the one that is easy to get wrong: when a project's policy keeps report details to
itself, the statements here do not read `description`, `error_detail` or `locator_attempts` at all. A
projection that discards a sensitive column is still a query that fetched it.

What comes back is stored facts rather than wire objects, as in `case_queries`: which of those facts may
leave the platform is the adapter's policy decision (§5.5), and this layer has no business making it.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import Select, case, func, null, select
from sqlalchemy.orm import Session

from ..db.models import Artifact, FailureAnalysis, HumanTask, StepExecution, TestExecution
from ..domain.enums import ACTIVE_HUMAN_STATUSES, StepStatus, UploadStatus
from ..repositories.base import keyset_later

#: §11 - the report references at most five failed steps; everything past them is the step page's job.
MAX_FAILURE_STEPS = 5
#: §8.2 - evidence references are read per step, and this is how many a step may carry into a page.
MAX_REFS_PER_STEP = 5
#: A step that says the run did not do what the case asked (§12.1). Cancelled and skipped say something else.
FAILURE_STATUSES = (StepStatus.FAILED.value, StepStatus.ERROR.value)
#: The step columns that are report content rather than state, so they are not even read when details are
#: off. `error_detail` is one of them, and its two prose keys come out of it here rather than in SQL.
GATED_STEP_COLUMNS = ("description", "locator_attempts", "error_detail", "resume_phase")


@dataclass(frozen=True)
class HumanTaskRow:
    """The open human task of a run: identifier, stable reason code and deadline, and nothing else (§7.3)."""

    task_id: str
    step_id: str
    reason: str
    deadline: datetime | None


@dataclass(frozen=True)
class ExecutionFacts:
    """One run as the reads above assemble it: its own columns, its step counts, the person it is waiting on."""

    execution_id: str
    tenant_id: str
    project_id: str
    case_id: str
    revision_id: str
    compile_artifact_id: str
    environment_revision_id: str | None
    status: str
    outcome: str | None
    error_code: str | None
    state_version: int
    evidence_mode: str
    browser: str
    cleanup_status: str
    artifact_status: str
    analysis_status: str
    ir_digest: str | None
    created_at: datetime | None
    started_at: datetime | None
    ended_at: datetime | None
    active_ms: int
    human_ms: int
    human_tasks_used: int
    step_counts: dict[str, int]
    human_task: HumanTaskRow | None


@dataclass(frozen=True)
class FailureRow:
    """One failed step, with the prose fields left out of the statement when the policy says to leave them out."""

    step_id: str
    step_no: int
    action: str
    status: str
    duration_ms: int | None
    error_code: str | None
    description: str | None
    expected: str | None
    actual: str | None


@dataclass(frozen=True)
class StepRow:
    """One step of a run, as far as this page's policy let it be read (§6.6)."""

    step_id: str
    step_no: int
    action: str
    status: str
    duration_ms: int | None
    error_code: str | None
    description: str | None
    locator_attempts: list[dict[str, Any]]
    expected: str | None
    actual: str | None
    resume_phase: str | None
    artifact_refs: list[dict[str, Any]]
    position: tuple[str, str]


#: The execution columns these reads answer from. Three are deliberately absent: `ir` and `snapshot` are the
#: two largest JSON documents on the row and no read here needs either (§8.2), and `error_detail` is the
#: run's own failure prose, where only the stable code may leave the platform (§11).
_EXECUTION_COLUMNS = select(
    TestExecution.id,
    TestExecution.tenant_id,
    TestExecution.project_id,
    TestExecution.case_id,
    TestExecution.revision_id,
    TestExecution.compile_artifact_id,
    TestExecution.environment_revision_id,
    TestExecution.status,
    TestExecution.outcome,
    TestExecution.error_code,
    TestExecution.state_version,
    TestExecution.evidence_mode,
    TestExecution.browser,
    TestExecution.cleanup_status,
    TestExecution.artifact_status,
    TestExecution.analysis_status,
    TestExecution.ir_digest,
    TestExecution.created_at,
    TestExecution.started_at,
    TestExecution.ended_at,
    TestExecution.active_ms,
    TestExecution.human_ms,
    TestExecution.human_tasks_used,
)


def execution_facts(session: Session, *, tenant_id: str, execution_id: str) -> ExecutionFacts | None:
    """One run's state, its step counts and its open human task - three bounded statements, one connection.

    They are separate statements because an aggregate cannot ride along on the row it counts, and each is
    bounded on its own: the run is looked up by id, the counts are one row per status, and the task is a
    `LIMIT 1` behind the partial unique index that guarantees a run has at most one open one.
    """
    row = session.execute(
        _EXECUTION_COLUMNS.where(TestExecution.tenant_id == tenant_id, TestExecution.id == execution_id).limit(1)
    ).first()
    if row is None:
        return None
    return ExecutionFacts(
        execution_id=str(row.id),
        tenant_id=str(row.tenant_id),
        project_id=str(row.project_id),
        case_id=str(row.case_id),
        revision_id=str(row.revision_id),
        compile_artifact_id=str(row.compile_artifact_id),
        environment_revision_id=_text(row.environment_revision_id),
        status=str(row.status),
        outcome=_text(row.outcome),
        error_code=_text(row.error_code),
        state_version=int(row.state_version or 1),
        evidence_mode=str(row.evidence_mode),
        browser=str(row.browser),
        cleanup_status=str(row.cleanup_status),
        artifact_status=str(row.artifact_status),
        analysis_status=str(row.analysis_status),
        ir_digest=_text(row.ir_digest),
        created_at=row.created_at,
        started_at=row.started_at,
        ended_at=row.ended_at,
        active_ms=int(row.active_ms or 0),
        human_ms=int(row.human_ms or 0),
        human_tasks_used=int(row.human_tasks_used or 0),
        step_counts=step_counts(session, tenant_id=tenant_id, execution_id=execution_id),
        human_task=open_human_task(session, tenant_id=tenant_id, execution_id=execution_id),
    )


def step_count_statement(*, tenant_id: str, execution_id: str) -> Select:
    """The aggregate on its own, for the same reason as the page: §11's index contract is a plan claim."""
    return (
        select(StepExecution.status, func.count())
        .where(StepExecution.tenant_id == tenant_id, StepExecution.execution_id == execution_id)
        .group_by(StepExecution.status)
    )


def step_counts(session: Session, *, tenant_id: str, execution_id: str) -> dict[str, int]:
    """How far a run has got, as one row per step status however many steps it has (§6.6).

    Counted in the database rather than assembled from ORM rows, which is what lets a run of two hundred
    steps be reported at the cost of the handful of statuses it used.
    """
    return {
        str(status): int(count)
        for status, count in session.execute(step_count_statement(tenant_id=tenant_id, execution_id=execution_id))
    }


def human_task_statement(*, tenant_id: str, execution_id: str) -> Select:
    """The open task's row, with the columns that may leave the platform already chosen (§7.3)."""
    return (
        select(HumanTask.id, HumanTask.step_id, HumanTask.reason, HumanTask.deadline)
        .where(
            HumanTask.tenant_id == tenant_id,
            HumanTask.execution_id == execution_id,
            HumanTask.status.in_(list(ACTIVE_HUMAN_STATUSES)),
        )
        .order_by(HumanTask.created_at.desc(), HumanTask.id.desc())
        .limit(1)
    )


def open_human_task(session: Session, *, tenant_id: str, execution_id: str) -> HumanTaskRow | None:
    """The task a run is waiting on, if it is waiting on a person (§7.3).

    `detail` is not selected: it is the operator's own description of what happened on the page, which is
    report content rather than a state identifier. `pause_token` is a control credential and could not be
    selected by any read on this path.
    """
    row = session.execute(human_task_statement(tenant_id=tenant_id, execution_id=execution_id)).first()
    return (
        None
        if row is None
        else HumanTaskRow(
            task_id=str(row.id), step_id=str(row.step_id), reason=str(row.reason), deadline=row.deadline
        )
    )


def analysis_type_statement(*, tenant_id: str, execution_id: str) -> Select:
    """The newest analysis row's category, ordered so "newest" is the revision and not the clock (§12.3)."""
    return (
        select(FailureAnalysis.failure_type)
        .where(FailureAnalysis.tenant_id == tenant_id, FailureAnalysis.execution_id == execution_id)
        .order_by(FailureAnalysis.revision.desc())
        .limit(1)
    )


def latest_failure_type(session: Session, *, tenant_id: str, execution_id: str) -> str | None:
    """The category of the newest analysis attempt, or nothing (§8.2: the last row is a `LIMIT 1`).

    A failed model pass leaves the rule classification on that same row rather than erasing it, so the
    newest revision is the right one to read whatever its status; whether an *answer* exists is
    `execution.analysis_status`'s job, and the two are reported side by side (§12.3).
    """
    return _text(session.scalar(analysis_type_statement(tenant_id=tenant_id, execution_id=execution_id)))


def failure_reference_statement(
    *, tenant_id: str, execution_id: str, details: bool, limit: int = MAX_FAILURE_STEPS
) -> Select:
    """The same statement the reference read runs, so its order and its index use can be asserted (§11)."""
    return (
        _failure_selection(details)
        .where(
            StepExecution.tenant_id == tenant_id,
            StepExecution.execution_id == execution_id,
            StepExecution.status.in_(list(FAILURE_STATUSES)),
        )
        .order_by(StepExecution.step_no.asc(), StepExecution.id.asc())
        .limit(limit)
    )


def failure_steps(session: Session, *, tenant_id: str, execution_id: str, details: bool) -> list[FailureRow]:
    """The first five steps that failed, oldest first, and not one column more than the policy allows (§11).

    Five, not all: §11 caps a report's failure summaries so an assistant reads the shape of a red run in one
    call, and the rest of them is what the step page is for. `step_counts` carries the true totals, so the
    cap is visible as a cap rather than reading as "these were the only failures".
    """
    statement = failure_reference_statement(tenant_id=tenant_id, execution_id=execution_id, details=details)
    result = []
    for row in session.execute(statement):
        # `description` is already the NULL this statement selected when the policy keeps details to itself,
        # but the error document is a whole JSON column: reading a key out of it at all is what §8.2 says
        # not to do, so the guard is here rather than left to the projection.
        detail = row.error_detail if details else None
        result.append(
            FailureRow(
                step_id=str(row.step_id),
                step_no=int(row.step_no),
                action=str(row.action),
                status=str(row.status),
                duration_ms=None if row.duration_ms is None else int(row.duration_ms),
                error_code=_text(row.error_code),
                description=_text(row.description),
                expected=_detail_value(detail, "expected"),
                actual=_detail_value(detail, "actual"),
            )
        )
    return result


def step_page(
    session: Session,
    *,
    tenant_id: str,
    execution_id: str,
    limit: int,
    after_step_no: int | None = None,
    after_id: str | None = None,
    details: bool = False,
) -> list[StepRow]:
    """One page of a run's steps in `step_no ASC, id ASC` order, with this page's evidence references (§6.6).

    The order is the execution's own: a reader walking a run wants step 1 before step 2, and the id is only
    the tiebreaker that makes the keyset total (§11). `expected` and `actual` are keys of the step's error
    document rather than columns of their own, so they come out after the row is read - the bound that
    matters is that at most one page of those documents is read at all, which the `limit` gives (§8.2).
    """
    statement = _step_selection(details).where(
        StepExecution.tenant_id == tenant_id, StepExecution.execution_id == execution_id
    ).order_by(StepExecution.step_no.asc(), StepExecution.id.asc()).limit(limit)
    if after_step_no is not None and after_id is not None:
        statement = statement.where(
            keyset_later(StepExecution.step_no, StepExecution.id, after=after_step_no, after_id=after_id)
        )
    rows = list(session.execute(statement))
    refs = artifact_refs(
        session,
        tenant_id=tenant_id,
        execution_id=execution_id,
        step_ids=[str(row.step_id) for row in rows],
    )
    return [_step_row(row, refs.get(str(row.step_id), [])) for row in rows]


def step_page_statement(
    *,
    tenant_id: str,
    execution_id: str,
    limit: int,
    after_step_no: int | None = None,
    after_id: str | None = None,
    details: bool = False,
) -> Select:
    """The page's statement, built separately so §11's ordering and index contract can be asserted on it."""
    statement = _step_selection(details).where(
        StepExecution.tenant_id == tenant_id, StepExecution.execution_id == execution_id
    ).order_by(StepExecution.step_no.asc(), StepExecution.id.asc()).limit(limit)
    if after_step_no is not None and after_id is not None:
        statement = statement.where(
            keyset_later(StepExecution.step_no, StepExecution.id, after=after_step_no, after_id=after_id)
        )
    return statement


def artifact_summary_statement(*, tenant_id: str, execution_id: str) -> Select:
    """One row per evidence kind, with the totals already added up (§8.2)."""
    return (
        select(
            Artifact.kind,
            func.count().label("total"),
            func.coalesce(func.sum(Artifact.size), 0).label("bytes"),
            func.coalesce(
                func.sum(case((Artifact.upload_status != UploadStatus.READY.value, 1), else_=0)), 0
            ).label("pending"),
            func.coalesce(func.sum(case((Artifact.publish_allowed.is_(False), 1), else_=0)), 0).label(
                "withheld"
            ),
        )
        .where(Artifact.tenant_id == tenant_id, Artifact.execution_id == execution_id)
        .group_by(Artifact.kind)
        .order_by(Artifact.kind.asc())
    )


def artifact_summary(session: Session, *, tenant_id: str, execution_id: str) -> dict[str, Any]:
    """Evidence completeness as one grouped statement: how much there is, and how much of it is readable (§8.2).

    Never a list of every artifact. The report says whether the evidence is whole and points at the console;
    the identifiers it lists are the ones that belong to the steps on the page that asked for them.
    """
    rows = list(session.execute(artifact_summary_statement(tenant_id=tenant_id, execution_id=execution_id)))
    return {
        "total": sum(int(row.total) for row in rows),
        "bytes": sum(int(row.bytes) for row in rows),
        "by_kind": {str(row.kind): int(row.total) for row in rows},
        "not_ready": sum(int(row.pending) for row in rows),
        "publish_withheld": sum(int(row.withheld) for row in rows),
        "download": False,
    }


def artifact_reference_statement(
    *, tenant_id: str, execution_id: str, step_ids: Sequence[str]
) -> Select:
    """The ranked reference query, built apart so its bound and its plan can be asserted (§11)."""
    rank = (
        func.row_number()
        .over(
            partition_by=Artifact.step_id,
            order_by=(Artifact.created_at.asc(), Artifact.id.asc()),
        )
        .label("ref_rank")
    )
    inner = (
        select(
            Artifact.id,
            Artifact.step_id,
            Artifact.kind,
            Artifact.size,
            Artifact.upload_status,
            Artifact.publish_allowed,
            rank,
        )
        .where(
            Artifact.tenant_id == tenant_id,
            Artifact.execution_id == execution_id,
            Artifact.step_id.in_(list(step_ids)),
        )
        .subquery()
    )
    return (
        select(
            inner.c.id,
            inner.c.step_id,
            inner.c.kind,
            inner.c.size,
            inner.c.upload_status,
            inner.c.publish_allowed,
        )
        .where(inner.c.ref_rank <= MAX_REFS_PER_STEP)
        .order_by(inner.c.step_id.asc(), inner.c.ref_rank.asc())
        .limit(len(step_ids) * MAX_REFS_PER_STEP)
    )


def artifact_refs(
    session: Session, *, tenant_id: str, execution_id: str, step_ids: Sequence[str]
) -> dict[str, list[dict[str, Any]]]:
    """Evidence references for the steps on this page, at most five each, in the steps' own order (§8.2).

    The rank is a window function rather than a page-sized fetch because a run whose first step took a
    screenshot twenty times would otherwise consume the whole budget and leave the later steps of the page
    with nothing. `object_key` is not among the columns read, and could not be: §11 forbids sending a
    storage key, a local path or a URL to a model, and a reference is meant to be pointed at, not fetched.
    """
    if not step_ids:
        return {}
    statement = artifact_reference_statement(
        tenant_id=tenant_id, execution_id=execution_id, step_ids=step_ids
    )
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in session.execute(statement):
        grouped.setdefault(str(row.step_id), []).append(
            {
                "ref": f"artifact:{row.id}",
                "kind": str(row.kind),
                "size": int(row.size or 0),
                "upload_status": str(row.upload_status),
                "publishable": bool(row.publish_allowed),
            }
        )
    return grouped


def _step_selection(details: bool) -> Select:
    """The page's columns, and when the project keeps its details, the gated ones are not asked for at all.

    The four gated columns become a typed `NULL` rather than being left out of the select list, so one
    statement shape serves both answers and a page cannot accidentally project a field it never meant to
    read.

    `error_detail` is read rather than sliced in SQL, and that is not an oversight: every JSON column in
    this schema is stored inside an encryption envelope (`db/encrypted_types.py`), so the value is opaque
    to `json_extract` on SQLite and to `->>` on PostgreSQL, and no database-side path can reach one key of
    it. The bound is therefore the row count - one page, at most a hundred steps - and the adapter clips
    every string it takes out of the document to §11's per-field limit.
    """
    gated = (
        {
            "description": StepExecution.description,
            "locator_attempts": StepExecution.locator_attempts,
            "error_detail": StepExecution.error_detail,
            "resume_phase": StepExecution.resume_phase,
        }
        if details
        else {name: null() for name in GATED_STEP_COLUMNS}
    )
    return select(
        StepExecution.id,
        StepExecution.step_id,
        StepExecution.step_no,
        StepExecution.action,
        StepExecution.status,
        StepExecution.duration_ms,
        StepExecution.error_code,
        *(gated[name].label(name) for name in GATED_STEP_COLUMNS),
    )


def _failure_selection(details: bool) -> Select:
    """The same rule for the report's five failure references (§11)."""
    gated = (
        {"description": StepExecution.description, "error_detail": StepExecution.error_detail}
        if details
        else {name: null() for name in ("description", "error_detail")}
    )
    return select(
        StepExecution.step_id,
        StepExecution.step_no,
        StepExecution.action,
        StepExecution.status,
        StepExecution.duration_ms,
        StepExecution.error_code,
        *(gated[name].label(name) for name in ("description", "error_detail")),
    )


def _step_row(row: Any, refs: list[dict[str, Any]]) -> StepRow:
    return StepRow(
        step_id=str(row.step_id),
        step_no=int(row.step_no),
        action=str(row.action),
        status=str(row.status),
        duration_ms=None if row.duration_ms is None else int(row.duration_ms),
        error_code=_text(row.error_code),
        description=_text(row.description),
        locator_attempts=_dict_list(row.locator_attempts),
        expected=_detail_value(row.error_detail, "expected"),
        actual=_detail_value(row.error_detail, "actual"),
        resume_phase=_text(row.resume_phase),
        artifact_refs=list(refs),
        position=(str(row.step_no), str(row.id)),
    )


def _dict_list(value: Any) -> list[dict[str, Any]]:
    return [item for item in (value or []) if isinstance(item, dict)]


def _detail_value(detail: Any, key: str) -> str | None:
    """One key of a step's error document, and nothing else of it.

    The two keys are the assertion's own words - what the case expected and what the page said - which is
    why they are read only on a page whose project sends report details (§5.5). The document's other keys
    are diagnostics, and the adapter projects those from the stable error code instead.
    """
    return _text(detail.get(key)) if isinstance(detail, dict) else None


def _text(value: Any) -> str | None:
    return None if value is None else str(value)
