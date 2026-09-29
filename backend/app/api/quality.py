"""Quality statistics and the audit trail a lead is allowed to read (§12.4, §13.2, §14.1)."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select

from ..db.base import coerce_utc
from ..db.models import AppUser
from ..domain.enums import Permission
from ..reporting.quality import execution_stats
from ..repositories.platform import AuditRepository
from .deps import Ctx, Page, page_envelope, page_params

router = APIRouter(tags=["quality"])


@router.get("/projects/{project_id}/metrics")
def project_metrics(
    ctx: Ctx,
    project_id: str,
    since: Annotated[datetime | None, Query(description="ISO timestamp, inclusive")] = None,
    until: Annotated[datetime | None, Query()] = None,
    environment_id: Annotated[str | None, Query(max_length=36)] = None,
    case_id: Annotated[str | None, Query(max_length=36)] = None,
) -> dict[str, Any]:
    """Pass rate over a fixed denominator, plus the infrastructure and human shares beside it (§12.4)."""
    ctx.project(project_id, permission=Permission.QUALITY_READ)
    with ctx.session() as session:
        return execution_stats(
            session,
            tenant_id=ctx.tenant_id,
            project_id=project_id,
            environment_id=environment_id,
            case_id=case_id,
            since=since,
            until=until,
        )


@router.get("/projects/{project_id}/audit-logs")
def project_audit_logs(
    ctx: Ctx,
    project_id: str,
    page: Annotated[Page, Depends(page_params)],
    since: Annotated[datetime | None, Query()] = None,
    until: Annotated[datetime | None, Query()] = None,
    actor_id: Annotated[str | None, Query(max_length=36)] = None,
    operation: Annotated[str | None, Query(max_length=80)] = None,
    resource_type: Annotated[str | None, Query(max_length=48)] = None,
    resource_id: Annotated[str | None, Query(max_length=36)] = None,
) -> dict[str, Any]:
    """Who changed what in this project. The trail is append-only and readable by leads (§14.1)."""
    ctx.project(project_id, permission=Permission.AUDIT_READ)
    with ctx.session() as session:
        rows, total = AuditRepository(session, ctx.tenant_id).query(
            project_id=project_id,
            actor_id=actor_id,
            operation=operation,
            resource_type=resource_type,
            resource_id=resource_id,
            since=since,
            until=until,
            limit=page.limit,
            offset=page.offset,
        )
        names = _actor_names(session, ctx.tenant_id, rows)
        items = [_audit_payload(row, actor_names=names) for row in rows]
    return page_envelope(items, page, total=total)


def _actor_names(session: Any, tenant_id: str, rows: list[Any]) -> dict[str, str]:
    """Display names resolve in one query for the page, not one per row."""
    ids = sorted({row.actor_id for row in rows if row.actor_id})
    if not ids:
        return {}
    pairs = session.execute(select(AppUser.id, AppUser.display_name).where(AppUser.id.in_(ids))).all()
    return {str(user_id): str(name or "") for user_id, name in pairs}


def _audit_payload(row: Any, *, actor_names: dict[str, str]) -> dict[str, Any]:
    return {
        "audit_id": row.id,
        "actor_id": row.actor_id,
        "actor_name": actor_names.get(str(row.actor_id or ""), ""),
        "project_id": row.project_id,
        "operation": row.operation,
        "resource_type": row.resource_type,
        "resource_id": row.resource_id,
        "request_id": row.request_id,
        # Details were redacted on write; they are returned as stored, never widened here.
        "detail": dict(row.detail or {}),
        "created_at": coerce_utc(row.created_at).isoformat() if row.created_at else None,
    }
