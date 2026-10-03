"""The human-assistance gateway: claim, watch, act, hand back (§10.2, §10.3, §13.2, §13.4)."""

from __future__ import annotations

import asyncio
import time
from datetime import timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Query, WebSocket
from pydantic import BaseModel, StringConstraints
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool
from starlette.websockets import WebSocketState

from ..db.base import coerce_utc, utcnow
from ..db.models import ExecutionCommand, HumanTask
from ..domain.enums import CommandStatus, CommandType, HumanTaskStatus, Permission
from ..domain.errors import ApiError, ErrorCode
from ..human.registry import get_control_plane
from ..observability import current_request, current_tenant, get_logger
from ..repositories.human import CommandRepository, HumanTaskRepository
from .deps import (
    Context,
    Ctx,
    Page,
    app_database,
    app_settings,
    idempotent,
    new_request_id,
    page_envelope,
    page_params,
    resolve_identity_by_id,
)
from .tickets import CONTROL, get_ticket_store

router = APIRouter(tags=["human"])
log = get_logger(__name__)

#: How long the gateway waits for the lease holder's verdict before letting the client poll on.
COMMAND_RESULT_WAIT_SECONDS = 3.0
OPEN_STATUSES = (
    HumanTaskStatus.PENDING.value,
    HumanTaskStatus.CLAIMED.value,
    HumanTaskStatus.RESUME_REQUESTED.value,
)
SETTLED_STATUSES = (CommandStatus.PROCESSED.value, CommandStatus.REJECTED.value, CommandStatus.EXPIRED.value)


class ClaimRequest(BaseModel):
    model_config = {"extra": "forbid"}

    #: `human_task` has no row_version column (§11.2): its concurrency token is the status itself,
    #: which the repository compares under a row lock, so a stale claim cannot overwrite a newer one.
    expected_status: Annotated[str | None, StringConstraints(max_length=20)] = None


class ReleaseRequest(BaseModel):
    model_config = {"extra": "forbid"}

    reason: Annotated[str, StringConstraints(strip_whitespace=True, max_length=300)] = "operator gave up control"


class ResumeRequest(BaseModel):
    model_config = {"extra": "forbid"}

    #: The operator's explicit confirmation that they completed the step itself (§10.3, mode=before).
    step_completed: bool = False
    note: Annotated[str | None, StringConstraints(max_length=300)] = None


class ControlCommand(BaseModel):
    model_config = {"extra": "forbid"}

    operation: Annotated[str, StringConstraints(min_length=1, max_length=12)]
    #: Monotonic per session and paired with the picture the operator saw; both are re-checked by the
    #: worker that owns the page, which is where the whitelist is actually enforced (§10.2).
    sequence: int
    frame_id: int
    x: float | None = None
    y: float | None = None
    key: Annotated[str | None, StringConstraints(max_length=32)] = None
    text: Annotated[str | None, StringConstraints(max_length=4096)] = None
    dx: float | None = None
    dy: float | None = None

    def as_gate_command(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "operation": self.operation,
            "sequence": int(self.sequence),
            "frame_id": int(self.frame_id),
        }
        for name in ("x", "y", "key", "text", "dx", "dy"):
            value = getattr(self, name)
            if value is not None:
                payload[name] = value
        return payload


class OtpRequest(BaseModel):
    model_config = {"extra": "forbid"}

    text: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    sequence: int
    frame_id: int


# --------------------------------------------------------------------- row loading


def _load_task(
    ctx: Context, session: Session, human_task_id: str, *, permission: Permission | None = None
) -> HumanTask:
    task = HumanTaskRepository(session, ctx.tenant_id).by_id(human_task_id)
    if task is None:
        raise ApiError(ErrorCode.NOT_FOUND, "Human task not found in your tenant")
    if not ctx.visible_project(task.project_id):
        raise ApiError(ErrorCode.FORBIDDEN, "You are not a member of this project")
    if permission is not None:
        ctx.require(permission, task.project_id)
    return task


