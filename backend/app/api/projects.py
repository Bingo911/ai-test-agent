"""Projects, membership, explicit grants and platform user administration (§13.2, §14.1)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Query, Response
from pydantic import BaseModel, Field, StringConstraints
from sqlalchemy import select

from ..db.base import new_id, utcnow
from ..db.models import Project, TenantMembership
from ..domain.enums import Permission, Role
from ..domain.errors import ApiError, ErrorCode
from ..repositories.platform import AccessRepository, UserRepository
from .deps import Ctx, Page, etag, idempotent, page_envelope, page_params, parse_if_match
from .tickets import get_ticket_store

router = APIRouter(tags=["projects"])

DisplayName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
Slug = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9_-]{0,79}$")]

#: Everything else comes from a role; these two abilities are only ever granted per user (§14.1).
GRANTABLE = (Permission.HUMAN_CONTROL, Permission.SENSITIVE_ARTIFACT_READ)


class ProjectCreate(BaseModel):
    model_config = {"extra": "forbid"}

    name: Slug
    display_name: DisplayName | None = None
    description: Annotated[str | None, Field(default=None, max_length=2000)] = None
    quota: dict[str, Any] | None = None
    settings: dict[str, Any] | None = None


class ProjectPatch(BaseModel):
    model_config = {"extra": "forbid"}

    display_name: DisplayName | None = None
    description: Annotated[str | None, Field(default=None, max_length=2000)] = None
    quota: dict[str, Any] | None = None
    settings: dict[str, Any] | None = None
    archived: bool | None = None


class MemberCreate(BaseModel):
    model_config = {"extra": "forbid"}

    user_id: Annotated[str, Field(min_length=8, max_length=36)]
    role: Role


class MemberPatch(BaseModel):
    model_config = {"extra": "forbid"}

    role: Role


class GrantCreate(BaseModel):
    model_config = {"extra": "forbid"}

    permission: Permission
    expires_at: datetime | None = None
    reason: Annotated[str | None, Field(default=None, max_length=300)] = None


class UserProvision(BaseModel):
    model_config = {"extra": "forbid"}

    issuer: Annotated[str, StringConstraints(min_length=1, max_length=120)]
    subject: Annotated[str, StringConstraints(min_length=1, max_length=200)]
    display_name: DisplayName | None = None
    email: Annotated[str | None, Field(default=None, max_length=300)] = None
    role: Role = Role.ENGINEER


class UserStatusPatch(BaseModel):
    model_config = {"extra": "forbid"}

    status: Annotated[str, StringConstraints(pattern=r"^(ACTIVE|DISABLED)$")]


# ----------------------------------------------------------------------- projects


@router.get("/projects")
def list_projects(ctx: Ctx, page: Annotated[Page, Depends(page_params)]) -> dict[str, Any]:
    with ctx.session() as session:
        access = AccessRepository(session, ctx.tenant_id)
        rows = access.projects_for_user(
            tenant_id=ctx.tenant_id, user_id=ctx.actor_id, tenant_role=ctx.identity.role_in(None)
        )
        items = [_project_payload(project, ctx) for project in rows]
    return page_envelope(items, page)


@router.post("/projects", status_code=201)
def create_project(
    ctx: Ctx,
    body: ProjectCreate,
    response: Response,
    idempotency_key: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    """A new project needs a tenant-wide role, because there is no project to be a member of yet."""
    ctx.require(Permission.PROJECT_MANAGE)
    payload = body.model_dump(mode="json")

    def action() -> tuple[str, dict[str, Any]]:
        with ctx.session() as session:
            access = AccessRepository(session, ctx.tenant_id)
            if access.project_by_name(ctx.tenant_id, body.name) is not None:
                raise ApiError(ErrorCode.CONFLICT, f"A project named '{body.name}' already exists in this tenant")
            project = access.create_project(
                tenant_id=ctx.tenant_id,
                name=body.name,
                display_name=body.display_name,
                description=body.description,
                quota=body.quota or {},
                settings=body.settings or {},
            )
            access.add_membership(tenant_id=ctx.tenant_id, project_id=project.id, user_id=ctx.actor_id, role=Role.LEAD)
            ctx.audit(
                session,
                operation="project.create",
                resource_type="project",
                resource_id=project.id,
                project_id=project.id,
            )
            session.flush()
            return project.id, _project_payload(project, ctx)

    result = idempotent(ctx, route="POST /projects", key=idempotency_key, payload=payload, action=action)
    response.headers["ETag"] = etag("project", str(result["id"]), int(result["row_version"]))
    return result


@router.get("/projects/{project_id}")
def get_project(ctx: Ctx, project_id: str, response: Response) -> dict[str, Any]:
    project = ctx.project(project_id)
    response.headers["ETag"] = etag("project", project.id, int(project.row_version))
    return _project_payload(project, ctx)


@router.patch("/projects/{project_id}")
def patch_project(
    ctx: Ctx,
    project_id: str,
    body: ProjectPatch,
    response: Response,
    if_match: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    project = ctx.project(project_id, permission=Permission.PROJECT_MANAGE)
    expected = parse_if_match(if_match, required=True)
    with ctx.session() as session:
        updated = AccessRepository(session, ctx.tenant_id).update_project(
            project,
            display_name=body.display_name,
            description=body.description,
            quota=body.quota,
            settings=body.settings,
            archived=body.archived,
            expected_row_version=expected,
        )
        ctx.audit(
            session,
            operation="project.update",
            resource_type="project",
            resource_id=updated.id,
            project_id=updated.id,
            detail={
                "archived": updated.archived_at is not None,
                "fields": sorted(key for key, value in body.model_dump().items() if value is not None),
            },
        )
        payload = _project_payload(updated, ctx)
    response.headers["ETag"] = etag("project", updated.id, int(updated.row_version))
    return payload


# ------------------------------------------------------------------------- members


@router.get("/projects/{project_id}/members")
def list_members(ctx: Ctx, project_id: str) -> dict[str, Any]:
    ctx.project(project_id)
    ctx.require(Permission.PROJECT_MANAGE, project_id)
    with ctx.session() as session:
        access = AccessRepository(session, ctx.tenant_id)
        users = UserRepository(session, ctx.tenant_id)
        members = []
        for row in access.memberships(project_id):
            user = users.by_id(row.user_id)
            members.append(
                {
                    "user_id": row.user_id,
                    "role": row.role,
                    "display_name": user.display_name if user else None,
                    "subject": user.subject if user else None,
                    "status": user.status if user else "UNKNOWN",
                    "joined_at": _iso(row.created_at),
                }
            )
        grants = [_grant_payload(row) for row in access.grant_rows(project_id)]
    return {"members": members, "grants": grants}


@router.post("/projects/{project_id}/members", status_code=201)
def add_member(ctx: Ctx, project_id: str, body: MemberCreate) -> dict[str, Any]:
    ctx.project(project_id, permission=Permission.PROJECT_MANAGE)
    with ctx.session() as session:
        access = AccessRepository(session, ctx.tenant_id)
        _require_tenant_member(session, ctx.tenant_id, body.user_id)
        access.add_membership(tenant_id=ctx.tenant_id, project_id=project_id, user_id=body.user_id, role=body.role)
        ctx.audit(
            session,
            operation="project.member.add",
            resource_type="project_membership",
            resource_id=body.user_id,
            project_id=project_id,
            detail={"role": body.role.value},
        )
    return {"project_id": project_id, "user_id": body.user_id, "role": body.role.value}


@router.patch("/projects/{project_id}/members/{user_id}")
def patch_member(ctx: Ctx, project_id: str, user_id: str, body: MemberPatch) -> dict[str, Any]:
    ctx.project(project_id, permission=Permission.PROJECT_MANAGE)
    with ctx.session() as session:
        access = AccessRepository(session, ctx.tenant_id)
        existing = access.membership(project_id=project_id, user_id=user_id)
        if existing is None:
            raise ApiError(ErrorCode.NOT_FOUND, "That user is not a member of this project")
        access.add_membership(tenant_id=ctx.tenant_id, project_id=project_id, user_id=user_id, role=body.role)
        ctx.audit(
            session,
            operation="project.member.update",
            resource_type="project_membership",
            resource_id=user_id,
            project_id=project_id,
            detail={"from": existing.role, "to": body.role.value},
        )
    return {"project_id": project_id, "user_id": user_id, "role": body.role.value}


@router.delete("/projects/{project_id}/members/{user_id}", status_code=204)
def remove_member(ctx: Ctx, project_id: str, user_id: str) -> None:
    ctx.project(project_id, permission=Permission.PROJECT_MANAGE)
    with ctx.session() as session:
        access = AccessRepository(session, ctx.tenant_id)
        if not access.remove_membership(project_id=project_id, user_id=user_id):
            raise ApiError(ErrorCode.NOT_FOUND, "That user is not a member of this project")
        for permission in GRANTABLE:
            # Leaving the project must not leave a specialist grant behind that keeps working (§14.1).
            access.revoke_grant(tenant_id=ctx.tenant_id, project_id=project_id, user_id=user_id, permission=permission)
        ctx.audit(
            session,
            operation="project.member.remove",
            resource_type="project_membership",
            resource_id=user_id,
            project_id=project_id,
        )
    get_ticket_store().revoke_actor(user_id)


# ---------------------------------------------------------------------- grants


@router.put("/projects/{project_id}/members/{user_id}/grants/{permission}")
def put_grant(
    ctx: Ctx,
    project_id: str,
    user_id: str,
    permission: Permission,
    body: GrantCreate,
) -> dict[str, Any]:
    ctx.project(project_id, permission=Permission.PROJECT_MANAGE)
    ctx.require(Permission.ADMIN_USERS)
    if permission not in GRANTABLE or body.permission != permission:
        raise ApiError(
            ErrorCode.VALIDATION_ERROR,
            "Only 'human_control' and 'sensitive_artifact_read' are granted per user; "
            "every other ability comes from a role",
            details={"path_permission": permission.value, "body_permission": body.permission.value},
        )
    expires_in = _seconds_until(body.expires_at)
    with ctx.session() as session:
        access = AccessRepository(session, ctx.tenant_id)
        _require_tenant_member(session, ctx.tenant_id, user_id)
        access.add_grant(
            tenant_id=ctx.tenant_id,
            project_id=project_id,
            user_id=user_id,
            permission=permission,
            granted_by=ctx.actor_id,
            reason=body.reason,
            expires_in_seconds=expires_in,
        )
        ctx.audit(
            session,
            operation="grant.issue",
            resource_type="permission_grant",
            resource_id=user_id,
            project_id=project_id,
            detail={"permission": permission.value, "expires_at": _iso(body.expires_at), "reason": body.reason},
        )
    return {
        "project_id": project_id,
        "user_id": user_id,
        "permission": permission.value,
        "expires_at": _iso(body.expires_at),
        "reason": body.reason,
    }


@router.delete("/projects/{project_id}/members/{user_id}/grants/{permission}")
def delete_grant(ctx: Ctx, project_id: str, user_id: str, permission: Permission) -> dict[str, Any]:
    ctx.project(project_id, permission=Permission.PROJECT_MANAGE)
    ctx.require(Permission.ADMIN_USERS)
    with ctx.session() as session:
        access = AccessRepository(session, ctx.tenant_id)
        if not access.revoke_grant(
            tenant_id=ctx.tenant_id, project_id=project_id, user_id=user_id, permission=permission
        ):
            raise ApiError(ErrorCode.NOT_FOUND, "That grant does not exist")
        ctx.audit(
            session,
            operation="grant.revoke",
            resource_type="permission_grant",
            resource_id=user_id,
            project_id=project_id,
            detail={"permission": permission.value},
        )
    get_ticket_store().revoke_actor(user_id)
    return {"project_id": project_id, "user_id": user_id, "permission": permission.value, "revoked": True}


# ------------------------------------------------------------------ platform users


@router.get("/admin/users")
def list_users(
    ctx: Ctx,
    page: Annotated[Page, Depends(page_params)],
    status: Annotated[str | None, Query(pattern=r"^(ACTIVE|DISABLED)$")] = None,
    search: Annotated[str | None, Query(max_length=120)] = None,
) -> dict[str, Any]:
    ctx.require(Permission.ADMIN_USERS)
    with ctx.session() as session:
        users = UserRepository(session, ctx.tenant_id)
        rows, total = users.list(status=status, search=search, limit=page.limit, offset=page.offset)
        items = []
        for row in rows:
            memberships = users.tenant_memberships(row.id)
            items.append(
                {
                    "id": row.id,
                    "issuer": row.issuer,
                    "subject": row.subject,
                    "display_name": row.display_name,
                    "email": row.email,
                    "status": row.status,
                    "tenants": [member.tenant_id for member in memberships],
                    "tenant_role": next(
                        (member.role for member in memberships if member.tenant_id == ctx.tenant_id), None
                    ),
                    "created_at": _iso(row.created_at),
                }
            )
    return page_envelope(items, page, total=total)


@router.post("/admin/users", status_code=201)
def provision_user(ctx: Ctx, body: UserProvision) -> dict[str, Any]:
    """An administrator enables an identity before it can sign in; unknown subjects are refused (§14.1)."""
    ctx.require(Permission.ADMIN_USERS)
    with ctx.session() as session:
        users = UserRepository(session, ctx.tenant_id)
        user = users.provision(
            issuer=body.issuer, subject=body.subject, display_name=body.display_name, email=body.email
        )
        if not users.tenant_memberships(user.id):
            # A brand-new identity has to land in a tenant, or the first request it makes 403s.
            session.add(TenantMembership(id=new_id(), tenant_id=ctx.tenant_id, user_id=user.id, role=body.role.value))
        ctx.audit(
            session,
            operation="user.provision",
            resource_type="user",
            resource_id=user.id,
            detail={"issuer": user.issuer, "subject": user.subject},
        )
        return {"id": user.id, "issuer": user.issuer, "subject": user.subject, "status": user.status}


@router.patch("/admin/users/{user_id}")
def patch_user(ctx: Ctx, user_id: str, body: UserStatusPatch) -> dict[str, Any]:
    """Disabling a user also revokes its outstanding control and download tickets (§13.2)."""
    ctx.require(Permission.ADMIN_USERS)
    with ctx.session() as session:
        user = UserRepository(session, ctx.tenant_id).set_status(user_id, status=body.status)
        if user is None:
            raise ApiError(ErrorCode.NOT_FOUND, "User not found")
        ctx.audit(
            session, operation="user.status", resource_type="user", resource_id=user_id, detail={"status": body.status}
        )
        payload = {"id": user.id, "subject": user.subject, "status": user.status}
    get_ticket_store().revoke_actor(user_id)
    return payload


# ------------------------------------------------------------------------ helpers


def _require_tenant_member(session, tenant_id: str, user_id: str) -> None:
    """Membership is checked against the tenant, so a user id cannot be pasted in from elsewhere."""
    if UserRepository(session, tenant_id).by_id(user_id) is None:
        raise ApiError(ErrorCode.NOT_FOUND, "User not found")
    row = session.scalar(
        select(TenantMembership).where(TenantMembership.tenant_id == tenant_id, TenantMembership.user_id == user_id)
    )
    if row is None:
        raise ApiError(ErrorCode.FORBIDDEN, "That user does not belong to your tenant")


def _seconds_until(value: datetime | None) -> int | None:
    if value is None:
        return None
    moment = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    seconds = int((moment - utcnow()).total_seconds())
    if seconds <= 0:
        raise ApiError(ErrorCode.VALIDATION_ERROR, "The grant expiry must be in the future")
    return seconds


def _grant_payload(row: Any) -> dict[str, Any]:
    return {
        "user_id": row.user_id,
        "project_id": row.project_id,
        "permission": row.permission,
        "granted_by": row.granted_by,
        "reason": row.reason,
        "expires_at": _iso(row.expires_at),
        "created_at": _iso(row.created_at),
    }


def _project_payload(project: Project, ctx: Ctx) -> dict[str, Any]:
    role = ctx.identity.role_in(project.id) or ctx.identity.role_in(None)
    return {
        "id": project.id,
        "name": project.name,
        "display_name": project.display_name,
        "description": project.description,
        "quota": dict(project.quota or {}),
        "settings": dict(project.settings or {}),
        "archived": project.archived_at is not None,
        "row_version": int(project.row_version or 1),
        "role": role.value if role is not None else None,
        "created_at": _iso(project.created_at),
        "updated_at": _iso(project.updated_at),
    }


def _iso(value: Any) -> str | None:
    return value.isoformat() if hasattr(value, "isoformat") else None
