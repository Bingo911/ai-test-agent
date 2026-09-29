"""Development workspace seeding. Deterministic ids keep it idempotent (§14.1 local dev identity)."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select

from ..config import Settings, get_settings
from ..domain.enums import Permission, Role
from ..observability import get_logger
from .base import new_id, utcnow
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

log = get_logger(__name__)

NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "https://ai-test-agent.local/dev")


def bootstrap_runtime(settings: Settings | None = None, *, seed: bool = True) -> dict[str, str]:
    """Create the schema and, for a development database, the demo workspace. Idempotent.

    Called by the API lifespan, the single-process supervisor and a Celery worker process, so any of
    them can be the first thing that runs against a fresh database.
    """
    from .base import get_database

    settings = settings or get_settings()
    settings.ensure_dirs()
    database = get_database()
    database.create_schema()
    if not (seed and settings.is_development):
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
                settings={"allow_vision": settings.ai_vision_enabled, "human_slot_ratio": settings.human_slot_ratio},
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
