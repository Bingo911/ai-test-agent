"""Idempotency, audit and the RBAC lookups that back `Identity` (§9.4, §11.2, §14.1)."""

from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from typing import Any

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import object_session

from ..db.base import coerce_utc, new_id, utcnow
from ..db.models import (
    AppUser,
    AuditLog,
    IdempotencyRecord,
    PermissionGrant,
    Project,
    ProjectMembership,
    Tenant,
    TenantMembership,
)
from ..domain.enums import Permission, Role
from ..domain.errors import ApiError, ErrorCode
from ..domain.mcp_policy import (
    McpPolicy,
    apply_policy_to_settings,
    merge_policy,
    read_policy,
    require_complete_repair,
)
from .base import Scoped


def request_digest(payload: Any) -> str:
    return (
        "sha256:"
        + hashlib.sha256(
            json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
        ).hexdigest()
    )


def _decode_permission(value: Any) -> Permission | None:
    """A stored permission that no longer exists in the enum resolves to nothing, never to access."""
    try:
        return Permission(str(value))
    except ValueError:
        return None


class IdempotencyRepository(Scoped[IdempotencyRecord]):
    model = IdempotencyRecord

    def lookup(self, *, actor_id: str, route: str, key: str, payload: Any) -> IdempotencyRecord | None:
        row = self.session.scalar(
            select(IdempotencyRecord).where(
                IdempotencyRecord.tenant_id == self.tenant_id,
                IdempotencyRecord.actor_id == actor_id,
                IdempotencyRecord.route == route,
                IdempotencyRecord.key == key,
            )
        )
        if row is None:
            return None
        if coerce_utc(row.expires_at) <= utcnow():
            self.session.delete(row)
            self.session.flush()
            return None
        if row.request_digest != request_digest(payload):
            raise ApiError(
                ErrorCode.IDEMPOTENCY_CONFLICT,
                "The same Idempotency-Key was reused with a different request body",
            )
        return row

    def find(self, *, actor_id: str, route: str, key: str) -> IdempotencyRecord | None:
        """The live row for a key, without the body check: used when closing out a known reservation."""
        row = self.session.scalar(
            select(IdempotencyRecord).where(
                IdempotencyRecord.tenant_id == self.tenant_id,
                IdempotencyRecord.actor_id == actor_id,
                IdempotencyRecord.route == route,
                IdempotencyRecord.key == key,
            )
        )
        if row is not None and coerce_utc(row.expires_at) <= utcnow():
            self.session.delete(row)
            self.session.flush()
            return None
        return row

    def reserve(self, *, actor_id: str, route: str, key: str, payload: Any, ttl_hours: int) -> IdempotencyRecord:
        row = IdempotencyRecord(
            id=new_id(),
            tenant_id=self.tenant_id,
            actor_id=actor_id,
            route=route,
            key=key,
            request_digest=request_digest(payload),
            resource_id=None,
            response=None,
            expires_at=_hours_from_now(ttl_hours),
        )
        self.session.add(row)
        self.session.flush()
        return row

    def reserve_or_replay(
        self, *, actor_id: str, route: str, key: str, payload: Any, ttl_hours: int
    ) -> tuple[IdempotencyRecord, dict[str, Any] | None]:
        """Atomically either hand the caller the lock or hand back the stored answer (§13.1).

        A reservation whose first attempt never completed is taken over rather than blocking the retry,
        so a client that lost the response to a network fault is never locked out of its own key.
        """
        existing = self.lookup(actor_id=actor_id, route=route, key=key, payload=payload)
        if existing is None:
            return self.reserve(actor_id=actor_id, route=route, key=key, payload=payload, ttl_hours=ttl_hours), None
        if existing.response is not None:
            return existing, dict(existing.response)
        existing.expires_at = _hours_from_now(ttl_hours)
        self.session.flush()
        return existing, None

    def complete(self, row: IdempotencyRecord, *, resource_id: str, response: dict[str, Any]) -> None:
        """Close out a reservation in *this* session, whoever handed the row over.

        The legacy caller reserved in an earlier transaction, so the object it kept is detached by now;
        writing to a detached row is silently lost, and the key would stay unfinished for the next call
        to run the action a second time.
        """
        target = row if object_session(row) is self.session else self.session.merge(row)
        target.resource_id = resource_id
        target.response = response
        self.session.flush()

    def release(self, row: IdempotencyRecord) -> None:
        """Free the key when the wrapped transaction failed so the caller may retry."""
        self.session.delete(row)
        self.session.flush()


