"""Case and compile reads that stay bounded by the page they answer for (§6.2, §6.3, §11).

The REST layer pages cases by `updated_at` with an offset and reads each row's tags, revision and newest
compile one statement at a time. That shape cannot be reused here: an MCP page is continued by a keyset
cursor, so its order has to be a stable `(created_at, id)` pair, and a query per row would make a page
cost `limit × several` round trips instead of two. So this module answers with one statement per page plus
one for the page's tags, and the "newest compile attempt" it reports is a correlated `LIMIT 1` on the same
ordering the repository uses (§6.3) - never a second, looser reading of it.

What comes back is stored rows, not projections: whether a name or a digest may be sent to a model is the
adapter's policy decision (§5.5), and a caller that had to re-read a row to answer that question is a
caller that will forget to.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from ..db.models import CaseRevision, CaseTag, CompileArtifact, Tag, TestCase
from ..repositories.base import keyset_earlier
from ..repositories.cases import latest_attempt
from .discovery import moment_position


@dataclass(frozen=True)
class CaseRow:
    """One case as its stored facts, with the current revision and its newest compile attempt folded in."""

    case_id: str
    project_id: str
    name: str
    description: str | None
    current_revision_id: str | None
    row_version: int
    archived: bool
    revision_version: int | None
    title: str | None
    source_digest: str | None
    dsl_version: str | None
    compile_status: str | None
    compile_artifact_id: str | None
    tags: list[str]
    position: tuple[str, str]


def case_page(
    session: Session,
    *,
    tenant_id: str,
    project_id: str,
    limit: int,
    search: str | None = None,
    tag_ids: Sequence[str] = (),
    after_created_at: datetime | None = None,
    after_id: str | None = None,
) -> list[CaseRow]:
    """One project's cases, newest first, with every filter applied in SQL before the page is taken (§6.2).

    Archived cases stay in the page and are flagged rather than dropped: `CaseRef` carries `archived`
    precisely so a client can tell "this exists but is closed" from "this does not exist", and a list that
    quietly omitted them would read as a project having fewer cases than it has.
    """
    statement = case_page_statement(
        tenant_id=tenant_id,
        project_id=project_id,
        limit=limit,
        search=search,
        tag_ids=tag_ids,
        after_created_at=after_created_at,
        after_id=after_id,
    )
    return _with_tags(session, tenant_id=tenant_id, rows=list(session.execute(statement)))


def case_page_statement(
    *,
    tenant_id: str,
    project_id: str,
    limit: int,
    search: str | None = None,
    tag_ids: Sequence[str] = (),
    after_created_at: datetime | None = None,
    after_id: str | None = None,
) -> Select:
    """The page's statement, built separately because §11's index contract is a property of this SQL.

    A page that sorts the whole project to find twenty rows is not bounded in the way the design promises,
    and that is only visible in the plan. Handing the statement back is what lets that be asserted without
    a second, looser copy of the query living in a test.
    """
    conditions = [TestCase.tenant_id == tenant_id, TestCase.project_id == project_id]
    if tag_ids:
        # A subquery rather than a join: a case carrying two of the wanted tags is still one row, and a
        # join would need `distinct`, which a keyset page cannot carry through its limit.
        conditions.append(
            TestCase.id.in_(
                select(CaseTag.case_id).where(
                    CaseTag.tenant_id == tenant_id, CaseTag.tag_id.in_(list(tag_ids))
                )
            )
        )
    if search:
        conditions.append(_name_like(search))
    statement = _selection().where(*conditions).order_by(TestCase.created_at.desc(), TestCase.id.desc()).limit(limit)
    if after_created_at is not None and after_id is not None:
        statement = statement.where(
            keyset_earlier(TestCase.created_at, TestCase.id, after=after_created_at, after_id=after_id)
        )
    return statement


def case_by_id(session: Session, *, tenant_id: str, case_id: str) -> CaseRow | None:
    """One case, or nothing: another tenant's case and no case at all are the same answer (§14.1)."""
    rows = list(
        session.execute(_selection().where(TestCase.tenant_id == tenant_id, TestCase.id == case_id).limit(1))
    )
    return None if not rows else _with_tags(session, tenant_id=tenant_id, rows=rows)[0]


