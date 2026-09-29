"""Environments, environment revisions and attachments (§11.2, §14.3)."""

from __future__ import annotations

import hashlib
from typing import Any

from sqlalchemy import func, select

from ..db.base import new_id, utcnow
from ..db.models import Attachment, Environment, EnvironmentRevision
from ..domain.enums import ScanStatus
from ..domain.errors import ApiError, ErrorCode
from .base import Scoped


def revision_digest(config: dict[str, Any], secret_bindings: dict[str, Any]) -> str:
    canonical = sorted((("config", k, _scalar(v)) for k, v in config.items())) + sorted(
        (("binding", k, _scalar(v)) for k, v in secret_bindings.items())
    )
    return "sha256:" + hashlib.sha256(repr(canonical).encode("utf-8")).hexdigest()


def _scalar(value: Any) -> str:
    return json_dumps(value)


def json_dumps(value: Any) -> str:
    import json

    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)


class EnvironmentRepository(Scoped[Environment]):
    model = Environment

    def by_name(self, project_id: str, name: str) -> Environment | None:
        return self.session.scalar(
            select(Environment).where(
                Environment.tenant_id == self.tenant_id,
                Environment.project_id == project_id,
                Environment.environment_name == name,
                Environment.archived_at.is_(None),
            )
        )

    def list(self, project_id: str) -> list[Environment]:
        return list(
            self.session.scalars(
                select(Environment)
                .where(Environment.tenant_id == self.tenant_id, Environment.project_id == project_id)
                .order_by(Environment.environment_name)
            ).all()
        )

    def create(self, *, project_id: str, name: str, created_by: str | None) -> Environment:
        environment = Environment(
            id=new_id(),
            tenant_id=self.tenant_id,
            project_id=project_id,
            environment_name=name,
            row_version=1,
        )
        self.session.add(environment)
        self.session.flush()
        return environment

    def publish_revision(
        self,
        environment: Environment,
        *,
        config: dict[str, Any],
        secret_bindings: dict[str, Any],
        created_by: str | None,
        expected_row_version: int | None = None,
    ) -> EnvironmentRevision:
        if expected_row_version is not None and environment.row_version != expected_row_version:
            raise ApiError(
                ErrorCode.VERSION_CONFLICT,
                "The environment changed since you loaded it; reload before saving.",
                details={"current_row_version": environment.row_version},
            )
        version = (
            int(
                self.session.scalar(
                    select(func.max(EnvironmentRevision.version)).where(
                        EnvironmentRevision.tenant_id == self.tenant_id,
                        EnvironmentRevision.environment_id == environment.id,
                    )
                )
                or 0
            )
            + 1
        )
        revision = EnvironmentRevision(
            id=new_id(),
            tenant_id=self.tenant_id,
            project_id=environment.project_id,
            environment_id=environment.id,
            version=version,
            config=config,
            secret_bindings=secret_bindings,
            digest=revision_digest(config, secret_bindings),
            created_by=created_by,
        )
        self.session.add(revision)
        environment.current_revision_id = revision.id
        environment.row_version = int(environment.row_version or 1) + 1
        self.session.flush()
        return revision

    def revision(self, revision_id: str) -> EnvironmentRevision | None:
        return self.session.scalar(
            select(EnvironmentRevision).where(
                EnvironmentRevision.tenant_id == self.tenant_id, EnvironmentRevision.id == revision_id
            )
        )

    def require_revision(self, revision_id: str) -> EnvironmentRevision:
        row = self.revision(revision_id)
        if row is None:
            raise ApiError(ErrorCode.NOT_FOUND, "Environment revision not found in this tenant")
        return row

    def revisions(self, environment_id: str) -> list[EnvironmentRevision]:
        return list(
            self.session.scalars(
                select(EnvironmentRevision)
                .where(
                    EnvironmentRevision.tenant_id == self.tenant_id,
                    EnvironmentRevision.environment_id == environment_id,
                )
                .order_by(EnvironmentRevision.version.desc())
            ).all()
        )

    def current_revision(self, environment: Environment) -> EnvironmentRevision | None:
        if not environment.current_revision_id:
            return None
        return self.revision(environment.current_revision_id)

    def archive(self, environment: Environment) -> Environment:
        environment.archived_at = utcnow()
        environment.row_version = int(environment.row_version or 1) + 1
        self.session.flush()
        return environment


class AttachmentRepository(Scoped[Attachment]):
    model = Attachment

    def record(
        self,
        *,
        project_id: str,
        filename: str,
        media_type: str,
        size: int,
        digest: str,
        object_key: str,
        created_by: str | None,
    ) -> Attachment:
        attachment = Attachment(
            id=new_id(),
            tenant_id=self.tenant_id,
            project_id=project_id,
            filename=filename[:300],
            media_type=media_type[:120],
            size=size,
            digest=digest,
            object_key=object_key,
            scan_status=ScanStatus.PENDING.value,
            created_by=created_by,
        )
        self.session.add(attachment)
        self.session.flush()
        return attachment

    def set_scan(self, attachment: Attachment, *, status: ScanStatus, detail: str | None = None) -> Attachment:
        attachment.scan_status = status.value
        attachment.scan_detail = (detail or None) and detail[:300]
        self.session.flush()
        return attachment

    def by_ids(self, attachment_ids: list[str]) -> dict[str, Attachment]:
        if not attachment_ids:
            return {}
        rows = self.session.scalars(
            select(Attachment).where(Attachment.tenant_id == self.tenant_id, Attachment.id.in_(list(attachment_ids)))
        ).all()
        return {row.id: row for row in rows}

    def ready_ids(self, *, project_id: str) -> set[str]:
        rows = self.session.scalars(
            select(Attachment.id).where(
                Attachment.tenant_id == self.tenant_id,
                Attachment.project_id == project_id,
                Attachment.scan_status == ScanStatus.CLEAN.value,
            )
        ).all()
        return set(rows)

    def list(self, project_id: str) -> list[Attachment]:
        return list(
            self.session.scalars(
                select(Attachment)
                .where(Attachment.tenant_id == self.tenant_id, Attachment.project_id == project_id)
                .order_by(Attachment.created_at.desc())
            ).all()
        )