def task_payload(task: HumanTask) -> dict[str, Any]:
    deadline = coerce_utc(task.deadline)
    lease = coerce_utc(task.control_lease_until) if task.control_lease_until else None
    now = utcnow()
    return {
        "human_task_id": task.id,
        "execution_id": task.execution_id,
        "project_id": task.project_id,
        "step_id": task.step_id,
        "reason": task.reason,
        "mode": task.detail,
        "status": task.status,
        "controller_id": task.assignee_id,
        "control_lease_until": lease.isoformat() if lease else None,
        "holds_control": bool(lease and lease > now),
        "deadline": deadline.isoformat(),
        "remaining_seconds": max(0, int((deadline - now).total_seconds())),
        "session_epoch": int(task.session_epoch or 0),
        "resume_phase": task.resume_phase,
        "resume_condition": task.resume_condition,
        "resume_requested_at": coerce_utc(task.resume_requested_at).isoformat() if task.resume_requested_at else None,
        "outcome_note": task.outcome_note,
        "created_at": coerce_utc(task.created_at).isoformat() if task.created_at else None,
        # `pause_token` proves a resume to the worker; it is never a client-visible field (§10.3).
        "control_channel": f"/api/v1/human-tasks/{task.id}/control",
    }


def command_payload(row: ExecutionCommand) -> dict[str, Any]:
    """Status only: an operator's typed text sits in `payload`, and that column is not readable back (§11.2)."""
    return {
        "command_id": row.id,
        "execution_id": row.execution_id,
        "human_task_id": row.human_task_id,
        "command_type": row.command_type,
        "status": row.status,
        "requested_by": row.requested_by,
        "result": row.result or {},
        "created_at": coerce_utc(row.created_at).isoformat() if row.created_at else None,
        "processed_at": coerce_utc(row.processed_at).isoformat() if row.processed_at else None,
        "expires_at": coerce_utc(row.expires_at).isoformat() if row.expires_at else None,
    }


def _command_horizon(ctx: Context) -> Any:
    """A control command that nobody has picked up must not fire after its own picture is stale."""
    return utcnow() + timedelta(seconds=max(5, int(ctx.settings.human_control_ticket_ttl_seconds)))


def _await_result(
    ctx: Context, command_id: str, *, timeout: float = COMMAND_RESULT_WAIT_SECONDS
) -> dict[str, Any] | None:
    """Wait briefly for the worker's verdict so the operator sees the rejection where it belongs."""
    deadline = time.monotonic() + timeout
    while True:
        with ctx.database.session(ctx.tenant_id) as session:
            row = CommandRepository(session, ctx.tenant_id).by_id(command_id)
            if row is None:
                return None
            if row.status in SETTLED_STATUSES:
                return {"status": row.status, "result": dict(row.result or {})}
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.2)


# ------------------------------------------------------------------------- listing


@router.get("/projects/{project_id}/human-tasks")
def list_human_tasks(
    ctx: Ctx,
    project_id: str,
    page: Annotated[Page, Depends(page_params)],
    status: Annotated[str | None, Query(max_length=20, description="open (default), a single status, or all")] = None,
) -> dict[str, Any]:
    ctx.project(project_id)
    wanted = (status or "open").lower()
    known = {item.value for item in HumanTaskStatus}
    if wanted not in ("open", "all") and wanted not in known:
        raise ApiError(
            ErrorCode.VALIDATION_ERROR,
            f"Unknown human task status '{wanted}'",
            details={"allowed": sorted(known | {"open", "all"})},
        )
    with ctx.session() as session:
        if wanted == "open":
            rows = HumanTaskRepository(session, ctx.tenant_id).open(
                project_id=project_id, limit=page.offset + page.limit
            )[page.offset :]
        else:
            conditions = [HumanTask.tenant_id == ctx.tenant_id, HumanTask.project_id == project_id]
            if wanted != "all":
                conditions.append(HumanTask.status == wanted)
            rows = list(
                session.scalars(
                    select(HumanTask)
                    .where(*conditions)
                    .order_by(HumanTask.created_at.desc())
                    .offset(page.offset)
                    .limit(page.limit)
                ).all()
            )
        return page_envelope([task_payload(row) for row in rows], page)


# --------------------------------------------------------------------------- claim