def tag_ids_for_names(
    session: Session, *, tenant_id: str, project_id: str, names: Sequence[str]
) -> tuple[list[str], list[str]]:
    """Resolve tag *names* to ids inside one project, and report the names that resolved to nothing.

    A caller filters by name because ids do not appear in a model's conversation, so the resolution happens
    here rather than in the adapter. Handing the unmatched names back instead of dropping them is the point:
    an assistant that asked for `login` and was filtered on a typo would conclude the project holds no such
    case, which is a wrong answer rather than a short one.
    """
    wanted = sorted({name.strip().lower() for name in names if name and name.strip()})
    if not wanted:
        return [], []
    found = {
        str(name): str(tag_id)
        for tag_id, name in session.execute(
            select(Tag.id, func.lower(Tag.name)).where(
                Tag.tenant_id == tenant_id, Tag.project_id == project_id, func.lower(Tag.name).in_(wanted)
            )
        )
    }
    return sorted(found.values()), [name for name in wanted if name not in found]


def revision_markdown(session: Session, *, tenant_id: str, revision_id: str) -> tuple[str, int] | None:
    """The revision's text and its UTF-8 size, read only when the answer is actually going to carry it."""
    text = session.scalar(
        select(CaseRevision.markdown).where(CaseRevision.tenant_id == tenant_id, CaseRevision.id == revision_id)
    )
    if text is None:
        return None
    return str(text), len(str(text).encode("utf-8"))


def _selection() -> Select:
    """The page and the single-row read share one column list, and therefore one compile-attempt rule."""
    return (
        select(
            TestCase.id,
            TestCase.project_id,
            TestCase.name,
            TestCase.description,
            TestCase.current_revision_id,
            TestCase.row_version,
            TestCase.archived_at,
            TestCase.created_at,
            CaseRevision.version,
            CaseRevision.title,
            CaseRevision.source_digest,
            CaseRevision.dsl_version,
            _newest(CompileArtifact.status).label("compile_status"),
            _newest(CompileArtifact.id).label("compile_artifact_id"),
        )
        .outerjoin(
            CaseRevision,
            (CaseRevision.id == TestCase.current_revision_id) & (CaseRevision.tenant_id == TestCase.tenant_id),
        )
    )


def _newest(column: Any) -> Select:
    """`LIMIT 1` on the newest attempt for this row's current revision, ordered exactly as §6.3 says.

    Correlated per row rather than one grouped query for the page, because this is the shape the composite
    `(tenant_id, revision_id, created_at, id)` index serves as a first-row fetch: a grouped query would read
    every attempt of every case on the page to work out which one was last.
    """
    return (
        latest_attempt(select(column))
        .where(
            CompileArtifact.tenant_id == TestCase.tenant_id,
            CompileArtifact.revision_id == TestCase.current_revision_id,
        )
        .correlate(TestCase)
        .scalar_subquery()
    )


def _name_like(search: str) -> Any:
    """A literal substring match on the case name, wildcards and all.

    `LIKE` would otherwise read a `%` or `_` in the search term as a pattern, and an assistant that looked
    for `50%_off` and matched every case in the project has no way to tell it asked the wrong question.
    """
    escaped = search.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")
    return TestCase.name.ilike(f"%{escaped}%", escape="\\")  # type: ignore[attr-defined]


def _with_tags(session: Session, *, tenant_id: str, rows: Sequence[Any]) -> list[CaseRow]:
    """The page's tags, in one bounded statement, folded into the rows that asked for them."""
    names: dict[str, list[str]] = {}
    case_ids = [str(row[0]) for row in rows]
    if case_ids:
        for case_id, tag in session.execute(
            select(CaseTag.case_id, Tag.name)
            .join(Tag, (Tag.id == CaseTag.tag_id) & (Tag.tenant_id == CaseTag.tenant_id))
            .where(CaseTag.tenant_id == tenant_id, CaseTag.case_id.in_(case_ids))
            .order_by(Tag.name.asc(), CaseTag.tag_id.asc())
        ):
            names.setdefault(str(case_id), []).append(str(tag))
    return [_row(row, names.get(str(row[0]), [])) for row in rows]


def _row(values: Sequence[Any], tags: list[str]) -> CaseRow:
    (
        case_id,
        project_id,
        name,
        description,
        current_revision_id,
        row_version,
        archived_at,
        created_at,
        revision_version,
        title,
        source_digest,
        dsl_version,
        compile_status,
        compile_artifact_id,
    ) = values
    return CaseRow(
        case_id=str(case_id),
        project_id=str(project_id),
        name=str(name),
        description=_text(description),
        current_revision_id=_text(current_revision_id),
        row_version=int(row_version or 1),
        archived=archived_at is not None,
        revision_version=None if revision_version is None else int(revision_version),
        title=_text(title),
        source_digest=_text(source_digest),
        dsl_version=_text(dsl_version),
        compile_status=_text(compile_status),
        compile_artifact_id=_text(compile_artifact_id),
        tags=tags,
        position=(moment_position(created_at), str(case_id)),
    )


def _text(value: Any) -> str | None:
    return None if value is None else str(value)
