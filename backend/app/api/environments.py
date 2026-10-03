"""Environments, their immutable revisions and the write-only secret API (§13.2, §14.3)."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Response
from pydantic import BaseModel, Field, StringConstraints

from ..domain.enums import Permission
from ..domain.errors import ApiError, ErrorCode
from ..repositories.resources import EnvironmentRepository
from ..services.environments import EnvironmentService
from .deps import Ctx, Page, etag, page_envelope, page_params, parse_if_match

router = APIRouter(tags=["environments"])

EnvironmentName = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,119}$")]
SecretName = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,119}$")]


class EnvironmentCreate(BaseModel):
    model_config = {"extra": "forbid"}

    name: EnvironmentName


class RevisionPublish(BaseModel):
    model_config = {"extra": "forbid"}

    #: Validated key by key at publish time: a typo must not become an unapproved navigation target (§14.2).
    config: dict[str, Any]
    secret_bindings: dict[str, Any] | None = None


class SecretWrite(BaseModel):
    model_config = {"extra": "forbid"}

    logical_name: SecretName
    value: Annotated[str, Field(min_length=1, max_length=8192)]


@router.get("/projects/{project_id}/environments")
def list_environments(ctx: Ctx, project_id: str) -> dict[str, Any]:
    """Reading is open to project members: a run has to show which network it will use (§13.5)."""
    ctx.project(project_id)
    service = EnvironmentService(ctx.settings, database=ctx.database)
    return {
        "items": service.list(tenant_id=ctx.tenant_id, project_id=project_id),
        "next_cursor": None,
    }


@router.post("/projects/{project_id}/environments", status_code=201)
def create_environment(ctx: Ctx, project_id: str, body: EnvironmentCreate) -> dict[str, Any]:
    ctx.project(project_id, permission=Permission.ENV_MANAGE)
    result = EnvironmentService(ctx.settings, database=ctx.database).create(
        tenant_id=ctx.tenant_id, project_id=project_id, name=body.name, created_by=ctx.actor_id
    )
    with ctx.session() as session:
        ctx.audit(
            session,
            operation="environment.create",
            resource_type="environment",
            resource_id=result["environment_id"],
            project_id=project_id,
            detail={"name": body.name},
        )
    return result


@router.get("/projects/{project_id}/environments/{name}")
def get_environment(ctx: Ctx, project_id: str, name: str, response: Response) -> dict[str, Any]:
    ctx.project(project_id)
    service = EnvironmentService(ctx.settings, database=ctx.database)
    payload = service.by_name(tenant_id=ctx.tenant_id, project_id=project_id, name=name)
    response.headers["ETag"] = etag("environment", payload["environment_id"], int(payload["row_version"]))
    return payload


@router.post("/environments/{environment_id}/revisions")
def publish_revision(
    ctx: Ctx,
    environment_id: str,
    body: RevisionPublish,
    response: Response,
    if_match: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    """A new revision never overwrites history, so a finished run stays reproducible (§3.2)."""
    with ctx.session() as session:
        environment = EnvironmentRepository(session, ctx.tenant_id).by_id(environment_id)
        if environment is None:
            raise ApiError(ErrorCode.NOT_FOUND, "Environment not found in this tenant")
    ctx.project(environment.project_id, permission=Permission.ENV_MANAGE)
    expected = parse_if_match(if_match, required=True)
    result = EnvironmentService(ctx.settings, database=ctx.database).publish(
        tenant_id=ctx.tenant_id,
        environment_id=environment_id,
        config=body.config,
        secret_bindings=body.secret_bindings,
        created_by=ctx.actor_id,
        expected_row_version=expected,
    )
    with ctx.session() as session:
        revision = result.get("current_revision") or {}
        config = revision.get("config") or {}
        ctx.audit(
            session,
            operation="environment.publish",
            resource_type="environment_revision",
            resource_id=revision.get("environment_revision_id"),
            project_id=environment.project_id,
            detail={
                "version": revision.get("version"),
                "digest": revision.get("digest"),
                "allowed_domains": config.get("allowed_domains"),
                "evidence": config.get("evidence"),
            },
        )
    response.headers["ETag"] = etag("environment", environment_id, int(result["row_version"]))
    return result


@router.get("/environments/{environment_id}/revisions")
def list_revisions(ctx: Ctx, environment_id: str, page: Annotated[Page, Depends(page_params)]) -> dict[str, Any]:
    with ctx.session() as session:
        environment = EnvironmentRepository(session, ctx.tenant_id).by_id(environment_id)
        if environment is None:
            raise ApiError(ErrorCode.NOT_FOUND, "Environment not found in this tenant")
    ctx.project(environment.project_id)
    service = EnvironmentService(ctx.settings, database=ctx.database)
    items = service.revisions(tenant_id=ctx.tenant_id, environment_id=environment_id)
    return page_envelope(items, page)


@router.delete("/environments/{environment_id}", status_code=200)
def archive_environment(ctx: Ctx, environment_id: str) -> dict[str, Any]:
    with ctx.session() as session:
        environment = EnvironmentRepository(session, ctx.tenant_id).by_id(environment_id)
        if environment is None:
            raise ApiError(ErrorCode.NOT_FOUND, "Environment not found in this tenant")
    ctx.project(environment.project_id, permission=Permission.ENV_MANAGE)
    service = EnvironmentService(ctx.settings, database=ctx.database)
    result = service.archive(tenant_id=ctx.tenant_id, environment_id=environment_id)
    with ctx.session() as session:
        ctx.audit(
            session,
            operation="environment.archive",
            resource_type="environment",
            resource_id=environment_id,
            project_id=environment.project_id,
        )
    return result


# -------------------------------------------------------------------------- secrets


@router.get("/projects/{project_id}/secrets")
def list_secrets(ctx: Ctx, project_id: str) -> dict[str, Any]:
    """Metadata only — names, versions, providers. A stored value is never readable back (§14.3)."""
    ctx.project(project_id, permission=Permission.SECRET_MANAGE)
    service = EnvironmentService(ctx.settings, database=ctx.database)
    items = service.secret_versions(tenant_id=ctx.tenant_id, project_id=project_id)
    return {"items": items, "next_cursor": None}


@router.post("/projects/{project_id}/secrets", status_code=201)
def put_secret(ctx: Ctx, project_id: str, body: SecretWrite) -> dict[str, Any]:
    ctx.project(project_id, permission=Permission.SECRET_MANAGE)
    result = EnvironmentService(ctx.settings, database=ctx.database).put_secret(
        tenant_id=ctx.tenant_id,
        project_id=project_id,
        logical_name=body.logical_name,
        value=body.value,
        created_by=ctx.actor_id,
    )
    with ctx.session() as session:
        # The audit row records the name and version only; a secret value must never reach a log (§14.3).
        ctx.audit(
            session,
            operation="secret.put",
            resource_type="secret_version",
            resource_id=result["secret_version_id"],
            project_id=project_id,
            detail={"logical_name": result["logical_name"], "version": result["version"]},
        )
    return result


@router.delete("/projects/{project_id}/secrets/{logical_name}/{version}", status_code=200)
def revoke_secret(ctx: Ctx, project_id: str, logical_name: str, version: int) -> dict[str, Any]:
    """Revocation is explicit: an execution bound to it then fails loudly instead of silently rotating."""
    ctx.project(project_id, permission=Permission.SECRET_MANAGE)
    result = EnvironmentService(ctx.settings, database=ctx.database).revoke_secret(
        tenant_id=ctx.tenant_id, project_id=project_id, logical_name=logical_name, version=version
    )
    with ctx.session() as session:
        ctx.audit(
            session,
            operation="secret.revoke",
            resource_type="secret_version",
            resource_id=None,
            project_id=project_id,
            detail={"logical_name": logical_name, "version": version},
        )
    return result
