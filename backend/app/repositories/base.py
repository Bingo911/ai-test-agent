"""Tenant-scoped data access helpers (§14.2).

Every statement built here carries an explicit `tenant_id` predicate, so a service that
forgets a scope check cannot read another tenant's rows. Cross-tenant writes are additionally
blocked by the composite `(tenant_id, id)` foreign keys in the schema.
"""

from __future__ import annotations

from typing import Any, Generic, TypeVar

from sqlalchemy import Select, and_, or_, select
from sqlalchemy.orm import Session

from ..db.base import utcnow
from ..domain.errors import ApiError, ErrorCode

ModelT = TypeVar("ModelT")


class Scoped(Generic[ModelT]):
    model: type[ModelT]

    def __init__(self, session: Session, tenant_id: str) -> None:
        self.session = session
        self.tenant_id = tenant_id

    def base(self) -> Select:
        return select(self.model).where(self.model.tenant_id == self.tenant_id)  # type: ignore[attr-defined]

    def by_id(self, row_id: str, *, for_update: bool = False) -> ModelT | None:
        stmt = self.base().where(self.model.id == row_id)  # type: ignore[attr-defined]
        if for_update:
            stmt = stmt.with_for_update()
        return self.session.scalar(stmt)

    def require(self, row_id: str, *, for_update: bool = False) -> ModelT:
        row = self.by_id(row_id, for_update=for_update)
        if row is None:
            raise ApiError(ErrorCode.NOT_FOUND, f"{self.model.__name__} {row_id} not found in this tenant")
        return row

    def now(self):
        return utcnow()


def json_copy(value: object) -> object:
    """Return a fresh container so SQLAlchemy observes JSON column mutation."""
    if isinstance(value, dict):
        return {key: json_copy(item) for key, item in value.items()}
    if isinstance(value, list):
        return [json_copy(item) for item in value]
    return value


def keyset_earlier(first: Any, second: Any, *, after: Any, after_id: Any) -> Any:
    """The rows strictly after a `ORDER BY first DESC, second DESC` page's last row.

    Written as the expanded `OR` rather than a row-value comparison because SQLite only learned
    tuple comparison recently and PostgreSQL is the only dialect that has always had it; this form
    plans the same way on both and cannot silently degrade to a scan.
    """
    return or_(first < after, and_(first == after, second < after_id))


def keyset_later(first: Any, second: Any, *, after: Any, after_id: Any) -> Any:
    """The same bound for an `ORDER BY first ASC, second ASC` page, which is what steps use."""
    return or_(first > after, and_(first == after, second > after_id))
