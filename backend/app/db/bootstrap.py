"""Development workspace seeding. Deterministic ids keep it idempotent (§14.1 local dev identity)."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select

from ..config import Settings, get_settings
from ..domain.enums import Permission, Role
from ..domain.mcp_policy import POLICY_KEY, McpPolicy
from ..observability import get_logger
from .base import Database, get_database, new_id, utcnow
from .models import (
    AppUser,
    Environment,
    EnvironmentRevision,
    PermissionGrant,
    Project,
    ProjectMembership,
    Tag,
    Tenant,
    TenantMembership,
    WorkerPool,
)
from .schema import SchemaNotReady, apply_schema, verify_schema

log = get_logger(__name__)

NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "https://ai-test-agent.local/dev")


def bootstrap_runtime(
    settings: Settings | None = None,
    *,
    database: Database | None = None,
    seed: bool = True,
) -> dict[str, str]:
    """Bring a development database up to shape, or confirm a production one already is. Idempotent.

    Called by the API lifespan, the single-process supervisor and a Worker process. The two paths are not
    the same thing (§13.6, AC-25): a development database is created and seeded here, because the person
    running it has no other way to start, while a production process is not allowed to touch the structure
    at all - the deploy job did that, and a process that cannot confirm the version it left behind refuses
    to serve rather than starting a request path that fails on its first write.
    """
    settings = settings or get_settings()
    settings.ensure_dirs()
    database = database if database is not None else get_database()
    if not settings.is_development:
        verify_schema(database)
        return {}
    drift = apply_schema(database, applied_by="bootstrap")
    # A development database left short is the same danger as a production one: the process would go on to
    # serve a request path that fails on the first write to the table that never got its column.
    if not drift.ok:
        raise SchemaNotReady(drift)
    if not seed:
        return {}
    with database.session() as session:
        workspace = ensure_development_workspace(session, settings)
        session.commit()
    return workspace


def dev_id(name: str) -> str:
    return str(uuid.uuid5(NAMESPACE, name))


DEVELOPMENT_WORKSPACE: dict[str, Any] = {
    "tenant": {"name": "default", "display_name": "Local development tenant"},
    "users": [
        {"key": "admin", "subject": "dev-admin", "display_name": "Dev Admin", "role": Role.ADMIN.value},
        {"key": "engineer", "subject": "dev-engineer", "display_name": "Dev Engineer", "role": Role.ENGINEER.value},
    ],
    "project": {"name": "demo", "display_name": "Demo project"},
    "environment": {
        "name": "local",
        "config": {
            "base_url": "https://www.baidu.com",
            "allowed_domains": ["www.baidu.com", "baidu.com", "localhost", "127.0.0.1", "example.com"],
            "allowed_protocols": ["https", "http"],
            "browsers": ["chromium", "chrome"],
            "viewport": {"width": 1280, "height": 720},
            "evidence": {"mode": "NORMAL", "trace": "off", "video": "off"},
            "variables": {},
        },
    },
    "pools": [{"name": "default", "capacity": 4}],
}


#: What a development workspace seeds when MCP is on. `allow_server_ai` stays off: content leaving
#: toward a client's model and the platform calling a model itself are two separate decisions (§5.5).
DEVELOPMENT_MCP_POLICY = McpPolicy(
    enabled=True,
    allow_case_content=True,
    allow_report_details=True,
    allow_server_ai=False,
).model_dump()


def _seed_project_settings(settings: Settings) -> dict[str, Any]:
    """The demo project's settings, and its MCP policy only when this build actually serves MCP.

    Written once, for a project being created: an administrator who switched MCP off in a development
    database must not find it back on after a restart, and a repeated seed must not touch versions or
    audit (§5.5, AC-35). With MCP disabled the original seed default is preserved.
    """
    seeded: dict[str, Any] = {
        "allow_vision": settings.ai_vision_enabled,
        "human_slot_ratio": settings.human_slot_ratio,
    }
    if settings.mcp_enabled:
        seeded[POLICY_KEY] = dict(DEVELOPMENT_MCP_POLICY)
    return seeded


def ensure_development_workspace(session, settings: Settings) -> dict[str, str]:
    tenant_id = dev_id(f"tenant:{DEVELOPMENT_WORKSPACE['tenant']['name']}")
    if session.get(Tenant, tenant_id) is None:
        session.add(
            Tenant(
                id=tenant_id,
                name=DEVELOPMENT_WORKSPACE["tenant"]["name"],
                display_name=DEVELOPMENT_WORKSPACE["tenant"]["display_name"],
                quota={"max_concurrent_executions": settings.worker_slots, "ai_enabled": settings.ai_enabled},
            )
        )
    user_ids: dict[str, str] = {}
    for spec in DEVELOPMENT_WORKSPACE["users"]:
        user_id = dev_id(f"user:{spec['subject']}")
        user_ids[spec["key"]] = user_id
        user = session.get(AppUser, user_id)
        if user is None:
            session.add(
                AppUser(
                    id=user_id,
                    issuer="local-dev",
                    subject=spec["subject"],
                    display_name=spec["display_name"],
                    status="ACTIVE",
                )
            )
        if (
            session.scalar(
                select(TenantMembership).where(
                    TenantMembership.tenant_id == tenant_id, TenantMembership.user_id == user_id
                )
            )
            is None
        ):
            session.add(TenantMembership(id=new_id(), tenant_id=tenant_id, user_id=user_id, role=spec["role"]))

    project_id = dev_id(f"project:{DEVELOPMENT_WORKSPACE['project']['name']}")
    project = session.get(Project, project_id)
    if project is None:
        session.add(
            Project(
                id=project_id,
                tenant_id=tenant_id,
                name=DEVELOPMENT_WORKSPACE["project"]["name"],
                display_name=DEVELOPMENT_WORKSPACE["project"]["display_name"],
                quota={"max_concurrent_executions": max(1, settings.worker_slots // 2)},
                settings=_seed_project_settings(settings),
            )
        )
        session.flush()
    for key, user_id in user_ids.items():
        role = next(spec["role"] for spec in DEVELOPMENT_WORKSPACE["users"] if spec["key"] == key)
        existing = session.scalar(
            select(ProjectMembership).where(
                ProjectMembership.tenant_id == tenant_id,
                ProjectMembership.project_id == project_id,
                ProjectMembership.user_id == user_id,
            )
        )
        if existing is None:
            session.add(
                ProjectMembership(id=new_id(), tenant_id=tenant_id, project_id=project_id, user_id=user_id, role=role)
            )
        else:
            existing.role = role
    engineer_id = user_ids["engineer"]
    for permission in (Permission.HUMAN_CONTROL, Permission.SENSITIVE_ARTIFACT_READ):
        if (
            session.scalar(
                select(PermissionGrant).where(
                    PermissionGrant.tenant_id == tenant_id,
                    PermissionGrant.project_id == project_id,
                    PermissionGrant.user_id == engineer_id,
                    PermissionGrant.permission == permission.value,
                )
            )
            is None
        ):
            session.add(
                PermissionGrant(
                    id=new_id(),
                    tenant_id=tenant_id,
                    project_id=project_id,
                    user_id=engineer_id,
                    permission=permission.value,
                    reason="development workspace convenience",
                )
            )

    for pool_spec in DEVELOPMENT_WORKSPACE["pools"]:
        if session.scalar(select(WorkerPool).where(WorkerPool.name == pool_spec["name"])) is None:
            session.add(
                WorkerPool(
                    id=dev_id(f"pool:{pool_spec['name']}"),
                    name=pool_spec["name"],
                    capacity=pool_spec["capacity"],
                    capabilities={"browsers": settings.browser_channel_list, "actions": "ir-1.0"},
                )
            )

    environment = session.scalar(
        select(Environment).where(Environment.tenant_id == tenant_id, Environment.project_id == project_id)
    )
    if environment is None:
        environment = Environment(
            id=dev_id(f"environment:{project_id}:local"),
            tenant_id=tenant_id,
            project_id=project_id,
            environment_name=DEVELOPMENT_WORKSPACE["environment"]["name"],
            row_version=1,
        )
        session.add(environment)
        session.flush()
        config = dict(DEVELOPMENT_WORKSPACE["environment"]["config"])
        revision = EnvironmentRevision(
            id=dev_id(f"environment_revision:{environment.id}:1"),
            tenant_id=tenant_id,
            project_id=project_id,
            environment_id=environment.id,
            version=1,
            config=config,
            secret_bindings={},
            digest="sha256:" + uuid.uuid5(NAMESPACE, str(sorted(config.items()))).hex,
            created_by=user_ids["admin"],
        )
        session.add(revision)
        environment.current_revision_id = revision.id
    for tag_name in ("smoke", "login", "e2e"):
        if (
            session.scalar(
                select(Tag).where(Tag.tenant_id == tenant_id, Tag.project_id == project_id, Tag.name == tag_name)
            )
            is None
        ):
            session.add(Tag(id=new_id(), tenant_id=tenant_id, project_id=project_id, name=tag_name))
    session.flush()
    log.info(
        "development workspace ready",
        extra={"fields": {"tenant_id": tenant_id, "project_id": project_id, "at": utcnow().isoformat()}},
    )
    return {
        "tenant_id": tenant_id,
        "project_id": project_id,
        "environment_id": environment.id,
        "environment_revision_id": environment.current_revision_id or "",
        "admin_user_id": user_ids["admin"],
        "engineer_user_id": user_ids["engineer"],
        "pool_id": dev_id("pool:default"),
    }
