"""Case authoring: store an immutable revision and ask the compiler for an IR (§6.1, §13.2).

Saving a case never compiles inline. The revision row and its `compile.request` outbox event commit
together, and the compile worker picks it up from the queue — a model call must not hold up a request.
"""

from __future__ import annotations

from ..config import Settings, get_settings
from ..db.base import get_database
from ..domain.errors import ApiError, ErrorCode
from ..orchestrator.events import COMPILE_REQUEST
from ..repositories.cases import CaseRepository, CompileRepository, TagRepository
from ..repositories.outbox import OutboxRepository


class CaseService:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def create(
        self,
        *,
        tenant_id: str,
        project_id: str,
        name: str,
        markdown: str,
        dsl_version: str,
        title: str | None,
        created_by: str | None,
        tags: list[str] | None = None,
    ) -> dict[str, object]:
        with get_database().session(tenant_id) as session:
            case, revision = CaseRepository(session, tenant_id).create(
                project_id=project_id,
                name=name,
                markdown=markdown,
                dsl_version=dsl_version,
                title=title,
                created_by=created_by,
            )
            if tags:
                TagRepository(session, tenant_id).set_case_tags(case, tags)
            request_compile(session, tenant_id, project_id=project_id, revision_id=revision.id, created_by=created_by)
            session.commit()
            return {
                "case_id": case.id,
                "revision_id": revision.id,
                "revision_no": revision.version,
                "source_digest": revision.source_digest,
            }

    def add_revision(
        self,
        *,
        tenant_id: str,
        case_id: str,
        markdown: str,
        dsl_version: str,
        title: str | None,
        created_by: str | None,
        expected_row_version: int | None = None,
    ) -> dict[str, object]:
        with get_database().session(tenant_id) as session:
            cases = CaseRepository(session, tenant_id)
            case = cases.by_id(case_id)
            if case is None:
                raise ApiError(ErrorCode.NOT_FOUND, "Case not found in this tenant")
            revision = cases.add_revision(
                case,
                markdown=markdown,
                dsl_version=dsl_version,
                title=title,
                created_by=created_by,
                expected_row_version=expected_row_version,
            )
            request_compile(
                session, tenant_id, project_id=case.project_id, revision_id=revision.id, created_by=created_by
            )
            session.commit()
            return {
                "case_id": case.id,
                "revision_id": revision.id,
                "revision_no": revision.version,
                "source_digest": revision.source_digest,
            }

    def compile_result(self, session, *, tenant_id: str, revision_id: str) -> dict[str, object] | None:
        """What the compiler concluded for a revision, or nothing if it has not produced an artifact yet.

        A save queues a compile, so "no artifact yet" is a normal transient state of the case view, not
        a missing resource — reading a case must never 404 because the worker has not caught up (§6.1).
        """
        artifact = CompileRepository(session, tenant_id).latest(revision_id)
        if artifact is None:
            return None
        return {
            "artifact_id": artifact.id,
            "status": artifact.status,
            "compiler_mode": artifact.compiler_mode,
            "model": artifact.model,
            "diagnostics": artifact.diagnostics or [],
            "review_items": artifact.review_items or [],
            "ir": artifact.ir,
            "ir_digest": artifact.ir_digest,
            "confirmed_at": artifact.confirmed_at.isoformat() if artifact.confirmed_at else None,
        }


def request_compile(
    session,
    tenant_id: str,
    *,
    project_id: str,
    revision_id: str,
    created_by: str | None,
    force: bool = False,
    use_ai: bool = False,
) -> None:
    """Queue a compilation for the revision, inside the caller's transaction.

    The artifact row itself is created by the worker, and the discriminator is the revision digest,
    so saving the same text twice cannot start two compiles (§6.1). The AI path is opt-in: no save
    puts case text in front of a model unless the caller asked for it (§6.2).
    """
    revision = CaseRepository(session, tenant_id).require_revision(revision_id)
    OutboxRepository(session, tenant_id).enqueue(
        aggregate_id=revision.id,
        event_type=COMPILE_REQUEST,
        payload={
            "tenant_id": tenant_id,
            "project_id": project_id,
            "revision_id": revision.id,
            "requested_by": created_by,
            "force": bool(force),
            "use_ai": bool(use_ai),
        },
        discriminator=f"{COMPILE_REQUEST}:{revision.id}:{revision.source_digest}:{int(force)}:{int(bool(use_ai))}",
    )


def compile_now(
    session,
    tenant_id: str,
    *,
    project_id: str,
    revision_id: str,
    created_by: str | None,
    use_ai: bool = False,
    force: bool = False,
) -> str:
    """Explicit recompile: reserve the artifact row first so the response can carry its id (§13.2).

    The worker creates the same row keyed the same way, so pre-creating here cannot fork a second
    compilation of the same source.
    """
    from ..ir.models import COMPILER_VERSION
    from ..repositories.cases import CompileRepository, compile_dedupe_key

    revision = CaseRepository(session, tenant_id).require_revision(revision_id)
    artifact = CompileRepository(session, tenant_id).create_pending(
        project_id=project_id,
        revision_id=revision.id,
        source_digest_value=revision.source_digest,
        compiler_version=COMPILER_VERSION,
        dedupe_key=compile_dedupe_key(
            tenant_id, revision.id, revision.source_digest, compiler_version=COMPILER_VERSION, use_ai=use_ai
        ),
    )
    request_compile(
        session,
        tenant_id,
        project_id=project_id,
        revision_id=revision.id,
        created_by=created_by,
        force=force,
        use_ai=use_ai,
    )
    return artifact.id
