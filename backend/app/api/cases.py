"""Case authoring, compilation and attachments (§4, §6, §13.2)."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Form, Header, Query, Response, UploadFile
from pydantic import BaseModel, Field, StringConstraints
from sqlalchemy import select

from ..db.models import CaseTag, Tag
from ..domain.enums import CompileStatus, Permission
from ..domain.errors import ApiError, ErrorCode
from ..ir.models import COMPILER_VERSION
from ..repositories.cases import CaseRepository, CompileRepository, TagRepository
from ..repositories.resources import AttachmentRepository
from ..services.attachments import AttachmentService, attachment_payload
from ..services.cases import CaseService, compile_now
from .deps import Ctx, Page, etag, idempotent, page_envelope, page_params, parse_if_match

router = APIRouter(tags=["cases"])

CaseName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
Markdown = Annotated[str, Field(min_length=1, max_length=262_144)]
TagName = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,59}$")]


class CaseCreate(BaseModel):
    model_config = {"extra": "forbid"}

    name: CaseName
    markdown: Markdown
    title: CaseName | None = None
    tags: list[TagName] = Field(default_factory=list)
    dsl_version: Annotated[str, StringConstraints(pattern=r"^\d+\.\d+$")] = "1.0"


class RevisionCreate(BaseModel):
    model_config = {"extra": "forbid"}

    markdown: Markdown
    title: CaseName | None = None
    dsl_version: Annotated[str, StringConstraints(pattern=r"^\d+\.\d+$")] = "1.0"


class CasePatch(BaseModel):
    model_config = {"extra": "forbid"}

    name: CaseName | None = None
    description: Annotated[str | None, Field(default=None, max_length=2000)] = None
    tags: list[TagName] | None = None
    archived: bool | None = None


class CompileRequest(BaseModel):
    model_config = {"extra": "forbid"}

    #: §6.2: the deterministic compiler is the default, and the AI path is an explicit opt-in.
    use_ai: bool = False
    force: bool = False


class ConfirmRequest(BaseModel):
    model_config = {"extra": "forbid"}

    ir_digest: Annotated[str, StringConstraints(min_length=10, max_length=80)]


@router.get("/projects/{project_id}/cases")
def list_cases(
    ctx: Ctx,
    project_id: str,
    page: Annotated[Page, Depends(page_params)],
    search: Annotated[str | None, Query(max_length=120)] = None,
    tag: Annotated[list[str] | None, Query(description="Repeatable tag name filter")] = None,
    include_archived: bool = False,
) -> dict[str, Any]:
    ctx.project(project_id, permission=Permission.CASE_READ)
    with ctx.session() as session:
        tags = TagRepository(session, ctx.tenant_id).for_project(project_id)
        wanted = {item.lower() for item in (tag or [])}
        tag_ids = [row.id for row in tags if not wanted or row.name.lower() in wanted]
        if wanted and len(tag_ids) != len(wanted):
            raise ApiError(ErrorCode.VALIDATION_ERROR, "One of the requested tags does not exist in this project")
        rows, total = CaseRepository(session, ctx.tenant_id).list(
            project_id=project_id,
            tag_ids=tag_ids,
            search=search,
            include_archived=include_archived,
            limit=page.limit,
            offset=page.offset,
        )
        items = [_case_summary(session, ctx.tenant_id, row) for row in rows]
    return page_envelope(items, page, total=total)


@router.post("/projects/{project_id}/cases", status_code=201)
def create_case(
    ctx: Ctx,
    project_id: str,
    body: CaseCreate,
    response: Response,
    idempotency_key: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    """Saving a case queues a compile; the request never waits for a model call (§6.1)."""
    ctx.project(project_id, permission=Permission.CASE_WRITE)
    payload = body.model_dump(mode="json")

    def action() -> tuple[str, dict[str, Any]]:
        result = CaseService(ctx.settings).create(
            tenant_id=ctx.tenant_id,
            project_id=project_id,
            name=body.name,
            markdown=body.markdown,
            dsl_version=body.dsl_version,
            title=body.title,
            created_by=ctx.actor_id,
            tags=body.tags,
        )
        with ctx.session() as session:
            ctx.audit(
                session,
                operation="case.create",
                resource_type="case",
                resource_id=str(result["case_id"]),
                project_id=project_id,
                detail={"revision_id": result["revision_id"], "revision_no": result["revision_no"]},
            )
        return str(result["case_id"]), result

    result = idempotent(
        ctx, route=f"POST /projects/{project_id}/cases", key=idempotency_key, payload=payload, action=action
    )
    response.headers["Location"] = f"/api/v1/cases/{result['case_id']}"
    return result


@router.post("/projects/{project_id}/cases/import", status_code=201)
async def import_case(
    ctx: Ctx,
    project_id: str,
    file: Annotated[UploadFile, File()],
    name: Annotated[str | None, Form()] = None,
    tags: Annotated[str | None, Form(description="Comma separated tag names")] = None,
) -> dict[str, Any]:
    """Upload a Markdown file: UTF-8 and size are checked before anything is stored (§13.2)."""
    ctx.project(project_id, permission=Permission.CASE_WRITE)
    data = await file.read()
    if len(data) > ctx.settings.max_case_bytes:
        raise ApiError(
            ErrorCode.CASE_TOO_LARGE,
            f"A case file is limited to {ctx.settings.max_case_bytes} bytes",
            details={"size": len(data)},
        )
    try:
        markdown = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ApiError(ErrorCode.VALIDATION_ERROR, "The uploaded case must be UTF-8 encoded text") from exc
    base = (name or (file.filename or "").rsplit(".", 1)[0] or "imported-case").strip()
    tag_list = [item.strip() for item in (tags or "").split(",") if item.strip()]
    result = CaseService(ctx.settings).create(
        tenant_id=ctx.tenant_id,
        project_id=project_id,
        name=base[:200],
        markdown=markdown,
        dsl_version="1.0",
        title=None,
        created_by=ctx.actor_id,
        tags=tag_list,
    )
    with ctx.session() as session:
        ctx.audit(
            session,
            operation="case.import",
            resource_type="case",
            resource_id=str(result["case_id"]),
            project_id=project_id,
            detail={"filename": (file.filename or "")[:120], "bytes": len(data)},
        )
    return result


@router.get("/cases/{case_id}")
def get_case(ctx: Ctx, case_id: str, response: Response) -> dict[str, Any]:
    with ctx.session() as session:
        cases = CaseRepository(session, ctx.tenant_id)
        case = cases.by_id(case_id)
        if case is None:
            raise ApiError(ErrorCode.NOT_FOUND, "Case not found in your tenant")
    ctx.project(case.project_id, permission=Permission.CASE_READ)
    with ctx.session() as session:
        cases = CaseRepository(session, ctx.tenant_id)
        revision = cases.revision(case.current_revision_id or "") if case.current_revision_id else None
        payload = {
            **_case_summary(session, ctx.tenant_id, case),
            "description": case.description,
            "markdown": revision.markdown if revision else None,
            "current_revision": _revision_payload(revision) if revision else None,
            "compile": CaseService(ctx.settings).compile_result(
                session, tenant_id=ctx.tenant_id, revision_id=revision.id
            )
            if revision
            else None,
            "archived": case.archived_at is not None,
            "row_version": int(case.row_version or 1),
            "updated_at": _iso(case.updated_at),
        }
    response.headers["ETag"] = etag("case", case.id, int(case.row_version))
    return payload


@router.patch("/cases/{case_id}")
def patch_case(
    ctx: Ctx,
    case_id: str,
    body: CasePatch,
    if_match: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    """Metadata only — the case text is immutable, so an edit is a new revision (§3.2)."""
    with ctx.session() as session:
        case = CaseRepository(session, ctx.tenant_id).by_id(case_id)
        if case is None:
            raise ApiError(ErrorCode.NOT_FOUND, "Case not found in your tenant")
    ctx.project(case.project_id, permission=Permission.CASE_WRITE)
    expected = parse_if_match(if_match)
    if expected is not None and expected != int(case.row_version or 1):
        raise ApiError(
            ErrorCode.VERSION_CONFLICT,
            "The case changed since you loaded it; reload before editing metadata",
            details={"current_row_version": int(case.row_version or 1)},
        )
    with ctx.session() as session:
        cases = CaseRepository(session, ctx.tenant_id)
        if body.name or body.description:
            cases.rename(case, name=body.name or case.name, description=body.description)
        if body.archived is True:
            cases.archive(case)
        elif body.archived is False:
            case.archived_at = None
            case.row_version = int(case.row_version or 1) + 1
        if body.tags is not None:
            TagRepository(session, ctx.tenant_id).set_case_tags(case, body.tags)
        ctx.audit(
            session,
            operation="case.update",
            resource_type="case",
            resource_id=case.id,
            project_id=case.project_id,
            detail={"fields": sorted(key for key, value in body.model_dump().items() if value is not None)},
        )
        session.flush()
        return _case_summary(session, ctx.tenant_id, case)


@router.get("/cases/{case_id}/revisions")
def list_revisions(ctx: Ctx, case_id: str, page: Annotated[Page, Depends(page_params)]) -> dict[str, Any]:
    with ctx.session() as session:
        case = CaseRepository(session, ctx.tenant_id).by_id(case_id)
        if case is None:
            raise ApiError(ErrorCode.NOT_FOUND, "Case not found in your tenant")
        revisions = CaseRepository(session, ctx.tenant_id).revisions(case_id)
    ctx.project(case.project_id, permission=Permission.CASE_READ)
    items = [_revision_payload(row) for row in revisions][page.offset : page.offset + page.limit]
    return page_envelope(items, page, total=len(revisions))


@router.post("/cases/{case_id}/revisions", status_code=201)
def add_revision(
    ctx: Ctx,
    case_id: str,
    body: RevisionCreate,
    if_match: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    with ctx.session() as session:
        case = CaseRepository(session, ctx.tenant_id).by_id(case_id)
        if case is None:
            raise ApiError(ErrorCode.NOT_FOUND, "Case not found in your tenant")
        if case.archived_at is not None:
            raise ApiError(ErrorCode.CASE_ARCHIVED, "An archived case cannot take a new revision")
    ctx.project(case.project_id, permission=Permission.CASE_WRITE)
    return CaseService(ctx.settings).add_revision(
        tenant_id=ctx.tenant_id,
        case_id=case_id,
        markdown=body.markdown,
        dsl_version=body.dsl_version,
        title=body.title,
        created_by=ctx.actor_id,
        expected_row_version=parse_if_match(if_match, required=True),
    )


@router.get("/case-revisions/{revision_id}")
def get_revision(ctx: Ctx, revision_id: str) -> dict[str, Any]:
    """The original text plus every compile attempt against it (§13.2)."""
    with ctx.session() as session:
        revision = CaseRepository(session, ctx.tenant_id).revision(revision_id)
        if revision is None:
            raise ApiError(ErrorCode.NOT_FOUND, "Case revision not found in your tenant")
        ctx.project(revision.project_id, permission=Permission.CASE_READ)
        compiles = CompileRepository(session, ctx.tenant_id)
        rows = compiles.base().where(CompileRepository.model.revision_id == revision_id)
        artifacts = [
            _compile_payload(row, include_ir=False)
            for row in session.scalars(rows.order_by(CompileRepository.model.created_at.desc())).all()
        ]
        return {**_revision_payload(revision), "markdown": revision.markdown, "compilations": artifacts}


@router.post("/case-revisions/{revision_id}/compile", status_code=202)
def compile_revision(
    ctx: Ctx,
    revision_id: str,
    body: CompileRequest,
    idempotency_key: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    """202 plus the artifact id: the client polls `GET /compilations/{id}` for the result (§13.1)."""
    with ctx.session() as session:
        revision = CaseRepository(session, ctx.tenant_id).require_revision(revision_id)
        ctx.project(revision.project_id, permission=Permission.CASE_COMPILE)
        payload = {
            "revision_id": revision_id,
            "use_ai": body.use_ai,
            "force": body.force,
            "digest": revision.source_digest,
        }

    def action() -> tuple[str, dict[str, Any]]:
        with ctx.session() as session:
            artifact_id = compile_now(
                session,
                ctx.tenant_id,
                project_id=revision.project_id,
                revision_id=revision.id,
                created_by=ctx.actor_id,
                use_ai=body.use_ai,
                force=body.force,
            )
            ctx.audit(
                session,
                operation="compile.request",
                resource_type="compile_artifact",
                resource_id=artifact_id,
                project_id=revision.project_id,
                detail={"revision_id": revision_id, "use_ai": body.use_ai, "force": body.force},
            )
            session.commit()
        return artifact_id, {
            "compile_artifact_id": artifact_id,
            "status": CompileStatus.PENDING.value,
            "revision_id": revision_id,
            "compiler_version": COMPILER_VERSION,
        }

    return idempotent(
        ctx, route=f"POST /case-revisions/{revision_id}/compile", key=idempotency_key, payload=payload, action=action
    )


@router.get("/compilations/{artifact_id}")
def get_compilation(ctx: Ctx, artifact_id: str) -> dict[str, Any]:
    with ctx.session() as session:
        artifact = CompileRepository(session, ctx.tenant_id).by_id(artifact_id)
        if artifact is None:
            raise ApiError(ErrorCode.NOT_FOUND, "Compilation not found in your tenant")
        ctx.project(artifact.project_id, permission=Permission.CASE_READ)
        return _compile_payload(artifact, include_ir=True)


@router.post("/compilations/{artifact_id}/confirm")
def confirm_compilation(
    ctx: Ctx,
    artifact_id: str,
    body: ConfirmRequest,
    idempotency_key: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    """A NEEDS_REVIEW product runs only after a human confirmed this exact IR digest (§3.2)."""
    with ctx.session() as session:
        artifact = CompileRepository(session, ctx.tenant_id).require(artifact_id)
        ctx.project(artifact.project_id, permission=Permission.CASE_COMPILE)
        payload = {"artifact_id": artifact_id, "ir_digest": body.ir_digest}

    def action() -> tuple[str, dict[str, Any]]:
        with ctx.session() as session:
            compiles = CompileRepository(session, ctx.tenant_id)
            current = compiles.require(artifact_id)
            if (current.ir_digest or "") != body.ir_digest:
                # Confirming means "this exact IR", so a recompile between reading and clicking voids it.
                raise ApiError(
                    ErrorCode.COMPILE_STALE_DIGEST,
                    "The compiled IR no longer matches the digest you confirmed; review it again",
                    details={"current_ir_digest": current.ir_digest},
                )
            confirmed = compiles.confirm(current, actor_id=ctx.actor_id, expected_source_digest=current.source_digest)
            ctx.audit(
                session,
                operation="compile.confirm",
                resource_type="compile_artifact",
                resource_id=artifact_id,
                project_id=confirmed.project_id,
                detail={"ir_digest": confirmed.ir_digest, "review_items": len(confirmed.review_items or [])},
            )
            session.commit()
            return artifact_id, {
                "compile_artifact_id": artifact_id,
                "status": confirmed.status,
                "confirmed_by": confirmed.confirmed_by,
                "confirmed_at": _iso(confirmed.confirmed_at),
                "executable": True,
            }

    return idempotent(
        ctx, route=f"POST /compilations/{artifact_id}/confirm", key=idempotency_key, payload=payload, action=action
    )


# ---------------------------------------------------------------------- attachments


@router.post("/projects/{project_id}/attachments", status_code=201)
async def upload_attachment(ctx: Ctx, project_id: str, file: Annotated[UploadFile, File()]) -> dict[str, Any]:
    """Store-and-scan in one call; only a CLEAN attachment can be referenced by an `upload` step."""
    ctx.project(project_id, permission=Permission.CASE_WRITE)
    data = await file.read()
    result = AttachmentService(ctx.settings).store(
        tenant_id=ctx.tenant_id,
        project_id=project_id,
        filename=file.filename or "attachment",
        media_type=file.content_type or "application/octet-stream",
        data=data,
        created_by=ctx.actor_id,
    )
    with ctx.session() as session:
        ctx.audit(
            session,
            operation="attachment.store",
            resource_type="attachment",
            resource_id=result["attachment_id"],
            project_id=project_id,
            detail={"filename": result["filename"], "size": result["size"], "scan_status": result["scan_status"]},
        )
    return result


@router.get("/attachments/{attachment_id}")
def get_attachment(ctx: Ctx, attachment_id: str) -> dict[str, Any]:
    with ctx.session() as session:
        row = AttachmentRepository(session, ctx.tenant_id).by_id(attachment_id)
        if row is None:
            raise ApiError(ErrorCode.NOT_FOUND, "Attachment not found in your tenant")
        ctx.project(row.project_id, permission=Permission.CASE_READ)
        return attachment_payload(row)


# --------------------------------------------------------------------------- helpers


def _case_summary(session, tenant_id: str, case: Any) -> dict[str, Any]:
    tags = session.scalars(
        select(Tag.name)
        .join(CaseTag, (CaseTag.tag_id == Tag.id) & (CaseTag.tenant_id == Tag.tenant_id))
        .where(CaseTag.case_id == case.id, CaseTag.tenant_id == tenant_id)
    ).all()
    revision = None
    if case.current_revision_id:
        revision = CaseRepository(session, tenant_id).revision(case.current_revision_id)
    compile_status = None
    if revision is not None:
        artifact = CompileRepository(session, tenant_id).latest(revision.id)
        compile_status = artifact.status if artifact else None
    return {
        "case_id": case.id,
        "project_id": case.project_id,
        "name": case.name,
        "title": revision.title if revision else None,
        "tags": sorted(tags),
        "current_revision_id": case.current_revision_id,
        "revision_no": revision.version if revision else None,
        "source_digest": revision.source_digest if revision else None,
        "compile_status": compile_status,
        "archived": case.archived_at is not None,
        "row_version": int(case.row_version or 1),
        "created_at": _iso(case.created_at),
        "updated_at": _iso(case.updated_at),
        "etag": etag("case", case.id, int(case.row_version or 1)),
    }


def _revision_payload(row: Any) -> dict[str, Any]:
    if row is None:
        return {}
    return {
        "revision_id": row.id,
        "case_id": row.case_id,
        "version": int(row.version),
        "title": row.title,
        "dsl_version": row.dsl_version,
        "source_digest": row.source_digest,
        "bytes": len(row.markdown.encode("utf-8")),
        "created_by": row.created_by,
        "created_at": _iso(row.created_at),
    }


def _compile_payload(row: Any, *, include_ir: bool) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "compile_artifact_id": row.id,
        "revision_id": row.revision_id,
        "project_id": row.project_id,
        "status": row.status,
        "compiler_mode": row.compiler_mode,
        "compiler_version": row.compiler_version,
        "model": row.model,
        "prompt_version": row.prompt_version,
        "source_digest": row.source_digest,
        "ir_digest": row.ir_digest,
        "error_code": row.error_code,
        "diagnostics": list(row.diagnostics or []),
        "review_items": list(row.review_items or []),
        "usage": dict(row.usage or {}),
        "confirmed_by": row.confirmed_by,
        "confirmed_at": _iso(row.confirmed_at),
        "created_at": _iso(row.created_at),
        "executable": row.status == CompileStatus.SUCCEEDED.value
        or (row.status == CompileStatus.NEEDS_REVIEW.value and row.confirmed_at is not None),
    }
    if include_ir:
        payload["ir"] = row.ir
    return payload


def _iso(value: Any) -> str | None:
    return value.isoformat() if hasattr(value, "isoformat") else None
