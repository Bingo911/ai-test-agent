"""Cases, revisions, tags and compile artifacts (§3.1, §3.2, §6.2)."""

from __future__ import annotations

import hashlib
from collections.abc import Sequence

from sqlalchemy import Select, and_, func, or_, select

from ..db.base import new_id, utcnow
from ..db.models import CaseRevision, CaseTag, CompileArtifact, Tag, TestCase
from ..domain.enums import CompileStatus
from ..domain.errors import ApiError, ErrorCode
from .base import Scoped


def source_digest(markdown: str) -> str:
    """Digest of the revision text with newlines normalised to LF (§5.1 wording)."""
    return "sha256:" + hashlib.sha256(markdown.replace("\r\n", "\n").encode("utf-8")).hexdigest()


def compile_dedupe_key(
    tenant_id: str, revision_id: str, revision_digest: str, *, compiler_version: str, use_ai: bool
) -> str:
    """One artifact per (revision, text, compiler version, AI policy) (§6.2).

    The policy is part of the identity: a deterministic request must not be answered with the artifact an
    earlier AI-assisted compile produced, and a save must not reuse the row an explicit recompile made.
    """
    return f"{tenant_id}:{revision_id}:{revision_digest}:{compiler_version}:{'ai' if use_ai else 'det'}"


def latest_attempt(statement: Select) -> Select:
    """Pin a compile-artifact statement to "the newest attempt", with the tiebreaker that makes it one row.

    `created_at DESC, id DESC` is the platform's only reading of *latest* (§6.3): attempts can share a
    timestamp, and a bare `created_at DESC` then leaves `LIMIT 1` free to answer differently on two runs of
    the same query. The `id` breaks the tie so the answer is stable, without claiming an id orders attempts
    in time. Every reader of "the newest attempt" goes through here - the repository and the MCP case page
    alike - so a client that was handed an artifact id is never told it is something else.
    """
    return statement.order_by(CompileArtifact.created_at.desc(), CompileArtifact.id.desc()).limit(1)


def is_executable(artifact: CompileArtifact) -> bool:
    """§3.2's "may a run use this", read off the row the caller is already holding.

    `CompileRepository.executable_for_revision` states the same rule in SQL, and the two are tested against
    each other. This form is still needed because a run may pin an *older* attempt by id (§6.4), and the SQL
    form - which searches for the newest executable one - would answer false for a confirmed artifact that
    a newer failed attempt happens to sit on top of.
    """
    if artifact.status == CompileStatus.SUCCEEDED.value:
        return True
    return artifact.status == CompileStatus.NEEDS_REVIEW.value and artifact.confirmed_at is not None