@router.post("/human-tasks/{human_task_id}/claim")
def claim_human_task(ctx: Ctx, human_task_id: str, body: ClaimRequest) -> dict[str, Any]:
    """One controller at a time; the claim is a CAS, so two operators cannot both win the lease (§14.2)."""
    with ctx.session() as session:
        task = _load_task(ctx, session, human_task_id, permission=Permission.HUMAN_CONTROL)
        if body.expected_status and body.expected_status != task.status:
            raise ApiError(
                ErrorCode.CONFLICT,
                "The task is no longer in the state you saw",
                details={
                    "expected_status": body.expected_status,
                    "status": task.status,
                    "controller_id": task.assignee_id,
                },
            )
        if task.status == HumanTaskStatus.RESUME_REQUESTED.value:
            raise ApiError(ErrorCode.CONFLICT, "A resume is already being verified for this task")
        if coerce_utc(task.deadline) <= utcnow():
            raise ApiError(ErrorCode.CONFLICT, "The task deadline has passed, so there is nothing left to control")
        # The lease lasts as long as the pause window and is renewed by socket traffic (§10.2).
        claimed = HumanTaskRepository(session, ctx.tenant_id).claim(
            human_task_id,
            actor_id=ctx.actor_id,
            control_ttl_seconds=int(ctx.settings.human_wait_timeout_seconds),
        )
        ctx.audit(
            session,
            operation="human.claim",
            resource_type="human_task",
            resource_id=human_task_id,
            project_id=claimed.project_id,
            detail={"execution_id": claimed.execution_id, "step_id": claimed.step_id},
        )
        session.commit()
        return task_payload(claimed)


@router.post("/human-tasks/{human_task_id}/control-ticket")
def issue_control_ticket(ctx: Ctx, human_task_id: str) -> dict[str, Any]:
    """A one-time ticket for the socket only: the API bearer token never reaches the browser page (§14.3)."""
    with ctx.session() as session:
        task = _load_task(ctx, session, human_task_id, permission=Permission.HUMAN_CONTROL)
        if task.assignee_id != ctx.actor_id:
            raise ApiError(ErrorCode.FORBIDDEN, "Only the current controller may open a control channel")
        lease = coerce_utc(task.control_lease_until) if task.control_lease_until else None
        if lease is None or lease <= utcnow():
            raise ApiError(ErrorCode.CONFLICT, "The control lease has lapsed; claim the task again")
        if task.status not in OPEN_STATUSES:
            raise ApiError(ErrorCode.CONFLICT, f"The task is {task.status}, so there is nothing to control")
        ttl = int(ctx.settings.human_control_ticket_ttl_seconds)
        ticket = get_ticket_store().issue(
            CONTROL,
            tenant_id=ctx.tenant_id,
            actor_id=ctx.actor_id,
            resource_id=task.id,
            project_id=task.project_id,
            session_epoch=int(task.session_epoch or 0),
            ttl_seconds=ttl,
        )
        session.commit()
    return {
        "ticket": ticket.value,
        "expires_in_seconds": ttl,
        "usage": "connect once to WS /api/v1/human-tasks/{id}/control?ticket=…; it is consumed at connect",
        "channel": f"/api/v1/human-tasks/{human_task_id}/control?ticket={ticket.value}",
    }


# ------------------------------------------------------------------------ viewing


@router.get("/human-tasks/{human_task_id}/frame")
def current_frame(ctx: Ctx, human_task_id: str) -> dict[str, Any]:
    """The viewport, to the current controller only, and never written to disk (§10.4)."""
    with ctx.session() as session:
        task = _load_task(ctx, session, human_task_id, permission=Permission.HUMAN_CONTROL)
        if task.assignee_id != ctx.actor_id:
            raise ApiError(ErrorCode.FORBIDDEN, "Only the current controller may view the live viewport")
        lease = coerce_utc(task.control_lease_until) if task.control_lease_until else None
        if lease is None or lease <= utcnow():
            raise ApiError(ErrorCode.CONFLICT, "The control lease has lapsed; claim the task again")
        task_id = task.id
    frame = get_control_plane().latest_frame(task_id)
    if frame is None:
        return {
            "human_task_id": task_id,
            "available": False,
            "reason": "this node is not holding the browser session",
            "usage": "the WS control channel routes frames from the holding worker",
        }
    return {"human_task_id": task_id, "available": True, **frame}


# ----------------------------------------------------------------------- commands


