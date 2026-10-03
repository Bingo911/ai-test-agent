"""Project, case, revision and execution authorisation for both adapters (§5.1, §9.3.1, §14.1).

REST used to reach the database through `Context.project()`, which opens its own session. An atomic
command cannot afford that: a reservation, an authorisation read and a business write that live in
three transactions can each succeed while another fails, and the half-state is exactly what the
idempotency contract exists to prevent. Every function here is handed the session the caller is
already in the middle of, so authorisation and the writes it authorises commit together or not at all.

The order of the refusals is part of the wire contract, not an implementation detail: an id the caller
cannot see answers 404 and a visible resource it may not touch answers 403, so a probe cannot tell
which tenants own which resources. Existing messages are reproduced verbatim for that reason.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from ..db.models import Project
from ..domain.enums import Permission
from ..domain.errors import ApiError, ErrorCode
from ..repositories.cases import CaseRepository
from ..repositories.executions import ExecutionRepository
from ..repositories.platform import AccessRepository
from .context import CallContext
from .identity import identity_for


def project(
    session: Session, call: CallContext, project_id: str, *, permission: Permission | None = None
) -> Project:
    """Load a project and authorise it in one step, inside the caller's transaction."""
    row = AccessRepository(session, call.tenant_id).project(call.tenant_id, project_id)
    if row is None:
        raise ApiError(ErrorCode.NOT_FOUND, "Project not found in your tenant")
    if not call.visible_project(project_id):
        raise ApiError(ErrorCode.FORBIDDEN, "You are not a member of this project")
    if permission is not None:
        call.identity.require(permission, project_id)
    return row


def require_after_lock(session: Session, call: CallContext, *, project_id: str, permission: Permission) -> None:
    """Re-read the caller's authority once a lock wait has ended, and refuse if it went away (§9.3.2).

    A command can block on another command for as long as its own deadline allows. The identity that
    admitted it was resolved before that wait, so a grant revoked in the meantime would otherwise ride
    through on a stale snapshot. The refusal looks exactly like the ordinary one, because to the caller
    there is no difference: it is not permitted.
    """
    user = AccessRepository(session, call.tenant_id).user(call.actor_id)
    if user is None:
        raise ApiError(ErrorCode.FORBIDDEN, "Your access was revoked while this command was waiting")
    current = identity_for(
        session,
        user,
        call.tenant_id,
        display=call.identity.display_name,
        issuer=call.identity.issuer,
        subject=call.identity.subject,
    )
    if current.role_in(project_id) is None:
        raise ApiError(ErrorCode.FORBIDDEN, "You are not a member of this project")
    current.require(permission, project_id)


def case(session: Session, call: CallContext, case_id: str, *, permission: Permission) -> Any:
    row = CaseRepository(session, call.tenant_id).by_id(case_id)
    if row is None:
        raise ApiError(ErrorCode.NOT_FOUND, "Case not found in your tenant")
    project(session, call, row.project_id, permission=permission)
    return row


def revision(session: Session, call: CallContext, revision_id: str, *, permission: Permission) -> Any:
    row = CaseRepository(session, call.tenant_id).revision(revision_id)
    if row is None:
        raise ApiError(ErrorCode.NOT_FOUND, "Case revision not found in your tenant")
    project(session, call, row.project_id, permission=permission)
    return row


def execution(session: Session, call: CallContext, execution_id: str) -> Any:
    """An execution the caller's project membership reaches, and nothing else.

    Unlike `project()` this does not load the project row: membership was resolved into the identity
    for this call, and a run that predates a project rename or archive is still the caller's to read.
    """
    row = ExecutionRepository(session, call.tenant_id).by_id(execution_id)
    if row is None:
        raise ApiError(ErrorCode.NOT_FOUND, "Execution not found in your tenant")
    if not call.visible_project(row.project_id):
        raise ApiError(ErrorCode.FORBIDDEN, "You are not a member of this project")
    return row


def cancellable_execution(session: Session, call: CallContext, execution_id: str) -> Any:
    """Cancel authority is narrower than read authority: the requester's own run, or a grant to cancel any (§14.2)."""
    row = execution(session, call, execution_id)
    project_id = row.project_id
    if call.can(Permission.EXECUTION_CANCEL_ANY, project_id):
        return row
    if row.requested_by == call.actor_id and call.can(Permission.EXECUTION_CANCEL_OWN, project_id):
        return row
    raise ApiError(
        ErrorCode.FORBIDDEN,
        "Only the requester or a lead of this project can cancel the execution",
        details={"requested_by": row.requested_by},
    )
