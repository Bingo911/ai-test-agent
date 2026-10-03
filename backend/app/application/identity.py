"""From a proved credential to the authority the platform actually holds (§5.1, §14.1).

A verified signature only proves *who* called. Whether that subject is provisioned, which tenants it
belongs to, what role it holds in a project and which specialist grants were issued to it are database
facts, read again on every call so a revoked membership stops working on the next request rather than
when a token happens to expire.

Each function here takes the session the caller is already using. That is the point: an authorisation
read that opens its own transaction can commit a half-built command, and the identity it returns would
be a snapshot of a moment that has already passed by the time the command runs.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db.models import TenantMembership
from ..domain.errors import ApiError, ErrorCode
from ..domain.rbac import Identity
from ..repositories.platform import AccessRepository, UserRepository


def identity_for(
    session: Session, user: Any, tenant_id: str, *, display: str, issuer: str, subject: str
) -> Identity:
    """Collect the tenant role, every project role and the per-project grants into one snapshot."""
    access = AccessRepository(session, tenant_id)
    tenant_role = access.tenant_role(tenant_id, user.id)
    roles: dict[str, Any] = {"*": tenant_role} if tenant_role is not None else {}
    roles.update(access.project_roles(tenant_id, user.id))
    grants = access.grants_by_project(tenant_id=tenant_id, user_id=user.id)
    return Identity(
        user_id=user.id,
        tenant_id=tenant_id,
        display_name=display,
        issuer=issuer,
        subject=subject,
        roles=roles,
        project_grants={key: frozenset(value) for key, value in grants.items()},
    )


def principal_only(session: Session, *, issuer: str, subject: str) -> Identity:
    """The caller's own identity, with no tenant selected.

    `aita_get_context` asks "which tenants may I see", which is a question about the *subject* rather
    than about a tenant, so it cannot be answered from inside a tenant-scoped transaction. Everything
    this returns is the caller's own: an actor id and nothing else. Roles stay empty, because a role
    belongs to a tenant and none has been chosen yet.
    """
    user = _provisioned_user(session, issuer=issuer, subject=subject)
    return Identity(
        user_id=user.id,
        tenant_id="",
        display_name=user.display_name or "",
        issuer=issuer,
        subject=subject,
        roles={},
        project_grants={},
    )


def resolve_subject(
    session: Session,
    *,
    issuer: str,
    subject: str,
    tenant_hint: str | None = None,
    display_name: str = "",
) -> Identity:
    """Map a verified `(issuer, subject)` onto a provisioned user and one selected tenant.

    The hint narrows a choice the caller is already entitled to make; it never selects a tenant the
    subject does not belong to, and an unknown or unprovisioned identity is refused rather than
    silently scoped to a default.
    """
    user = _provisioned_user(session, issuer=issuer, subject=subject)
    memberships = UserRepository(session, "").tenant_memberships(user.id)
    if not memberships:
        raise ApiError(ErrorCode.FORBIDDEN, "The identity belongs to no tenant")
    wanted = tenant_hint.strip() if tenant_hint else None
    chosen = next((row for row in memberships if wanted is None or row.tenant_id == wanted), None)
    if chosen is None:
        raise ApiError(ErrorCode.FORBIDDEN, "You are not a member of that tenant")
    return identity_for(
        session, user, chosen.tenant_id, display=display_name, issuer=issuer, subject=subject
    )


def _provisioned_user(session: Session, *, issuer: str, subject: str) -> Any:
    """The database row for a verified subject, or the refusal that says it was never enabled (§14.1)."""
    user = AccessRepository(session, "").user_by_subject(issuer, subject)
    if user is None:
        raise ApiError(
            ErrorCode.UNAUTHENTICATED,
            "This identity is not provisioned; an administrator must enable it before sign-in",
        )
    return user


def resolve_user_id(session: Session, *, user_id: str, tenant_id: str) -> Identity:
    """Re-authorise a caller who proved identity with a ticket instead of a bearer (§14.3).

    The ticket only ever says *who*; permissions are still read here, so a revoked grant or a disabled
    account stops working on the next request even while the ticket itself stays valid.
    """
    user = UserRepository(session, "").by_id(user_id)
    if user is None or user.status != "ACTIVE":
        raise ApiError(ErrorCode.FORBIDDEN, "The identity behind the ticket is no longer active")
    membership = session.scalar(
        select(TenantMembership).where(TenantMembership.tenant_id == tenant_id, TenantMembership.user_id == user_id)
    )
    if membership is None:
        raise ApiError(ErrorCode.FORBIDDEN, "You are not a member of that tenant")
    return identity_for(
        session,
        user,
        tenant_id,
        display=user.display_name or "",
        issuer=user.issuer,
        subject=user.subject,
    )
