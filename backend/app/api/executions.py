"""Starting runs, watching them and streaming their events (§9.1, §13.2, §13.3, §13.4)."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, StringConstraints
from starlette.concurrency import run_in_threadpool

from ..config import get_settings
from ..db.base import utcnow
from ..db.models import TestExecution
from ..domain.enums import CommandType, ExecutionStatus, Outcome, Permission
from ..domain.errors import ApiError, ErrorCode
from ..observability import current_request, current_tenant, get_logger
from ..reporting.report import build_report, error_detail_view, steps_detail
from ..repositories.cases import CaseRepository, CompileRepository
from ..repositories.executions import ExecutionRepository
from ..repositories.human import CommandRepository
from ..services.executions import ExecutionService, execution_summary
from .deps import (
    Context,
    Ctx,
    Page,
    get_context,
    idempotent,
    new_request_id,
    page_envelope,
    page_params,
    resolve_identity_by_id,
)
from .tickets import DEFAULT_TTL_SECONDS, STREAM, get_ticket_store

router = APIRouter(tags=["executions"])
log = get_logger(__name__)

#: §13.4: the durable journal is the source of truth, so a poll — not a broker — carries the tail.
SSE_POLL_SECONDS = 1.0
SSE_HEARTBEAT_SECONDS = 15.0
SSE_MAX_SECONDS = 1800.0
TERMINAL_STATUSES = frozenset({ExecutionStatus.FINISHED.value})


class ExecutionCreate(BaseModel):
    model_config = {"extra": "forbid"}

    #: §13.3: naming the artifact freezes exactly which IR and which review confirmation is run.
    compile_artifact_id: Annotated[str | None, StringConstraints(max_length=36)] = None
    case_id: Annotated[str | None, StringConstraints(max_length=36)] = None
    revision_id: Annotated[str | None, StringConstraints(max_length=36)] = None
    environment_id: Annotated[str | None, StringConstraints(max_length=36)] = None
    environment_revision_id: Annotated[str | None, StringConstraints(max_length=36)] = None
    variables: dict[str, Any] = Field(default_factory=dict)
    browser: Annotated[str | None, StringConstraints(max_length=24)] = None
    evidence_mode: Annotated[str | None, StringConstraints(max_length=16)] = None


class CancelRequest(BaseModel):
    model_config = {"extra": "forbid"}

    reason: Annotated[str, StringConstraints(strip_whitespace=True, max_length=300)] = "requested"


class PauseRequest(BaseModel):
    model_config = {"extra": "forbid"}

    reason: Annotated[str, StringConstraints(strip_whitespace=True, max_length=300)] = "an operator requested a pause"
    #: Pause before this step id; empty means "before whichever step comes next".
    for_step: Annotated[str | None, StringConstraints(max_length=12)] = None


class RerunRequest(BaseModel):
    model_config = {"extra": "forbid"}

    environment_revision_id: Annotated[str | None, StringConstraints(max_length=36)] = None


# ------------------------------------------------------------------- authorisation


def _load_execution(ctx: Context, session: Any, execution_id: str) -> TestExecution:
    """Foreign and unknown ids answer 404, non-members 403 (§14.1)."""
    execution = ExecutionRepository(session, ctx.tenant_id).by_id(execution_id)
    if execution is None:
        raise ApiError(ErrorCode.NOT_FOUND, "Execution not found in your tenant")
    if not ctx.visible_project(execution.project_id):
        raise ApiError(ErrorCode.FORBIDDEN, "You are not a member of this project")
    return execution


def _authorize_cancel(ctx: Context, execution: TestExecution) -> None:
    project_id = execution.project_id
    if ctx.can(Permission.EXECUTION_CANCEL_ANY, project_id):
        return
    if execution.requested_by == ctx.actor_id and ctx.can(Permission.EXECUTION_CANCEL_OWN, project_id):
        return
    raise ApiError(
        ErrorCode.FORBIDDEN,
        "Only the requester or a lead of this project can cancel the execution",
        details={"requested_by": execution.requested_by},
    )


# ------------------------------------------------------------------------ create


@router.post("/executions", status_code=202)
def create_execution(
    ctx: Ctx,
    body: ExecutionCreate,
    response: Response,
    idempotency_key: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    """Queue a run and return immediately: the scheduler, not this request, pays for the browser (§9.3)."""
    payload = body.model_dump(mode="json")

    def resolve_targets() -> tuple[str, str | None, str | None]:
        """(project_id, case_id, revision_id) from whichever ids the caller named."""
        with ctx.session() as session:
            if body.compile_artifact_id:
                artifact = CompileRepository(session, ctx.tenant_id).by_id(body.compile_artifact_id)
                if artifact is None:
                    raise ApiError(ErrorCode.NOT_FOUND, "Compile artifact not found in your tenant")
                if body.revision_id and body.revision_id != artifact.revision_id:
                    raise ApiError(
                        ErrorCode.SEMANTIC_ERROR,
                        "The named revision is not the one that artifact was compiled from",
                        details={"revision_id": artifact.revision_id},
                    )
                revision = CaseRepository(session, ctx.tenant_id).require_revision(artifact.revision_id)
                return str(artifact.project_id), str(revision.case_id), str(revision.id)
            if body.case_id:
                case = CaseRepository(session, ctx.tenant_id).by_id(body.case_id)
                if case is None:
                    raise ApiError(ErrorCode.NOT_FOUND, "Case not found in your tenant")
                return str(case.project_id), str(case.id), body.revision_id or case.current_revision_id
            raise ApiError(
                ErrorCode.VALIDATION_ERROR,
                "Name a compile_artifact_id or a case_id to run",
                details={"allowed": ["compile_artifact_id", "case_id"]},
            )

    project_id, case_id, revision_id = resolve_targets()
    ctx.project(project_id, permission=Permission.EXECUTION_RUN)
    service = ExecutionService(ctx.settings)

    def action() -> tuple[str, dict[str, Any]]:
        execution = service.create(
            tenant_id=ctx.tenant_id,
            project_id=project_id,
            case_id=str(case_id),
            revision_id=revision_id,
            environment_id=body.environment_id,
            environment_revision_id=body.environment_revision_id,
            compile_artifact_id=body.compile_artifact_id,
            requested_by=ctx.actor_id,
            run_variables=body.variables,
            evidence_mode=body.evidence_mode,
            browser=body.browser,
        )
        with ctx.session() as session:
            ctx.audit(
                session,
                operation="execution.create",
                resource_type="execution",
                resource_id=execution.id,
                project_id=execution.project_id,
                detail={
                    "case_id": execution.case_id,
                    "revision_id": execution.revision_id,
                    "compile_artifact_id": execution.compile_artifact_id,
                    "environment_revision_id": execution.environment_revision_id,
                    "browser": execution.browser,
                    "evidence_mode": execution.evidence_mode,
                    "variables": sorted((body.variables or {}).keys()),
                },
            )
            session.commit()
        return execution.id, _run_view(execution)

    view = idempotent(ctx, route="POST /executions", key=idempotency_key, payload=payload, action=action)
    response.headers["Location"] = f"/api/v1/executions/{view['id']}"
    return view


def _run_view(execution: TestExecution) -> dict[str, Any]:
    """The §13.3 response shape, with the links the monitor page needs."""
    return {
        "id": execution.id,
        "status": execution.status,
        "outcome": execution.outcome,
        "report_url": f"/api/v1/executions/{execution.id}/report",
        "events_url": f"/api/v1/executions/{execution.id}/events",
        "steps_url": f"/api/v1/executions/{execution.id}/steps",
        "project_id": execution.project_id,
        "case_id": execution.case_id,
        "trigger": execution.trigger,
    }


# --------------------------------------------------------------------------- read


@router.get("/projects/{project_id}/executions")
def list_executions(
    ctx: Ctx,
    project_id: str,
    page: Annotated[Page, Depends(page_params)],
    case_id: Annotated[str | None, Query(max_length=36)] = None,
    environment_id: Annotated[str | None, Query(max_length=36)] = None,
    status: Annotated[list[str] | None, Query(description="Repeatable status filter")] = None,
    outcome: Annotated[list[str] | None, Query(description="Repeatable outcome filter")] = None,
    since: Annotated[datetime | None, Query()] = None,
    until: Annotated[datetime | None, Query()] = None,
) -> dict[str, Any]:
    ctx.project(project_id)
    _reject_unknown(status, ExecutionStatus, "status")
    _reject_unknown(outcome, Outcome, "outcome")
    rows, total = ExecutionService(ctx.settings).list(
        tenant_id=ctx.tenant_id,
        project_id=project_id,
        case_id=case_id,
        environment_id=environment_id,
        statuses=tuple(status or ()),
        outcomes=tuple(outcome or ()),
        since=since,
        until=until,
        limit=page.limit,
        offset=page.offset,
    )
    return page_envelope([execution_summary(row) for row in rows], page, total=total)


def _reject_unknown(values: list[str] | None, enum: Any, field_name: str) -> None:
    """A filter spelling that matches nothing is a typo, and a typo should say so (§13.1)."""
    if not values:
        return
    known = {item.value for item in enum}
    stray = [value for value in values if value not in known]
    if stray:
        raise ApiError(
            ErrorCode.VALIDATION_ERROR,
            f"Unknown {field_name} filter: {', '.join(stray[:5])}",
            details={"allowed": sorted(known)},
        )


@router.get("/executions/{execution_id}")
def get_execution(ctx: Ctx, execution_id: str) -> dict[str, Any]:
    """Live aggregate state plus the step summary the monitor timeline renders (§13.2)."""
    with ctx.session() as session:
        execution = _load_execution(ctx, session, execution_id)
        repo = ExecutionRepository(session, ctx.tenant_id)
        steps = repo.steps(execution.id)
        human_task = _active_human_task(session, ctx.tenant_id, execution.id)
        view = execution_summary(execution)
        view.update(
            {
                "state_version": int(execution.state_version or 0),
                "last_event_seq": int(execution.last_event_seq or 0),
                "environment_id": execution.environment_id,
                "environment_revision_id": execution.environment_revision_id,
                "compile_artifact_id": execution.compile_artifact_id,
                "revision_id": execution.revision_id,
                "requested_by": execution.requested_by,
                "error": None
                if not execution.error_code
                else {"code": execution.error_code, "detail": error_detail_view(execution.error_detail)},
                "step_summary": {
                    "total": len(steps),
                    "by_status": _count_by(steps, "status"),
                    "passed": sum(1 for row in steps if row.status == "PASSED"),
                    "failed": sum(1 for row in steps if row.status in ("FAILED", "ERROR")),
                },
                "steps": [
                    {
                        "step_id": row.step_id,
                        "step_no": row.step_no,
                        "action": row.action,
                        "description": row.description,
                        "status": row.status,
                        "duration_ms": int(row.duration_ms or 0),
                        "error_code": row.error_code,
                    }
                    for row in steps
                ],
                "human_task": human_task,
                "report_url": f"/api/v1/executions/{execution.id}/report",
                "events_url": f"/api/v1/executions/{execution.id}/events",
            }
        )
    return view


def _count_by(rows: list[Any], attribute: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        key = str(getattr(row, attribute, "") or "")
        counts[key] = counts.get(key, 0) + 1
    return counts


def _active_human_task(session: Any, tenant_id: str, execution_id: str) -> dict[str, Any] | None:
    from ..db.base import coerce_utc
    from ..repositories.human import HumanTaskRepository

    task = HumanTaskRepository(session, tenant_id).active_for(execution_id)
    if task is None:
        return None
    deadline = coerce_utc(task.deadline)
    return {
        "human_task_id": task.id,
        "step_id": task.step_id,
        "reason": task.reason,
        "status": task.status,
        "controller_id": task.assignee_id,
        "deadline": deadline.isoformat(),
        "remaining_seconds": max(0, int((deadline - utcnow()).total_seconds())),
    }


@router.get("/executions/{execution_id}/steps")
def get_execution_steps(
    ctx: Ctx,
    execution_id: str,
    page: Annotated[Page, Depends(page_params)],
) -> dict[str, Any]:
    with ctx.session() as session:
        execution = _load_execution(ctx, session, execution_id)
        rows = steps_detail(session, execution, tenant_id=ctx.tenant_id)
    window = rows[page.offset : page.offset + page.limit]
    return page_envelope(window, page, total=len(rows))


@router.get("/executions/{execution_id}/report")
def get_execution_report(ctx: Ctx, execution_id: str) -> dict[str, Any]:
    with ctx.session() as session:
        execution = _load_execution(ctx, session, execution_id)
        return build_report(session, execution, settings=ctx.settings, tenant_id=ctx.tenant_id)


# ---------------------------------------------------------------------- commands


@router.post("/executions/{execution_id}/cancel", status_code=202)
def cancel_execution(
    ctx: Ctx,
    execution_id: str,
    body: CancelRequest,
    idempotency_key: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    """202 with the *current* state; once finished the same call keeps answering with the final one."""
    with ctx.session() as session:
        execution = _load_execution(ctx, session, execution_id)
        _authorize_cancel(ctx, execution)
        project_id = execution.project_id

    service = ExecutionService(ctx.settings)

    def action() -> tuple[str, dict[str, Any]]:
        stopped = service.cancel(
            tenant_id=ctx.tenant_id,
            execution_id=execution_id,
            requested_by=ctx.actor_id,
            reason=body.reason,
        )
        with ctx.session() as session:
            ctx.audit(
                session,
                operation="execution.cancel",
                resource_type="execution",
                resource_id=execution_id,
                project_id=project_id,
                detail={"reason": body.reason[:300]},
            )
            session.commit()
        return stopped.id, {"id": stopped.id}

    idempotent(
        ctx,
        route=f"POST /executions/{execution_id}/cancel",
        key=idempotency_key,
        payload=body.model_dump(mode="json"),
        action=action,
    )
    with ctx.session() as session:
        # Re-read after the command: the caller asked what the run looks like now, not what it looked
        # like when the key was first used.
        return execution_summary(_load_execution(ctx, session, execution_id))


@router.post("/executions/{execution_id}/pause", status_code=202)
def pause_execution(
    ctx: Ctx,
    execution_id: str,
    body: PauseRequest,
    idempotency_key: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    """Ask the lease holder to stop at the next safe boundary and open a human task (§10.2, §13.2)."""
    with ctx.session() as session:
        execution = _load_execution(ctx, session, execution_id)
        ctx.require(Permission.HUMAN_CONTROL, execution.project_id)
        project_id = execution.project_id
        if execution.status != ExecutionStatus.RUNNING.value:
            raise ApiError(
                ErrorCode.CONFLICT,
                "A pause can only be requested while the execution is running",
                details={"status": execution.status},
            )
        payload = {"reason": body.reason, "for_step": body.for_step, "requested_by": ctx.actor_id}
        # The request is only useful for the step that is running now: anything older than the
        # longest permitted step would pause a step nobody meant (§9.5).
        horizon = utcnow() + timedelta(milliseconds=float(ctx.settings.step_timeout_max_ms))
        command = CommandRepository(session, ctx.tenant_id).enqueue(
            project_id=project_id,
            execution_id=execution_id,
            command_type=CommandType.PAUSE.value,
            dedupe_key=f"pause:{ctx.actor_id}:{idempotency_key or new_request_id()}"[:80],
            requested_by=ctx.actor_id,
            payload=payload,
            expires_at=horizon,
        )
        ctx.audit(
            session,
            operation="execution.pause",
            resource_type="execution",
            resource_id=execution_id,
            project_id=project_id,
            detail={"command_id": command.id, "for_step": body.for_step, "reason": body.reason[:300]},
        )
        session.commit()
        return {"command_id": command.id, "status": command.status, "execution_status": execution.status}


@router.post("/executions/{execution_id}/rerun", status_code=202)
def rerun_execution(
    ctx: Ctx,
    execution_id: str,
    body: RerunRequest,
    response: Response,
    idempotency_key: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    """A new execution that records which run it replayed; the original evidence stays put (§12.4)."""
    with ctx.session() as session:
        previous = _load_execution(ctx, session, execution_id)
        ctx.require(Permission.EXECUTION_RUN, previous.project_id)
        project_id = previous.project_id
    service = ExecutionService(ctx.settings)

    def action() -> tuple[str, dict[str, Any]]:
        execution = service.retry(
            tenant_id=ctx.tenant_id,
            execution_id=execution_id,
            requested_by=ctx.actor_id,
            environment_revision_id=body.environment_revision_id,
        )
        with ctx.session() as session:
            ctx.audit(
                session,
                operation="execution.rerun",
                resource_type="execution",
                resource_id=execution.id,
                project_id=project_id,
                detail={"retry_of": execution_id},
            )
            session.commit()
        return execution.id, _run_view(execution)

    view = idempotent(
        ctx,
        route=f"POST /executions/{execution_id}/rerun",
        key=idempotency_key,
        payload=body.model_dump(mode="json"),
        action=action,
    )
    response.headers["Location"] = f"/api/v1/executions/{view['id']}"
    return view


@router.post("/executions/{execution_id}/analysis", status_code=202)
def analyze_execution(
    ctx: Ctx,
    execution_id: str,
    idempotency_key: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    """Re-ask the failure analyser without touching the browser again (§12.3)."""
    with ctx.session() as session:
        execution = _load_execution(ctx, session, execution_id)
        project_id = execution.project_id
    service = ExecutionService(ctx.settings)

    def action() -> tuple[str, dict[str, Any]]:
        analyzed = service.analyze(tenant_id=ctx.tenant_id, execution_id=execution_id, requested_by=ctx.actor_id)
        with ctx.session() as session:
            ctx.audit(
                session,
                operation="execution.analysis",
                resource_type="execution",
                resource_id=analyzed,
                project_id=project_id,
                detail={},
            )
            session.commit()
        return analyzed, {"execution_id": analyzed, "analysis_status": "PENDING"}

    return idempotent(
        ctx,
        route=f"POST /executions/{execution_id}/analysis",
        key=idempotency_key,
        payload={},
        action=action,
    )


# --------------------------------------------------------------------------- SSE


def _stream_context(
    request: Request, authorization: str | None, tenant_hint: str | None, ticket: str | None, execution_id: str
) -> Context:
    """EventSource cannot send a header, so a redeemed stream ticket opens the same session (§14.3).

    The ticket is consumed here and only here: it proves *who* is connecting, and the authority is
    rebuilt from the database on top of that, so revoking a grant still bites immediately.
    """
    if not ticket:
        return get_context(request, authorization, tenant_hint)
    stored = get_ticket_store().redeem(STREAM, ticket, resource_id=execution_id)
    settings = get_settings()
    identity = resolve_identity_by_id(settings, stored.actor_id, stored.tenant_id)
    request_id = getattr(request.state, "request_id", None) or new_request_id()
    current_tenant.set(identity.tenant_id)
    current_request.set(request_id)
    return Context(identity=identity, request_id=request_id, settings=settings)


@router.post("/executions/{execution_id}/events/ticket")
def issue_stream_ticket(ctx: Ctx, execution_id: str) -> dict[str, Any]:
    with ctx.session() as session:
        execution = _load_execution(ctx, session, execution_id)
        project_id = execution.project_id
    ticket = get_ticket_store().issue(
        STREAM,
        tenant_id=ctx.tenant_id,
        actor_id=ctx.actor_id,
        resource_id=execution_id,
        project_id=project_id,
        ttl_seconds=DEFAULT_TTL_SECONDS,
    )
    return {
        "ticket": ticket.value,
        "expires_in_seconds": DEFAULT_TTL_SECONDS,
        "usage": "connect once to GET /api/v1/executions/{id}/events?ticket=…; it is consumed at connect",
        "url": f"/api/v1/executions/{execution_id}/events?ticket={ticket.value}",
    }


def _frame(event: str, data: dict[str, Any], seq: int | None = None) -> str:
    body = json.dumps(data, ensure_ascii=False, separators=(",", ":"), default=str)
    head = f"event: {event}\n"
    if seq is not None:
        head = f"id: {seq}\n" + head
    return f"{head}data: {body}\n\n"


@router.get("/executions/{execution_id}/events")
async def execution_events(
    request: Request,
    execution_id: str,
    authorization: Annotated[str | None, Header()] = None,
    x_tenant_id: Annotated[str | None, Header(alias="X-Tenant-Id")] = None,
    ticket: Annotated[str | None, Query(max_length=64)] = None,
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
) -> StreamingResponse:
    ctx = _stream_context(request, authorization, x_tenant_id, ticket, execution_id)
    with ctx.session() as session:
        execution = _load_execution(ctx, session, execution_id)
        snapshot = {
            "id": execution.id,
            "status": execution.status,
            "outcome": execution.outcome,
            "state_version": int(execution.state_version or 0),
        }
    try:
        after = int(last_event_id or 0)
    except (TypeError, ValueError):
        raise ApiError(ErrorCode.VALIDATION_ERROR, "Last-Event-ID must be the numeric sequence it issued") from None
    service = ExecutionService(ctx.settings)
    tenant_id = ctx.tenant_id

    floor = await run_in_threadpool(service.probe, tenant_id=tenant_id, execution_id=execution_id)
    earliest = floor.get("first_event_seq")
    if after > 0 and earliest is not None and after < earliest - 1:
        # §13.4: the journal no longer holds that point, so say so and let the client refetch.
        async def expired() -> AsyncIterator[str]:
            yield _frame(
                "error",
                {
                    "code": ErrorCode.EVENT_CURSOR_EXPIRED.value,
                    "message": "The event journal no longer holds the requested position; refetch the snapshot",
                    "request_id": ctx.request_id,
                    "details": {"requested_seq": after, "earliest_seq": earliest},
                },
            )

        return StreamingResponse(expired(), media_type="text/event-stream", headers=_SSE_HEADERS)

    async def stream() -> AsyncIterator[str]:
        seq = after
        opened = time.monotonic()
        last_output = time.monotonic()
        yield _frame("stream.started", {"execution_id": execution_id, "resume_seq": seq, **snapshot})
        while True:
            rows = await run_in_threadpool(
                service.events, tenant_id=tenant_id, execution_id=execution_id, after_seq=seq
            )
            for row in rows:
                seq = int(row["seq"])
                payload = dict(row.get("payload") or {})
                payload.setdefault("execution_id", execution_id)
                yield _frame(str(row.get("event") or "event"), payload, seq)
                last_output = time.monotonic()
            probe = await run_in_threadpool(service.probe, tenant_id=tenant_id, execution_id=execution_id)
            if not rows and time.monotonic() - last_output >= SSE_HEARTBEAT_SECONDS:
                yield ": keep-alive\n\n"
                last_output = time.monotonic()
            if probe["status"] in TERMINAL_STATUSES and seq >= int(probe["last_event_seq"]):
                yield _frame(
                    "stream.end",
                    {
                        "execution_id": execution_id,
                        "resume_seq": seq,
                        "reason": "execution_finished",
                        **{key: probe[key] for key in ("status", "outcome", "state_version")},
                    },
                )
                return
            if time.monotonic() - opened >= SSE_MAX_SECONDS:
                yield _frame(
                    "stream.end", {"execution_id": execution_id, "resume_seq": seq, "reason": "stream_window_elapsed"}
                )
                return
            if await request.is_disconnected():
                return
            await asyncio.sleep(SSE_POLL_SECONDS)

    return StreamingResponse(stream(), media_type="text/event-stream", headers=_SSE_HEADERS)


#: `no-store` matters: a cached event stream would replay one tenant's run to another (§14.2).
_SSE_HEADERS = {
    "Cache-Control": "no-cache, no-store, must-revalidate",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}