class AuditRepository(Scoped[AuditLog]):
    model = AuditLog

    def append(
        self,
        *,
        operation: str,
        resource_type: str,
        resource_id: str | None = None,
        actor_id: str | None = None,
        project_id: str | None = None,
        request_id: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> AuditLog:
        row = AuditLog(
            id=new_id(),
            tenant_id=self.tenant_id,
            actor_id=actor_id,
            project_id=project_id,
            operation=operation[:80],
            resource_type=resource_type[:48],
            resource_id=resource_id,
            request_id=request_id,
            detail=_redact(detail or {}),
        )
        self.session.add(row)
        self.session.flush()
        return row

    def list(self, *, project_id: str | None = None, limit: int = 100) -> list[AuditLog]:
        conditions = [AuditLog.tenant_id == self.tenant_id]
        if project_id:
            conditions.append(AuditLog.project_id == project_id)
        return list(
            self.session.scalars(
                select(AuditLog).where(*conditions).order_by(AuditLog.created_at.desc()).limit(limit)
            ).all()
        )

    def query(
        self,
        *,
        project_id: str | None = None,
        actor_id: str | None = None,
        operation: str | None = None,
        resource_type: str | None = None,
        resource_id: str | None = None,
        since: Any = None,
        until: Any = None,
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[list[AuditLog], int]:
        """The filtered read behind `GET /projects/{id}/audit-logs` (§13.2): who did what, when."""
        conditions = [AuditLog.tenant_id == self.tenant_id]
        if project_id:
            conditions.append(AuditLog.project_id == project_id)
        if actor_id:
            conditions.append(AuditLog.actor_id == actor_id)
        if operation:
            conditions.append(AuditLog.operation == operation)
        if resource_type:
            conditions.append(AuditLog.resource_type == resource_type)
        if resource_id:
            conditions.append(AuditLog.resource_id == resource_id)
        if since is not None:
            conditions.append(AuditLog.created_at >= since)
        if until is not None:
            conditions.append(AuditLog.created_at <= until)
        total = int(self.session.scalar(select(func.count()).select_from(AuditLog).where(and_(*conditions))) or 0)
        rows = self.session.scalars(
            select(AuditLog).where(and_(*conditions)).order_by(AuditLog.created_at.desc()).limit(limit).offset(offset)
        ).all()
        return list(rows), total


_SECRET_KEYS = {"password", "secret", "value", "ciphertext", "token", "cookie", "authorization", "otp", "api_key"}


def _redact(detail: dict[str, Any]) -> dict[str, Any]:
    cleaned: dict[str, Any] = {}
    for key, value in detail.items():
        if key.lower() in _SECRET_KEYS:
            cleaned[key] = "***"
        elif isinstance(value, dict):
            cleaned[key] = _redact(value)
        elif isinstance(value, list):
            cleaned[key] = [_redact(item) if isinstance(item, dict) else item for item in value]
        else:
            cleaned[key] = value
    return cleaned


class AccessRepository(Scoped[Project]):
    """Membership and grant resolution; the identity is rebuilt per request, never cached (§14.1)."""

    model = Project

    def user(self, user_id: str) -> AppUser | None:
        return self.session.scalar(select(AppUser).where(AppUser.id == user_id, AppUser.status == "ACTIVE"))

    def tenant_by_name(self, name: str) -> Tenant | None:
        return self.session.scalar(select(Tenant).where(Tenant.name == name))

    def tenant(self, tenant_id: str) -> Tenant | None:
        return self.session.scalar(select(Tenant).where(Tenant.id == tenant_id))

    def projects_for_user(self, *, tenant_id: str, user_id: str, tenant_role: Role | None) -> list[Project]:
        conditions = [Project.tenant_id == tenant_id, Project.archived_at.is_(None)]
        if tenant_role is None:
            direct = list(
                self.session.scalars(
                    select(ProjectMembership.project_id).where(
                        ProjectMembership.tenant_id == tenant_id, ProjectMembership.user_id == user_id
                    )
                ).all()
            )
            if not direct:
                return []
            conditions.append(Project.id.in_(direct))
        return list(self.session.scalars(select(Project).where(*conditions).order_by(Project.name)).all())

    def project(self, tenant_id: str, project_id: str) -> Project | None:
        return self.session.scalar(select(Project).where(Project.tenant_id == tenant_id, Project.id == project_id))

    def project_by_name(self, tenant_id: str, name: str) -> Project | None:
        """Archived rows count as taken: `uq_project_active_name` only frees a name when it is deleted."""
        return self.session.scalar(select(Project).where(Project.tenant_id == tenant_id, Project.name == name))

    def grant_rows(self, project_id: str) -> list[PermissionGrant]:
        return list(
            self.session.scalars(
                select(PermissionGrant).where(
                    PermissionGrant.tenant_id == self.tenant_id, PermissionGrant.project_id == project_id
                )
            ).all()
        )

    def tenant_role(self, tenant_id: str, user_id: str) -> Role | None:
        row = self.session.scalar(
            select(TenantMembership.role).where(
                TenantMembership.tenant_id == tenant_id, TenantMembership.user_id == user_id
            )
        )
        return Role(row) if row else None

    def project_role(self, *, tenant_id: str, project_id: str, user_id: str) -> Role | None:
        row = self.session.scalar(
            select(ProjectMembership.role).where(
                ProjectMembership.tenant_id == tenant_id,
                ProjectMembership.project_id == project_id,
                ProjectMembership.user_id == user_id,
            )
        )
        return Role(row) if row else None

    def project_roles(self, tenant_id: str, user_id: str) -> dict[str, Role]:
        """Every project the user belongs to, with its role: one read per request to build an `Identity`."""
        rows = self.session.execute(
            select(ProjectMembership.project_id, ProjectMembership.role).where(
                ProjectMembership.tenant_id == tenant_id, ProjectMembership.user_id == user_id
            )
        ).all()
        resolved: dict[str, Role] = {}
        for project_id, role in rows:
            try:
                resolved[str(project_id)] = Role(role)
            except ValueError:  # a role removed from the enum must not widen anyone's access
                continue
        return resolved

    def grants_by_project(self, *, tenant_id: str, user_id: str) -> dict[str, set[Permission]]:
        """Every live explicit grant, keyed by the project it was given on (§14.1).

        `permission_grant.project_id` is NOT NULL, so a specialist permission (`human_control`,
        `sensitive_artifact_read`) is authority over one project's evidence and pauses and nothing else.
        Flattening these rows into a tenant-wide set would let a grant on one project unlock another
        team's screenshots. A row past `expires_at` is not a permission.
        """
        rows = self.session.execute(
            select(PermissionGrant.project_id, PermissionGrant.permission).where(
                PermissionGrant.tenant_id == tenant_id,
                PermissionGrant.user_id == user_id,
                or_(PermissionGrant.expires_at.is_(None), PermissionGrant.expires_at > utcnow()),
            )
        ).all()
        found: dict[str, set[Permission]] = {}
        for project_id, permission in rows:
            decoded = _decode_permission(permission)
            if decoded is not None:
                found.setdefault(str(project_id), set()).add(decoded)
        return found

    def grants(self, *, tenant_id: str, project_id: str, user_id: str) -> set[Permission]:
        rows = self.session.scalars(
            select(PermissionGrant.permission).where(
                PermissionGrant.tenant_id == tenant_id,
                PermissionGrant.project_id == project_id,
                PermissionGrant.user_id == user_id,
                # A grant past its expiry is not a permission, whatever the row says (§13.2).
                or_(PermissionGrant.expires_at.is_(None), PermissionGrant.expires_at > utcnow()),
            )
        ).all()
        return {permission for permission in map(_decode_permission, rows) if permission is not None}

    def memberships(self, project_id: str) -> list[ProjectMembership]:
        return list(
            self.session.scalars(
                select(ProjectMembership).where(
                    ProjectMembership.tenant_id == self.tenant_id, ProjectMembership.project_id == project_id
                )
            ).all()
        )

    def membership(self, *, project_id: str, user_id: str) -> ProjectMembership | None:
        return self.session.scalar(
            select(ProjectMembership).where(
                ProjectMembership.tenant_id == self.tenant_id,
                ProjectMembership.project_id == project_id,
                ProjectMembership.user_id == user_id,
            )
        )

    def remove_membership(self, *, project_id: str, user_id: str) -> bool:
        row = self.membership(project_id=project_id, user_id=user_id)
        if row is None:
            return False
        self.session.delete(row)
        self.session.flush()
        return True

    def update_project(
        self,
        project_id: str,
        *,
        display_name: str | None = None,
        description: str | None = None,
        quota: dict[str, Any] | None = None,
        settings: dict[str, Any] | None = None,
        archived: bool | None = None,
        expected_row_version: int | None = None,
    ) -> Project:
        """Metadata only: a project's name is its unique key here, so renaming is not an edit (§11.2).

        The row is read *here* and locked before it is compared. An object handed in from a finished
        session is detached, and writing to a detached row answers 200 while changing nothing - and its
        `row_version` is only a snapshot of what that earlier transaction saw, so `If-Match` would be
        guarding nothing at all.
        """
        project = self.session.scalar(
            select(Project)
            .where(Project.tenant_id == self.tenant_id, Project.id == project_id)
            .with_for_update()
        )
        if project is None:
            raise ApiError(ErrorCode.NOT_FOUND, "Project not found in your tenant")
        if expected_row_version is not None and project.row_version != expected_row_version:
            raise ApiError(
                ErrorCode.VERSION_CONFLICT,
                "The project changed since you loaded it; reload before saving.",
                details={"current_row_version": project.row_version},
            )
        if display_name is not None:
            project.display_name = display_name
        if description is not None:
            project.description = description
        if quota is not None:
            project.quota = quota
        if settings is not None:
            project.settings = settings
        if archived is True:
            project.archived_at = utcnow()
        elif archived is False:
            project.archived_at = None
        project.row_version = int(project.row_version or 1) + 1
        self.session.flush()
        return project

    def patch_mcp_policy(
        self,
        project_id: str,
        *,
        patch: dict[str, bool],
        expected_row_version: int | None,
    ) -> tuple[Project, McpPolicy, McpPolicy]:
        """The other settings write entrance: a partial MCP policy update, on the same row lock (§5.5).

        Both writers re-read the row here and compare `If-Match` inside the lock, because a detached
        project read earlier is a snapshot of what some other transaction saw, and comparing against it
        would let a stale policy write succeed. Returns the project and the policy before and after, so
        the caller can audit the pair in this same transaction.
        """
        project = self.session.scalar(
            select(Project)
            .where(Project.tenant_id == self.tenant_id, Project.id == project_id)
            .with_for_update()
        )
        if project is None:
            raise ApiError(ErrorCode.NOT_FOUND, "Project not found in your tenant")
        if expected_row_version is not None and project.row_version != expected_row_version:
            raise ApiError(
                ErrorCode.VERSION_CONFLICT,
                "The project changed since you loaded it; reload before saving.",
                details={"current_row_version": project.row_version},
            )
        view = read_policy(project.settings)
        require_complete_repair(view, patch)
        after = merge_policy(view.policy, patch)
        project.settings = apply_policy_to_settings(project.settings, after)
        project.row_version = int(project.row_version or 1) + 1
        self.session.flush()
        return project, view.policy, after

    def user_by_subject(self, issuer: str, subject: str) -> AppUser | None:
        return self.session.scalar(
            select(AppUser).where(AppUser.issuer == issuer, AppUser.subject == subject, AppUser.status == "ACTIVE")
        )

    def memberships_for_user(self, tenant_id: str, user_id: str) -> list[str]:
        return list(
            self.session.scalars(
                select(ProjectMembership.project_id).where(
                    ProjectMembership.tenant_id == tenant_id, ProjectMembership.user_id == user_id
                )
            ).all()
        )

    def create_project(
        self,
        *,
        tenant_id: str,
        name: str,
        display_name: str | None,
        description: str | None,
        quota: dict[str, Any],
        settings: dict[str, Any],
    ) -> Project:
        project = Project(
            id=new_id(),
            tenant_id=tenant_id,
            name=name,
            display_name=display_name,
            description=description,
            quota=quota,
            settings=settings,
            row_version=1,
        )
        self.session.add(project)
        self.session.flush()
        return project

    def add_membership(self, *, tenant_id: str, project_id: str, user_id: str, role: Role) -> None:
        existing = self.session.scalar(
            select(ProjectMembership).where(
                ProjectMembership.tenant_id == tenant_id,
                ProjectMembership.project_id == project_id,
                ProjectMembership.user_id == user_id,
            )
        )
        if existing is not None:
            existing.role = role.value
        else:
            self.session.add(
                ProjectMembership(
                    id=new_id(), tenant_id=tenant_id, project_id=project_id, user_id=user_id, role=role.value
                )
            )
        self.session.flush()

    def add_grant(
        self,
        *,
        tenant_id: str,
        project_id: str,
        user_id: str,
        permission: Permission,
        granted_by: str | None,
        reason: str | None,
        expires_in_seconds: int | None = None,
    ) -> None:
        existing = self.session.scalar(
            select(PermissionGrant).where(
                PermissionGrant.tenant_id == tenant_id,
                PermissionGrant.project_id == project_id,
                PermissionGrant.user_id == user_id,
                PermissionGrant.permission == permission.value,
            )
        )
        expires_at = utcnow() + timedelta(seconds=expires_in_seconds) if expires_in_seconds else None
        if existing is not None:
            # Re-issuing a grant refreshes it instead of piling up a second row against the same key.
            existing.granted_by = granted_by
            existing.reason = (reason or "")[:300] or None
            existing.expires_at = expires_at
        else:
            self.session.add(
                PermissionGrant(
                    id=new_id(),
                    tenant_id=tenant_id,
                    project_id=project_id,
                    user_id=user_id,
                    permission=permission.value,
                    granted_by=granted_by,
                    reason=(reason or "")[:300] or None,
                    expires_at=expires_at,
                )
            )
        self.session.flush()

    def revoke_grant(self, *, tenant_id: str, project_id: str, user_id: str, permission: Permission) -> bool:
        row = self.session.scalar(
            select(PermissionGrant).where(
                PermissionGrant.tenant_id == tenant_id,
                PermissionGrant.project_id == project_id,
                PermissionGrant.user_id == user_id,
                PermissionGrant.permission == permission.value,
            )
        )
        if row is None:
            return False
        self.session.delete(row)
        self.session.flush()
        return True


def _hours_from_now(hours: int):
    return utcnow() + timedelta(hours=hours)


class UserRepository(Scoped[AppUser]):
    """Platform-level identity admin (§13.2 `GET/PATCH /admin/users`)."""

    model = AppUser

    def base(self) -> Any:
        # `app_user` is deliberately tenant-free: tenants are reached through `tenant_membership`.
        return select(AppUser)

    def by_id(self, row_id: str, *, for_update: bool = False) -> AppUser | None:
        return self.session.scalar(select(AppUser).where(AppUser.id == row_id))

    def list(
        self, *, status: str | None = None, search: str | None = None, limit: int = 50, offset: int = 0
    ) -> tuple[list[AppUser], int]:
        conditions = []
        if status:
            conditions.append(AppUser.status == status)
        if search:
            conditions.append(or_(AppUser.subject.ilike(f"%{search}%"), AppUser.email.ilike(f"%{search}%")))  # type: ignore[attr-defined]
        total = self.session.scalar(select(func.count()).select_from(AppUser).where(*conditions)) or 0
        rows = self.session.scalars(
            select(AppUser).where(*conditions).order_by(AppUser.created_at.desc()).limit(limit).offset(offset)
        ).all()
        return list(rows), int(total)

    def provision(
        self,
        *,
        issuer: str,
        subject: str,
        display_name: str | None,
        email: str | None,
    ) -> AppUser:
        """Create-or-get by `(issuer, subject)`: email is a contact attribute, never a key (§14.1)."""
        user = self.session.scalar(select(AppUser).where(AppUser.issuer == issuer, AppUser.subject == subject))
        if user is None:
            user = AppUser(
                id=new_id(), issuer=issuer, subject=subject, display_name=display_name, email=email, status="ACTIVE"
            )
            self.session.add(user)
            self.session.flush()
        else:
            user.display_name = display_name or user.display_name
            user.email = email or user.email
        return user

    def set_status(self, user_id: str, *, status: str) -> AppUser | None:
        user = self.by_id(user_id)
        if user is None:
            return None
        user.status = status
        self.session.flush()
        return user

    def tenant_memberships(self, user_id: str) -> list[TenantMembership]:
        return list(self.session.scalars(select(TenantMembership).where(TenantMembership.user_id == user_id)).all())
