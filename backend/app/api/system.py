"""Discovery routes: health, capabilities and the caller's own authority (§13.2, §15.4)."""

from __future__ import annotations

from fastapi import APIRouter
from sqlalchemy import select

from ..db.base import get_database
from ..domain.enums import Permission
from ..executors.playwright.adapter import PlaywrightExecutor
from ..ir.models import COMPILER_VERSION
from ..observability import get_logger
from .deps import Ctx

log = get_logger(__name__)

router = APIRouter(tags=["system"])

IR_VERSION = "1.0"

#: What the console needs to know before it offers a feature, so the UI never promises an action
#: the deployment has not configured.
FEATURE_FLAGS = {
    "markdown_dsl": "available",
    "test_ir": IR_VERSION,
    "browser_execution": "available",
    "ai_compilation": "configured",
    "human_handoff": "available",
    "evidence_capture": "available",
    "failure_analysis": "configured",
}


@router.get("/health")
def health() -> dict[str, object]:
    """Liveness plus a database round-trip; a deployment behind a load balancer reads this (§15.4)."""
    try:
        with get_database().session() as session:
            session.execute(select(1))
        database = "ok"
    except Exception as exc:
        log.exception("health check failed", extra={"context": {"error": type(exc).__name__}})
        database = "unavailable"
    return {"status": "ok" if database == "ok" else "degraded", "service": "ai-test-agent", "database": database}


@router.get("/capabilities")
def capabilities(ctx: Ctx) -> dict[str, object]:
    settings = ctx.settings
    executor = PlaywrightExecutor(settings)
    return {
        "api_version": "v1",
        "ir_version": IR_VERSION,
        "compiler_version": COMPILER_VERSION,
        "actions": sorted(executor.capabilities().actions),
        "browsers": settings.browser_channel_list,
        "conditions": sorted(executor.capabilities().conditions),
        "features": {
            **FEATURE_FLAGS,
            "ai_compilation": "available" if settings.ai_enabled else "disabled",
            "ai_vision": "available" if settings.ai_vision_enabled else "disabled",
            "failure_analysis": "available" if settings.ai_enabled else "rules_only",
        },
        "limits": {
            "max_case_bytes": settings.max_case_bytes,
            "max_case_steps": settings.max_case_steps,
            "max_attachment_bytes": settings.max_attachment_bytes,
            "step_timeout_max_ms": settings.step_timeout_max_ms,
            "artifact_max_bytes": settings.artifact_max_bytes,
        },
        "auth_mode": settings.auth_mode,
    }


@router.get("/whoami")
def whoami(ctx: Ctx) -> dict[str, object]:
    """The caller's own resolved permissions: the console renders from this, never from a guess (§13.5)."""
    identity = ctx.identity
    return {
        "user_id": identity.user_id,
        "tenant_id": identity.tenant_id,
        "display_name": identity.display_name,
        "issuer": identity.issuer,
        "roles": {key: value.value for key, value in identity.roles.items()},
        "grants_by_project": identity.granted_projects,
        "permissions_by_project": {
            project_id: sorted(item.value for item in identity.permissions_in(project_id))
            for project_id in sorted(set(identity.roles) - {"*"} | set(identity.project_grants))
        },
        "is_admin": identity.is_admin,
        "can_manage_users": identity.can(Permission.ADMIN_USERS),
        "request_id": ctx.request_id,
    }
