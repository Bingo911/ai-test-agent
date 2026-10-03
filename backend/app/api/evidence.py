"""Evidence: a short download ticket, then a second authorization at read time (§13.2, §14.3)."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, StringConstraints

from ..db.base import coerce_utc, utcnow
from ..db.models import Artifact
from ..domain.enums import Permission, Sensitivity, UploadStatus
from ..domain.errors import ApiError, ErrorCode
from ..observability import current_request, current_tenant, get_logger
from ..reporting.report import RESTRICTED_KINDS
from ..repositories.artifacts import ArtifactRepository
from ..repositories.executions import ExecutionRepository
from ..services.object_store import get_object_store, safe_filename
from .deps import Context, Ctx, app_database, app_settings, new_request_id, resolve_identity_by_id
from .tickets import DOWNLOAD, get_ticket_store

router = APIRouter(tags=["evidence"])
log = get_logger(__name__)

#: A link that outlives its usefulness leaks evidence; §14.3 recommends 60 seconds.
DOWNLOAD_TTL_SECONDS = 60
#: What the caller claims it is downloading the evidence *for*, recorded in the audit trail.
USAGES = ("view", "debug", "defect-report", "archive")

#: Target-page HTML, DOM dumps and traces carry live scripts: they are never rendered in the
#: console's own origin (§14.3). A separate origin is the real control; this is the header that
#: keeps a same-origin mistake from becoming script execution.
_SANDBOX_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; sandbox; base-uri 'none'; form-action 'none'",
    "X-Content-Type-Options": "nosniff",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}


class DownloadTicketRequest(BaseModel):
    model_config = {"extra": "forbid"}

    usage: Annotated[str, StringConstraints(min_length=1, max_length=32)] = "view"


def _load_artifact(ctx: Context, artifact_id: str) -> tuple[Artifact, str]:
    """Returns the row and the execution's evidence mode; both routes need the same policy (§14.1)."""
    with ctx.session() as session:
        artifact = ArtifactRepository(session, ctx.tenant_id).by_id(artifact_id)
        if artifact is None:
            raise ApiError(ErrorCode.NOT_FOUND, "Artifact not found in your tenant")
        if not ctx.visible_project(artifact.project_id):
            raise ApiError(ErrorCode.FORBIDDEN, "You are not a member of this project")
        execution = ExecutionRepository(session, ctx.tenant_id).by_id(artifact.execution_id)
        mode = execution.evidence_mode if execution is not None else Sensitivity.NORMAL.value
        return artifact, mode


def _requires_specialist(artifact: Artifact, evidence_mode: str) -> bool:
    return (
        artifact.sensitivity == Sensitivity.SENSITIVE.value
        or evidence_mode == Sensitivity.SENSITIVE.value
        or artifact.kind in RESTRICTED_KINDS
        or artifact.publish_allowed is False
    )


def _assert_retrievable(artifact: Artifact) -> None:
    if artifact.upload_status != UploadStatus.READY.value:
        raise ApiError(
            ErrorCode.NOT_FOUND,
            "The evidence is not available for download",
            details={"upload_status": artifact.upload_status},
        )
    if artifact.retention_until is not None and coerce_utc(artifact.retention_until) <= utcnow():
        # Expired evidence says "expired", not "does not exist": the report still lists the gap (§12.2).
        raise ApiError(
            ErrorCode.NOT_FOUND,
            "The evidence has passed its retention deadline and has been removed",
            details={"retention_until": coerce_utc(artifact.retention_until).isoformat()},
        )


