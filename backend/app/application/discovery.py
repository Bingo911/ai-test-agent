"""Who the caller is and what they may reach, read with a bound on rows (§6.2, §11).

These are the three discovery pages an assistant starts from: the tenants a subject belongs to, the
projects visible inside the selected tenant, and the environments of one project. All three do their
filtering in SQL *before* the page limit, because "permission filtering runs before paging" (§6.2) is
what stops a caller with 5,000 invisible projects from ever seeing a short page.

The rows returned are the stored facts, not a projection of them: whether a name may be shown, and
which of the caller's abilities to list, are the adapter's policy decision (§5.5), and this layer has
no business making it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db.base import coerce_utc
from ..db.models import (
    AppUser,
    Environment,
    EnvironmentRevision,
    Project,
    ProjectMembership,
    Tenant,
    TenantMembership,
)
from ..domain.enums import Role
from ..domain.mcp_policy import read_policy
from ..repositories.base import keyset_earlier, keyset_later


@dataclass(frozen=True)
class TenantRow:
    tenant_id: str
    name: str
    display_name: str | None
    role: Role | None
    #: The keyset position of this row, so the adapter can mint a cursor without reading the columns back.
    position: tuple[str, str]


@dataclass(frozen=True)
class ProjectRow:
    project_id: str
    name: str
    display_name: str | None
    mcp_enabled: bool
    position: tuple[str, str]


@dataclass(frozen=True)
class EnvironmentRow:
    environment_id: str
    name: str
    current_revision_id: str | None
    revision_version: int | None
    row_version: int
    position: tuple[str, str]


def tenant_page(
    session: Session,
    *,
    issuer: str,
    subject: str,
    limit: int,
    after_name: str | None = None,
    after_id: str | None = None,
) -> list[TenantRow]:
    """The tenants this subject belongs to, in name order.

    Membership rows are keyed by the caller's own `(issuer, subject)` and nothing else, which is why
    this is the one page allowed to be read without a selected tenant: it is the read that answers
    "which tenant should I select". A subject an administrator never provisioned has no rows here, and
    the refusal for that was already made when the identity was resolved.
    """
    statement = (
        select(Tenant.id, Tenant.name, Tenant.display_name, TenantMembership.role)
        .join(TenantMembership, TenantMembership.tenant_id == Tenant.id)
        .join(AppUser, AppUser.id == TenantMembership.user_id)
        .where(
            AppUser.issuer == issuer,
            AppUser.subject == subject,
            AppUser.status == "ACTIVE",
            Tenant.status == "ACTIVE",
        )
        .order_by(Tenant.name.asc(), Tenant.id.asc())
        .limit(limit)
    )
    if after_name is not None and after_id is not None:
        statement = statement.where(keyset_later(Tenant.name, Tenant.id, after=after_name, after_id=after_id))
    rows = []
    for tenant_id, name, display_name, role in session.execute(statement):
        rows.append(
            TenantRow(
                tenant_id=str(tenant_id),
                name=str(name),
                display_name=display_name,
                role=_role(role),
                position=(str(name), str(tenant_id)),
            )
        )
    return rows


def project_page(
    session: Session,
    *,
    tenant_id: str,
    user_id: str,
    tenant_role: Role | None,
    limit: int,
    after_created_at: datetime | None = None,
    after_id: str | None = None,
) -> list[ProjectRow]:
    """Projects the caller may see, newest first, with the MCP gate already resolved.

    A tenant-wide role reaches every project in the tenant; anyone else reaches only their own
    membership rows, expressed as a subquery so the bound is the page size rather than the number of
    projects the caller happens to belong to.
    """
    conditions = [Project.tenant_id == tenant_id, Project.archived_at.is_(None)]
    if tenant_role is None:
        member_ids = select(ProjectMembership.project_id).where(
            ProjectMembership.tenant_id == tenant_id, ProjectMembership.user_id == user_id
        )
        conditions.append(Project.id.in_(member_ids))
    statement = (
        select(Project.id, Project.name, Project.display_name, Project.settings, Project.created_at)
        .where(*conditions)
        .order_by(Project.created_at.desc(), Project.id.desc())
        .limit(limit)
    )
    if after_created_at is not None and after_id is not None:
        statement = statement.where(
            keyset_earlier(Project.created_at, Project.id, after=after_created_at, after_id=after_id)
        )
    rows = []
    for project_id, name, display_name, settings, created_at in session.execute(statement):
        rows.append(
            ProjectRow(
                project_id=str(project_id),
                name=str(name),
                display_name=display_name,
                mcp_enabled=read_policy(settings if isinstance(settings, dict) else None).policy.enabled,
                position=(moment_position(created_at), str(project_id)),
            )
        )
    return rows


def environment_page(
    session: Session,
    *,
    tenant_id: str,
    project_id: str,
    limit: int,
    after_created_at: datetime | None = None,
    after_id: str | None = None,
) -> list[EnvironmentRow]:
    """One project's live environments, newest first.

    Archived environments are left out rather than flagged: the DTO this page answers with has no field
    to say "archived" (§6.6), and an assistant that picks one is only going to be refused at run time
    (§6.4 requires the environment not be archived).
    """
    statement = (
        select(
            Environment.id,
            Environment.environment_name,
            Environment.current_revision_id,
            EnvironmentRevision.version,
            Environment.row_version,
            Environment.created_at,
        )
        .outerjoin(
            EnvironmentRevision,
            (EnvironmentRevision.id == Environment.current_revision_id)
            & (EnvironmentRevision.tenant_id == Environment.tenant_id),
        )
        .where(
            Environment.tenant_id == tenant_id,
            Environment.project_id == project_id,
            Environment.archived_at.is_(None),
        )
        .order_by(Environment.created_at.desc(), Environment.id.desc())
        .limit(limit)
    )
    if after_created_at is not None and after_id is not None:
        statement = statement.where(
            keyset_earlier(Environment.created_at, Environment.id, after=after_created_at, after_id=after_id)
        )
    rows = []
    for environment_id, name, revision_id, version, row_version, created_at in session.execute(statement):
        rows.append(
            EnvironmentRow(
                environment_id=str(environment_id),
                name=str(name),
                current_revision_id=revision_id,
                revision_version=version,
                row_version=int(row_version or 1),
                position=(moment_position(created_at), str(environment_id)),
            )
        )
    return rows


def _role(value: object) -> Role | None:
    """An unparseable stored role is no role at all, so it widens nothing (§14.1)."""
    try:
        return Role(str(value))
    except ValueError:
        return None


def moment_position(value: datetime | None) -> str:
    """The timestamp half of a keyset position, as the text a cursor carries.

    `created_at` is not nullable, so a row that cannot produce one is a broken record; saying so here
    beats minting a cursor the next page would refuse for a reason the caller cannot act on.
    """
    moment = coerce_utc(value)
    if moment is None:
        raise ValueError("a project, environment or case row without a creation time cannot be paged")
    return moment.isoformat()