class CaseRepository(Scoped[TestCase]):
    model = TestCase

    def create(
        self,
        *,
        project_id: str,
        name: str,
        markdown: str,
        dsl_version: str,
        title: str | None,
        created_by: str | None,
        description: str | None = None,
    ) -> tuple[TestCase, CaseRevision]:
        case = TestCase(
            id=new_id(),
            tenant_id=self.tenant_id,
            project_id=project_id,
            name=name,
            description=description,
            row_version=1,
            created_by=created_by,
        )
        self.session.add(case)
        self.session.flush()
        revision = self.add_revision(
            case,
            markdown=markdown,
            dsl_version=dsl_version,
            title=title,
            created_by=created_by,
        )
        return case, revision

    def add_revision(
        self,
        case: TestCase,
        *,
        markdown: str,
        dsl_version: str,
        title: str | None,
        created_by: str | None,
        expected_row_version: int | None = None,
    ) -> CaseRevision:
        """Save always appends an immutable revision; edits are guarded by optimistic locking (§3.2)."""
        if expected_row_version is not None and case.row_version != expected_row_version:
            raise ApiError(
                ErrorCode.VERSION_CONFLICT,
                "The case changed since you loaded it; reload and merge your edit.",
                details={"expected_row_version": expected_row_version, "current_row_version": case.row_version},
            )
        version = (
            int(
                self.session.scalar(
                    select(func.max(CaseRevision.version)).where(
                        CaseRevision.tenant_id == self.tenant_id, CaseRevision.case_id == case.id
                    )
                )
                or 0
            )
            + 1
        )
        revision = CaseRevision(
            id=new_id(),
            tenant_id=self.tenant_id,
            project_id=case.project_id,
            case_id=case.id,
            version=version,
            markdown=markdown,
            source_digest=source_digest(markdown),
            dsl_version=dsl_version,
            title=title,
            created_by=created_by,
        )
        self.session.add(revision)
        case.current_revision_id = revision.id
        case.row_version = (case.row_version or 1) + 1
        self.session.flush()
        return revision

    def rename(self, case: TestCase, *, name: str, description: str | None) -> TestCase:
        case.name = name
        if description is not None:
            case.description = description
        case.row_version = (case.row_version or 1) + 1
        self.session.flush()
        return case

    def archive(self, case: TestCase) -> TestCase:
        case.archived_at = utcnow()
        case.row_version = (case.row_version or 1) + 1
        self.session.flush()
        return case

    def list(
        self,
        *,
        project_id: str,
        tag_ids: Sequence[str] = (),
        search: str | None = None,
        include_archived: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[TestCase], int]:
        conditions = [TestCase.tenant_id == self.tenant_id, TestCase.project_id == project_id]
        if not include_archived:
            conditions.append(TestCase.archived_at.is_(None))
        if search:
            conditions.append(TestCase.name.ilike(f"%{search}%"))  # type: ignore[attr-defined]
        wanted = select(TestCase.id).where(*conditions)
        if tag_ids:
            # `in_` rather than a join in the page statement: a case carrying two of the requested tags
            # is still one row. `distinct` here is what lets the same statement answer for the count, so
            # `total` cannot disagree with what the page lists.
            wanted = (
                wanted.join(
                    CaseTag,
                    (CaseTag.case_id == TestCase.id) & (CaseTag.tenant_id == TestCase.tenant_id),  # type: ignore[arg-type]
                )
                .where(CaseTag.tag_id.in_(list(tag_ids)))
                .distinct()
            )
        total = self.session.scalar(select(func.count()).select_from(wanted.subquery())) or 0
        stmt = (
            select(TestCase)
            .where(TestCase.id.in_(wanted))
            .order_by(TestCase.updated_at.desc())
            .limit(limit)
            .offset(offset)
        )
        return list(self.session.scalars(stmt).all()), int(total)

    def revision(self, revision_id: str) -> CaseRevision | None:
        return self.session.scalar(
            select(CaseRevision).where(CaseRevision.tenant_id == self.tenant_id, CaseRevision.id == revision_id)
        )

    def require_revision(self, revision_id: str) -> CaseRevision:
        revision = self.revision(revision_id)
        if revision is None:
            raise ApiError(ErrorCode.NOT_FOUND, f"Case revision {revision_id} not found in this tenant")
        return revision

    def revisions(self, case_id: str) -> list[CaseRevision]:
        return list(
            self.session.scalars(
                select(CaseRevision)
                .where(CaseRevision.tenant_id == self.tenant_id, CaseRevision.case_id == case_id)
                .order_by(CaseRevision.version.desc())
            ).all()
        )


class TagRepository(Scoped[Tag]):
    model = Tag

    def ensure(self, project_id: str, name: str) -> Tag:
        cleaned = name.strip()[:60]
        if not cleaned:
            raise ApiError(ErrorCode.VALIDATION_ERROR, "Tag name must not be empty")
        existing = self.session.scalar(
            select(Tag).where(Tag.tenant_id == self.tenant_id, Tag.project_id == project_id, Tag.name == cleaned)
        )
        if existing is not None:
            return existing
        tag = Tag(id=new_id(), tenant_id=self.tenant_id, project_id=project_id, name=cleaned)
        self.session.add(tag)
        self.session.flush()
        return tag

    def for_project(self, project_id: str) -> list[Tag]:
        return list(
            self.session.scalars(
                select(Tag).where(Tag.tenant_id == self.tenant_id, Tag.project_id == project_id).order_by(Tag.name)
            ).all()
        )

    def set_case_tags(self, case: TestCase, tag_names: Sequence[str]) -> list[Tag]:
        tags = [self.ensure(case.project_id, name) for name in tag_names]
        self.session.execute(
            CaseTag.__table__.delete().where(CaseTag.tenant_id == self.tenant_id, CaseTag.case_id == case.id)
        )
        for tag in tags:
            self.session.add(CaseTag(id=new_id(), tenant_id=self.tenant_id, case_id=case.id, tag_id=tag.id))
        self.session.flush()
        return tags

    def tag_ids_for_case(self, case_id: str) -> list[str]:
        return list(
            self.session.scalars(
                select(CaseTag.tag_id).where(CaseTag.tenant_id == self.tenant_id, CaseTag.case_id == case_id)
            ).all()
        )


