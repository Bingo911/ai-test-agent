"""RBAC (§14.1). Tenant comes from the authenticated context only, never the request body."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from .enums import ROLE_PERMISSIONS, Permission, Role
from .errors import ApiError, ErrorCode


@dataclass(frozen=True)
class Identity:
    user_id: str
    tenant_id: str
    display_name: str = ""
    issuer: str = "local"
    subject: str = ""
    #: role per project id, plus "*" for tenant-wide roles
    roles: dict[str, Role] = field(default_factory=dict)
    #: explicit permission_grant rows, kept per project because that is the scope they were granted in
    project_grants: dict[str, frozenset[Permission]] = field(default_factory=dict)
    expires_at: datetime | None = None

    def role_in(self, project_id: str | None) -> Role | None:
        if project_id is not None and project_id in self.roles:
            return self.roles[project_id]
        return self.roles.get("*")

    def permissions_in(self, project_id: str | None) -> frozenset[Permission]:
        role = self.role_in(project_id)
        base = ROLE_PERMISSIONS.get(role, frozenset()) if role else frozenset()
        if project_id is None:
            # A specialist grant is authority over one project's pauses and evidence; it never
            # accumulates into tenant-wide authority (§14.1, §14.2).
            return base
        return base | self.project_grants.get(project_id, frozenset())

    def grants_in(self, project_id: str | None) -> frozenset[Permission]:
        if project_id is None:
            return frozenset()
        return self.project_grants.get(project_id, frozenset())

    @property
    def granted_projects(self) -> dict[str, list[str]]:
        """Display-only view for `/whoami`: which project each specialist permission came from."""
        return {
            project_id: sorted(permission.value for permission in value)
            for project_id, value in sorted(self.project_grants.items())
            if value
        }

    def can(self, permission: Permission, project_id: str | None = None) -> bool:
        if self.expires_at is not None and self.expires_at < datetime.now(timezone.utc):
            return False
        return permission in self.permissions_in(project_id)

    def require(self, permission: Permission, project_id: str | None = None) -> None:
        if not self.can(permission, project_id):
            raise ApiError(
                ErrorCode.FORBIDDEN,
                f"Permission '{permission.value}' is required for this operation",
                details={"permission": permission.value, "project_id": project_id},
            )

    @property
    def is_admin(self) -> bool:
        return self.role_in("*") is Role.ADMIN