@router.post("/artifacts/{artifact_id}/download-ticket")
def issue_download_ticket(ctx: Ctx, artifact_id: str, body: DownloadTicketRequest) -> dict[str, Any]:
    """Issue a one-minute, one-use, single-purpose link; issuing it is itself an audited act (§14.3)."""
    usage = body.usage.strip()
    if usage not in USAGES:
        raise ApiError(
            ErrorCode.VALIDATION_ERROR, f"Unknown download usage '{usage}'", details={"allowed": list(USAGES)}
        )
    artifact, mode = _load_artifact(ctx, artifact_id)
    _assert_retrievable(artifact)
    if _requires_specialist(artifact, mode):
        ctx.require(Permission.SENSITIVE_ARTIFACT_READ, artifact.project_id)
    ttl = int(ctx.settings.artifact_access_ttl_seconds or DOWNLOAD_TTL_SECONDS)
    ticket = get_ticket_store().issue(
        DOWNLOAD,
        tenant_id=ctx.tenant_id,
        actor_id=ctx.actor_id,
        resource_id=artifact.id,
        project_id=artifact.project_id,
        ttl_seconds=ttl,
    )
    with ctx.session() as session:
        ctx.audit(
            session,
            operation="artifact.download_ticket",
            resource_type="artifact",
            resource_id=artifact.id,
            project_id=artifact.project_id,
            detail={
                "usage": usage,
                "kind": artifact.kind,
                "sensitive": _requires_specialist(artifact, mode),
                "expires_in_seconds": ttl,
            },
        )
        session.commit()
    return {
        "ticket": ticket.value,
        "expires_in_seconds": ttl,
        "media_type": artifact.media_type,
        "size": int(artifact.size or 0),
        "download_url": f"/api/v1/artifacts/{artifact.id}/download?ticket={ticket.value}",
        "usage_note": "the ticket is consumed by the first download and cannot be replayed",
    }


def _context_from_download(app: Any, ticket_value: str, artifact_id: str) -> Context:
    """Redeem the ticket, then rebuild the caller's authority from the database (§14.1)."""
    stored = get_ticket_store().redeem(DOWNLOAD, ticket_value, resource_id=artifact_id)
    settings, database = app_settings(app), app_database(app)
    identity = resolve_identity_by_id(database, user_id=stored.actor_id, tenant_id=stored.tenant_id)
    request_id = new_request_id()
    current_tenant.set(identity.tenant_id)
    current_request.set(request_id)
    return Context(identity=identity, request_id=request_id, settings=settings, database=database)


@router.get("/artifacts/{artifact_id}/download")
def download_artifact(
    request: Request,
    artifact_id: str,
    ticket: Annotated[str, Query(max_length=64)],
) -> Response:
    """Proxy the bytes: the object store is never exposed, and access is logged (§14.3)."""
    ctx = _context_from_download(request.app, ticket, artifact_id)
    request_id = getattr(request.state, "request_id", None) or ctx.request_id
    artifact, mode = _load_artifact(ctx, artifact_id)
    _assert_retrievable(artifact)
    if _requires_specialist(artifact, mode):
        ctx.require(Permission.SENSITIVE_ARTIFACT_READ, artifact.project_id)

    store = get_object_store(ctx.settings)
    try:
        data = store.read_bytes(artifact.object_key, max_bytes=int(ctx.settings.artifact_max_bytes))
    except FileNotFoundError:
        raise ApiError(ErrorCode.NOT_FOUND, "The stored object is no longer present") from None
    except Exception as exc:
        log.warning("artifact read failed", extra={"context": {"artifact_id": artifact_id, "error": str(exc)[:200]}})
        raise ApiError(ErrorCode.DEPENDENCY_UNAVAILABLE, "The evidence store is not reachable right now") from None

    with ctx.session() as session:
        ctx.audit(
            session,
            operation="artifact.download",
            resource_type="artifact",
            resource_id=artifact.id,
            project_id=artifact.project_id,
            detail={"kind": artifact.kind, "bytes": len(data), "sensitive": _requires_specialist(artifact, mode)},
        )
        session.commit()

    headers = dict(_SANDBOX_HEADERS)
    headers["Content-Disposition"] = f'attachment; filename="{safe_filename(artifact.name or artifact.id)}"'
    headers["X-Request-ID"] = request_id
    headers["Accept-Ranges"] = "none"
    return Response(content=data, media_type=artifact.media_type or "application/octet-stream", headers=headers)