class CompileRepository(Scoped[CompileArtifact]):
    model = CompileArtifact

    def create_pending(
        self,
        *,
        project_id: str,
        revision_id: str,
        source_digest_value: str,
        compiler_version: str,
        dedupe_key: str,
    ) -> CompileArtifact:
        existing = self.session.scalar(select(CompileArtifact).where(CompileArtifact.dedupe_key == dedupe_key))
        if existing is not None:
            return existing
        artifact = CompileArtifact(
            id=new_id(),
            tenant_id=self.tenant_id,
            project_id=project_id,
            revision_id=revision_id,
            status=CompileStatus.PENDING.value,
            source_digest=source_digest_value,
            compiler_version=compiler_version,
            dedupe_key=dedupe_key,
        )
        self.session.add(artifact)
        self.session.flush()
        return artifact

    def record_result(
        self,
        artifact: CompileArtifact,
        *,
        status: str,
        ir: dict | None,
        ir_digest: str | None,
        diagnostics: list[dict],
        review_items: list[dict],
        usage: dict,
        model: str | None,
        prompt_version: str | None,
        compiler_mode: str,
        error_code: str | None = None,
    ) -> CompileArtifact:
        artifact.status = status
        artifact.ir = ir
        artifact.ir_digest = ir_digest
        artifact.diagnostics = diagnostics
        artifact.review_items = review_items
        artifact.usage = usage
        artifact.model = model
        artifact.prompt_version = prompt_version
        artifact.compiler_mode = compiler_mode
        artifact.error_code = error_code
        self.session.flush()
        return artifact

    def confirm(self, artifact: CompileArtifact, *, actor_id: str, expected_source_digest: str) -> CompileArtifact:
        """NEEDS_REVIEW becomes executable only after a human confirmed the mapping (§3.2)."""
        if artifact.status not in (CompileStatus.NEEDS_REVIEW.value, CompileStatus.SUCCEEDED.value):
            raise ApiError(ErrorCode.CONFLICT, f"Compile status {artifact.status} cannot be confirmed")
        if artifact.source_digest != expected_source_digest:
            raise ApiError(
                ErrorCode.COMPILE_STALE_DIGEST,
                "The case text changed after this compile; recompile before confirming.",
            )
        artifact.confirmed_by = actor_id
        artifact.confirmed_at = utcnow()
        self.session.flush()
        return artifact

    def latest(self, revision_id: str) -> CompileArtifact | None:
        return self.session.scalar(
            latest_attempt(
                select(CompileArtifact).where(
                    CompileArtifact.tenant_id == self.tenant_id, CompileArtifact.revision_id == revision_id
                )
            )
        )

    def executable_for_revision(self, revision_id: str) -> CompileArtifact | None:
        """A run may only use a clean compile, or a review one a human has confirmed (§3.2, §6.2)."""
        return self.session.scalar(
            latest_attempt(
                select(CompileArtifact).where(
                    CompileArtifact.tenant_id == self.tenant_id,
                    CompileArtifact.revision_id == revision_id,
                    or_(
                        CompileArtifact.status == CompileStatus.SUCCEEDED.value,
                        and_(
                            CompileArtifact.status == CompileStatus.NEEDS_REVIEW.value,
                            CompileArtifact.confirmed_at.isnot(None),
                        ),
                    ),
                )
            )
        )
