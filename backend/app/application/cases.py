"""The case commands both adapters run: save, revise, and the queries that read them back (§3.1, §6.1).

A command here is the whole unit of work a caller asks for: authorise in the injected session, decide
replay or execute, write the business rows, queue the compile, and audit - one transaction, one
committer. `cases_payload` and `revision_payload` exist because REST and MCP project the same stored
row differently but must never disagree about what a case *is*.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ..domain.enums import Permission
from ..domain.errors import ApiError, ErrorCode
from ..repositories.cases import CaseRepository
from ..services import cases as cases_core
from . import authorization
from .idempotency import execute_atomic_command
from .unit_of_work import UnitOfWork


def create_case(
    uow: UnitOfWork,
    *,
    project_id: str,
    name: str,
    markdown: str,
    dsl_version: str,
    title: str | None,
    tags: list[str] | None,
    idempotency_key: str | None,
    gate: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """Save a new case and queue its deterministic compile (§6.1)."""
    call = uow.call
    session = uow.scope
    authorization.project(session, call, project_id, permission=Permission.CASE_WRITE)
    command: dict[str, Any] = {
        "name": name,
        "markdown": markdown,
        "title": title,
        "tags": list(tags or []),
        "dsl_version": dsl_version,
    }

    def action() -> tuple[str, dict[str, Any]]:
        result = cases_core.save_case(
            session,
            call.tenant_id,
            project_id=project_id,
            name=name,
            markdown=markdown,
            dsl_version=dsl_version,
            title=title,
            created_by=call.actor_id,
            tags=command["tags"],
        )
        uow.audit(
            operation="case.create",
            resource_type="case",
            resource_id=str(result["case_id"]),
            project_id=project_id,
            detail={"revision_id": result["revision_id"], "revision_no": result["revision_no"]},
        )
        return str(result["case_id"]), dict(result)

    return execute_atomic_command(
        uow,
        route=f"POST /projects/{project_id}/cases",
        key=idempotency_key,
        command=command,
        action=action,
        project_id=project_id,
        permission=Permission.CASE_WRITE,
        gate=gate,
        legacy_payload=command,
    )


def add_case_revision(
    uow: UnitOfWork,
    *,
    case_id: str,
    markdown: str,
    dsl_version: str,
    title: str | None,
    expected_row_version: int | None,
    idempotency_key: str | None,
    gate: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """Append an immutable revision; the text of a revision is never edited in place (§3.2).

    This route had no idempotency header before, so there are no legacy records to reconcile against -
    a key here is a new capability rather than a reformatting.
    """
    call = uow.call
    session = uow.scope
    case = authorization.case(session, call, case_id, permission=Permission.CASE_WRITE)
    command: dict[str, Any] = {
        "markdown": markdown,
        "title": title,
        "dsl_version": dsl_version,
        "expected_row_version": expected_row_version,
    }

    def action() -> tuple[str, dict[str, Any]]:
        current = CaseRepository(session, call.tenant_id).require(case_id)
        if current.archived_at is not None:
            raise ApiError(ErrorCode.CASE_ARCHIVED, "An archived case cannot take a new revision")
        result = cases_core.append_revision(
            session,
            call.tenant_id,
            current,
            markdown=markdown,
            dsl_version=dsl_version,
            title=title,
            created_by=call.actor_id,
            expected_row_version=expected_row_version,
        )
        uow.audit(
            operation="case.revision.add",
            resource_type="case",
            resource_id=case_id,
            project_id=current.project_id,
            detail={"revision_id": result["revision_id"], "revision_no": result["revision_no"]},
        )
        return case_id, dict(result)

    return execute_atomic_command(
        uow,
        route=f"POST /cases/{case_id}/revisions",
        key=idempotency_key,
        command=command,
        action=action,
        project_id=case.project_id,
        permission=Permission.CASE_WRITE,
        gate=gate,
    )