@router.post("/human-tasks/{human_task_id}/commands", status_code=202)
def send_control_command(
    ctx: Ctx,
    human_task_id: str,
    body: ControlCommand,
    idempotency_key: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    """The HTTP path of the control channel: same whitelist, same serial queue, same verdict (§13.4)."""
    with ctx.session() as session:
        task = _load_task(ctx, session, human_task_id, permission=Permission.HUMAN_CONTROL)
        if task.assignee_id != ctx.actor_id:
            raise ApiError(ErrorCode.FORBIDDEN, "Only the current controller may send operations to the page")
        payload = body.as_gate_command()
        command = CommandRepository(session, ctx.tenant_id).enqueue(
            project_id=task.project_id,
            execution_id=task.execution_id,
            command_type=CommandType.CONTROL.value,
            dedupe_key=f"ctl:{ctx.actor_id}:{idempotency_key or body.sequence}"[:80],
            requested_by=ctx.actor_id,
            payload=payload,
            human_task_id=human_task_id,
            expires_at=_command_horizon(ctx),
        )
        command_id = command.id
        session.commit()

    delivered = get_control_plane().deliver(human_task_id, {**payload, "command_id": command_id})
    settled = _await_result(ctx, command_id) if delivered else None
    with ctx.session() as session:
        row = CommandRepository(session, ctx.tenant_id).by_id(command_id)
        out = (
            command_payload(row)
            if row is not None
            else {"command_id": command_id, "status": CommandStatus.PENDING.value}
        )
    out["delivered_in_process"] = delivered
    if settled is not None:
        out["settled"] = settled
    return out


@router.post("/human-tasks/{human_task_id}/otp", status_code=202)
def submit_otp(ctx: Ctx, human_task_id: str, body: OtpRequest) -> dict[str, Any]:
    """A secret goes to the page through memory only: it never becomes a persisted command (§11.2)."""
    with ctx.session() as session:
        task = _load_task(ctx, session, human_task_id, permission=Permission.HUMAN_CONTROL)
        if task.assignee_id != ctx.actor_id:
            raise ApiError(ErrorCode.FORBIDDEN, "Only the current controller may submit a one-time code")
        task_id = task.id
    delivered = get_control_plane().deliver(
        task_id,
        {
            "operation": "type",
            "text": body.text,
            "sequence": int(body.sequence),
            "frame_id": int(body.frame_id),
            "secret": True,
        },
    )
    if not delivered:
        raise ApiError(
            ErrorCode.DEPENDENCY_UNAVAILABLE,
            "The worker holding this session is not reachable from here, and a one-time code is never queued on disk",
            details={"human_task_id": task_id},
        )
    return {"human_task_id": task_id, "accepted": True, "note": "the code is typed into the page and not recorded"}


@router.post("/human-tasks/{human_task_id}/resume", status_code=202)
def request_resume(
    ctx: Ctx,
    human_task_id: str,
    body: ResumeRequest,
    idempotency_key: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    """202, never a claim of success: the worker verifies the lease, the page and the condition (§10.3)."""
    payload = body.model_dump(mode="json")

    def action() -> tuple[str, dict[str, Any]]:
        with ctx.session() as session:
            task = _load_task(ctx, session, human_task_id, permission=Permission.HUMAN_CONTROL)
            humans = HumanTaskRepository(session, ctx.tenant_id)
            resumed = humans.request_resume(human_task_id, actor_id=ctx.actor_id)
            command = CommandRepository(session, ctx.tenant_id).enqueue(
                project_id=task.project_id,
                execution_id=task.execution_id,
                command_type=CommandType.RESUME.value,
                dedupe_key=f"resume:{ctx.actor_id}:{idempotency_key or new_request_id()}"[:80],
                requested_by=ctx.actor_id,
                payload={"step_completed": bool(body.step_completed), "note": (body.note or "")[:300]},
                human_task_id=human_task_id,
                expires_at=coerce_utc(task.deadline),
            )
            ctx.audit(
                session,
                operation="human.resume_requested",
                resource_type="human_task",
                resource_id=human_task_id,
                project_id=task.project_id,
                detail={"command_id": command.id, "step_completed": bool(body.step_completed)},
            )
            session.commit()
            return command.id, {
                "command_id": command.id,
                "human_task_id": human_task_id,
                "task_status": resumed.status,
                "execution_id": task.execution_id,
                "note": "the worker confirms the resume; watch GET /executions/{id} for RUNNING",
            }

    return idempotent(
        ctx, route=f"POST /human-tasks/{human_task_id}/resume", key=idempotency_key, payload=payload, action=action
    )


@router.post("/human-tasks/{human_task_id}/release")
def release_control(ctx: Ctx, human_task_id: str, body: ReleaseRequest) -> dict[str, Any]:
    """Hand the pause back: the task stays open and its deadline does not move (§10.3)."""
    with ctx.session() as session:
        task = _load_task(ctx, session, human_task_id, permission=Permission.HUMAN_CONTROL)
        released = HumanTaskRepository(session, ctx.tenant_id).release_control(human_task_id, actor_id=ctx.actor_id)
        if released is None:
            raise ApiError(ErrorCode.CONFLICT, "You do not hold the control lease for this task")
        ctx.audit(
            session,
            operation="human.control_released",
            resource_type="human_task",
            resource_id=human_task_id,
            project_id=task.project_id,
            detail={"reason": body.reason[:300]},
        )
        session.commit()
        return task_payload(released)


@router.get("/commands/{command_id}")
def get_command(ctx: Ctx, command_id: str) -> dict[str, Any]:
    """Where a pause, control or resume request stands — and why it was refused (§13.2)."""
    with ctx.session() as session:
        row = CommandRepository(session, ctx.tenant_id).by_id(command_id)
        if row is None:
            raise ApiError(ErrorCode.NOT_FOUND, "Command not found in your tenant")
        if not ctx.visible_project(row.project_id):
            raise ApiError(ErrorCode.FORBIDDEN, "You are not a member of this project")
        if row.requested_by != ctx.actor_id and row.command_type == CommandType.CONTROL.value:
            ctx.require(Permission.HUMAN_CONTROL, row.project_id)
        return command_payload(row)


# ------------------------------------------------------------------ control socket


def _context_from_ticket(app: Any, ticket_value: str, human_task_id: str) -> Context:
    stored = get_ticket_store().redeem(CONTROL, ticket_value, resource_id=human_task_id)
    settings, database = app_settings(app), app_database(app)
    identity = resolve_identity_by_id(database, user_id=stored.actor_id, tenant_id=stored.tenant_id)
    request_id = new_request_id()
    current_tenant.set(identity.tenant_id)
    current_request.set(request_id)
    return Context(
        identity=identity,
        request_id=request_id,
        settings=settings,
        database=database,
        session_epoch=stored.session_epoch,
    )


def _authorize_socket(ctx: Context, human_task_id: str) -> dict[str, Any]:
    """Everything the ticket cannot carry is re-read from the database at connect (§14.1)."""
    with ctx.session() as session:
        task = _load_task(ctx, session, human_task_id, permission=Permission.HUMAN_CONTROL)
        if ctx.session_epoch is not None and int(task.session_epoch or 0) != int(ctx.session_epoch):
            raise ApiError(ErrorCode.FORBIDDEN, "The ticket belongs to a superseded worker session")
        if task.assignee_id != ctx.actor_id:
            raise ApiError(ErrorCode.FORBIDDEN, "Only the current controller may open a control channel")
        lease = coerce_utc(task.control_lease_until) if task.control_lease_until else None
        if lease is None or lease <= utcnow():
            raise ApiError(ErrorCode.CONFLICT, "The control lease has lapsed; claim the task again")
        return {
            "task_id": task.id,
            "execution_id": task.execution_id,
            "project_id": task.project_id,
            "step_id": task.step_id,
            "session_epoch": int(task.session_epoch or 0),
            "deadline": coerce_utc(task.deadline).isoformat(),
        }


def _hold_lease(ctx: Context, human_task_id: str) -> bool:
    """Traffic on the socket is what renews the lease; a dead socket stops renewing it (§10.2)."""
    with ctx.database.session(ctx.tenant_id) as session:
        return HumanTaskRepository(session, ctx.tenant_id).hold_control_lease(
            human_task_id, actor_id=ctx.actor_id, ttl_seconds=int(ctx.settings.human_wait_timeout_seconds)
        )


def _release_socket(ctx: Context, human_task_id: str) -> None:
    """Disconnect releases control but keeps the task open for a re-claim (§10.3 point 2)."""
    with ctx.database.session(ctx.tenant_id) as session:
        HumanTaskRepository(session, ctx.tenant_id).release_control(human_task_id, actor_id=ctx.actor_id)
        session.commit()


def _socket_command(ctx: Context, bound: dict[str, Any], human_task_id: str, command: ControlCommand) -> dict[str, Any]:
    """Persist, hand to the holder, and wait a moment for its verdict — all away from the loop."""
    if not _hold_lease(ctx, human_task_id):
        return {"body": {"type": "rejected", "reason": "the control lease is no longer yours"}}
    payload = command.as_gate_command()
    with ctx.database.session(ctx.tenant_id) as session:
        row = CommandRepository(session, ctx.tenant_id).enqueue(
            project_id=str(bound["project_id"]),
            execution_id=str(bound["execution_id"]),
            command_type=CommandType.CONTROL.value,
            dedupe_key=f"ctl:{ctx.actor_id}:{command.sequence}"[:80],
            requested_by=ctx.actor_id,
            payload=payload,
            human_task_id=human_task_id,
            expires_at=_command_horizon(ctx),
        )
        session.commit()
        command_id = row.id
        state = row.status
    body: dict[str, Any] = {"type": "accepted", "command_id": command_id, "state": state}
    if state != CommandStatus.PENDING.value:
        body["note"] = "a command with this sequence was already recorded"
        return {"body": body}
    if not get_control_plane().deliver(human_task_id, {**payload, "command_id": command_id}):
        body["note"] = "queued for the holding worker, which reads persisted commands at each step boundary"
        return {"body": body}
    settled = _await_result(ctx, command_id)
    if settled is None:
        return {"body": body}
    return {
        "body": body,
        "settle": {
            "type": "command_result",
            "command_id": command_id,
            "status": settled["status"],
            "reason": (settled.get("result") or {}).get("reason"),
        },
    }


@router.websocket("/human-tasks/{human_task_id}/control")
async def control_channel(
    websocket: WebSocket,
    human_task_id: str,
    ticket: Annotated[str, Query(max_length=64)],
) -> None:
    """The §13.4 channel: one redeemed ticket, then frames out and whitelisted operations in.

    The socket keeps the authority it redeemed, the way the control lease does — but every command is
    still persisted, so the worker that owns the page is the one that validates and serialises it.
    """
    try:
        ctx = await run_in_threadpool(_context_from_ticket, websocket.app, ticket, human_task_id)
    except ApiError as exc:
        await websocket.close(code=4403, reason=exc.message[:80])
        return
    try:
        bound = await run_in_threadpool(_authorize_socket, ctx, human_task_id)
    except ApiError as exc:
        await websocket.close(code=4409, reason=exc.message[:80])
        return

    await websocket.accept()
    plane = get_control_plane()
    queue = plane.subscribe(human_task_id)
    await websocket.send_json({"type": "ready", **bound, "worker_local": plane.is_live(human_task_id)})

    async def pump_frames() -> None:
        while True:
            frame = await queue.get()
            if websocket.client_state is not WebSocketState.CONNECTED:
                return
            await websocket.send_json({"type": "frame", "data": frame})

    pump = asyncio.create_task(pump_frames())
    try:
        while True:
            message = await websocket.receive_json()
            kind = str(message.get("type") or "")
            if kind == "ping":
                await websocket.send_json({"type": "pong", "server_time": utcnow().isoformat()})
                continue
            if kind != "command":
                await websocket.send_json({"type": "rejected", "reason": f"unknown message type '{kind}'"})
                continue
            try:
                command = ControlCommand.model_validate(message.get("payload") or message)
            except Exception as exc:
                await websocket.send_json({"type": "rejected", "reason": f"malformed command: {type(exc).__name__}"})
                continue
            verdict = await run_in_threadpool(_socket_command, ctx, bound, human_task_id, command)
            await websocket.send_json(verdict["body"])
            if verdict.get("settle"):
                await websocket.send_json(verdict["settle"])
    except Exception as exc:
        log.info(
            "control socket closed", extra={"context": {"human_task_id": human_task_id, "error": type(exc).__name__}}
        )
    finally:
        pump.cancel()
        plane.unsubscribe(human_task_id, queue)
        await run_in_threadpool(_release_socket, ctx, human_task_id)
