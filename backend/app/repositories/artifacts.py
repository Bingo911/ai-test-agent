"""Evidence index, failure analysis rows and element memory (§11.2, §12)."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import func, select, update

from ..db.base import new_id, utcnow
from ..db.models import Artifact, ElementMemory, FailureAnalysis
from ..domain.enums import AnalysisStatus, ArtifactKind, UploadStatus
from ..domain.errors import ApiError, ErrorCode
from .base import Scoped


class ArtifactRepository(Scoped[Artifact]):
    model = Artifact

    def record(
        self,
        *,
        project_id: str,
        execution_id: str,
        step_id: str | None,
        kind: ArtifactKind | str,
        name: str | None,
        object_key: str,
        media_type: str,
        sensitivity: str,
        size: int = 0,
        sha256: str | None = None,
        metadata: dict[str, Any] | None = None,
        publish_allowed: bool = True,
        upload_status: UploadStatus = UploadStatus.READY,
        retention_days: int = 30,
    ) -> Artifact:
        artifact = Artifact(
            id=new_id(),
            tenant_id=self.tenant_id,
            project_id=project_id,
            execution_id=execution_id,
            step_id=step_id,
            kind=kind.value if isinstance(kind, ArtifactKind) else str(kind),
            name=name,
            object_key=object_key,
            sha256=sha256,
            size=size,
            media_type=media_type,
            sensitivity=sensitivity,
            upload_status=upload_status.value,
            publish_allowed=publish_allowed,
            artifact_metadata=metadata or {},
            retention_until=utcnow() + timedelta(days=retention_days),
        )
        self.session.add(artifact)
        self.session.flush()
        return artifact

    def mark_uploaded(
        self, artifact_id: str, *, sha256: str, size: int, status: UploadStatus = UploadStatus.READY
    ) -> None:
        self.session.execute(
            update(Artifact)
            .where(Artifact.tenant_id == self.tenant_id, Artifact.id == artifact_id)
            .values(sha256=sha256, size=size, upload_status=status.value)
        )
        self.session.commit()

    def mark_failed(self, artifact_id: str, *, detail: str) -> None:
        locked = self.by_id(artifact_id, for_update=True)
        if locked is None:
            return
        locked.upload_status = UploadStatus.FAILED.value
        merged = dict(locked.artifact_metadata or {})
        merged["upload_error"] = detail[:300]
        locked.artifact_metadata = merged
        self.session.flush()

    def for_execution(self, execution_id: str, *, kinds: tuple[str, ...] = ()) -> list[Artifact]:
        conditions = [Artifact.tenant_id == self.tenant_id, Artifact.execution_id == execution_id]
        if kinds:
            conditions.append(Artifact.kind.in_(list(kinds)))
        return list(self.session.scalars(select(Artifact).where(*conditions).order_by(Artifact.created_at.asc())).all())

    def forbid_publish_for(self, *, execution_id: str, kinds: tuple[str, ...], reason: str) -> int:
        """Sensitive-mode downgrade: never publish a trace or video captured before the switch (§10.4)."""
        result = self.session.execute(
            update(Artifact)
            .where(
                Artifact.tenant_id == self.tenant_id,
                Artifact.execution_id == execution_id,
                Artifact.kind.in_(list(kinds)),
                Artifact.publish_allowed.is_(True),
            )
            .values(publish_allowed=False, artifact_metadata={"publish_revoked_reason": reason[:300]})
        )
        self.session.commit()
        return int(result.rowcount or 0)

    def summary(self, execution_id: str) -> dict[str, Any]:
        rows = self.for_execution(execution_id)
        counts: dict[str, int] = {}
        for row in rows:
            counts[row.kind] = counts.get(row.kind, 0) + 1
        return {
            "total": len(rows),
            "by_kind": counts,
            "bytes": sum(int(row.size or 0) for row in rows),
            "missing": [row.id for row in rows if row.upload_status != UploadStatus.READY.value],
        }

    def enforce_disk_budget(self, *, budget_bytes: int) -> list[str]:
        """Return the oldest ready artifacts that exceed the local budget so the janitor can drop them."""
        total = self.session.scalar(
            select(func.coalesce(func.sum(Artifact.size), 0)).where(
                Artifact.tenant_id == self.tenant_id, Artifact.upload_status == UploadStatus.READY.value
            )
        )
        if int(total or 0) <= budget_bytes:
            return []
        overflow = int(total or 0) - budget_bytes
        rows = list(
            self.session.scalars(
                select(Artifact)
                .where(Artifact.tenant_id == self.tenant_id, Artifact.upload_status == UploadStatus.READY.value)
                .order_by(Artifact.created_at.asc())
            ).all()
        )
        doomed: list[str] = []
        freed = 0
        for row in rows:
            if freed >= overflow:
                break
            doomed.append(row.id)
            freed += int(row.size or 0)
        return doomed


class FailureAnalysisRepository(Scoped[FailureAnalysis]):
    model = FailureAnalysis

    def start(self, *, project_id: str, execution_id: str, revision: int) -> FailureAnalysis:
        existing = self.session.scalar(
            select(FailureAnalysis).where(
                FailureAnalysis.tenant_id == self.tenant_id,
                FailureAnalysis.execution_id == execution_id,
                FailureAnalysis.revision == revision,
            )
        )
        if existing is not None and existing.status != AnalysisStatus.FAILED.value:
            return existing
        row = FailureAnalysis(
            id=new_id(),
            tenant_id=self.tenant_id,
            project_id=project_id,
            execution_id=execution_id,
            revision=revision,
            status=AnalysisStatus.RUNNING.value,
            evidence_refs=[],
            usage={},
        )
        self.session.add(row)
        self.session.flush()
        return row

    def finish(
        self,
        row: FailureAnalysis,
        *,
        status: str,
        failure_type: str | None = None,
        reason: str | None = None,
        suggestion: str | None = None,
        confidence: float | None = None,
        evidence_refs: list[dict[str, Any]] | None = None,
        model: str | None = None,
        prompt_version: str | None = None,
        usage: dict[str, Any] | None = None,
        source: str = "rules",
        error_code: str | None = None,
        is_hypothesis: bool | None = None,
    ) -> FailureAnalysis:
        row.status = status
        row.failure_type = failure_type
        row.reason = reason
        row.suggestion = suggestion
        row.confidence = confidence
        row.evidence_refs = evidence_refs or []
        row.model = model
        row.prompt_version = prompt_version
        row.usage = usage or {}
        row.source = source
        row.error_code = error_code
        # Only a model's guess is a hypothesis; the rule classification is a fact about the error code (§12.3).
        row.is_hypothesis = (source == "ai") if is_hypothesis is None else bool(is_hypothesis)
        self.session.flush()
        return row

    def for_execution(self, execution_id: str) -> list[FailureAnalysis]:
        return list(
            self.session.scalars(
                select(FailureAnalysis)
                .where(FailureAnalysis.tenant_id == self.tenant_id, FailureAnalysis.execution_id == execution_id)
                .order_by(FailureAnalysis.revision.desc())
            ).all()
        )

    def latest(self, execution_id: str) -> FailureAnalysis | None:
        return self.session.scalar(
            select(FailureAnalysis)
            .where(FailureAnalysis.tenant_id == self.tenant_id, FailureAnalysis.execution_id == execution_id)
            .order_by(FailureAnalysis.revision.desc())
            .limit(1)
        )

    def next_revision(self, execution_id: str) -> int:
        current = self.session.scalar(
            select(func.max(FailureAnalysis.revision)).where(
                FailureAnalysis.tenant_id == self.tenant_id, FailureAnalysis.execution_id == execution_id
            )
        )
        return int(current or 0) + 1


class ElementMemoryRepository(Scoped[ElementMemory]):
    """Candidate cache only: every entry must be re-validated on the live page before use (§8.3)."""

    model = ElementMemory

    def note_success(
        self,
        *,
        project_id: str,
        environment_id: str,
        origin: str,
        route_pattern: str,
        browser_family: str,
        target_fingerprint: str,
        description: str | None,
        strategy: str,
        selector: str,
        app_version: str | None,
    ) -> ElementMemory:
        row = self._find(
            environment_id=environment_id,
            origin=origin,
            route_pattern=route_pattern,
            strategy=strategy,
            selector=selector,
            target_fingerprint=target_fingerprint,
            browser_family=browser_family,
        )
        if row is None:
            row = ElementMemory(
                id=new_id(),
                tenant_id=self.tenant_id,
                project_id=project_id,
                environment_id=environment_id,
                origin=origin,
                route_pattern=route_pattern,
                browser_family=browser_family,
                target_fingerprint=target_fingerprint,
                description=description,
                strategy=strategy,
                selector=selector,
                success_count=1,
                failure_count=0,
                consecutive_failures=0,
                approval_status="UNREVIEWED",
                last_verified_at=utcnow(),
                app_version=app_version,
            )
            self.session.add(row)
        else:
            row.success_count = int(row.success_count or 0) + 1
            row.consecutive_failures = 0
            row.last_verified_at = utcnow()
            row.app_version = app_version
        self.session.flush()
        return row

    def note_failure(
        self,
        *,
        environment_id: str,
        origin: str,
        route_pattern: str,
        strategy: str,
        selector: str,
        target_fingerprint: str,
        browser_family: str,
    ) -> None:
        row = self._find(
            environment_id=environment_id,
            origin=origin,
            route_pattern=route_pattern,
            strategy=strategy,
            selector=selector,
            target_fingerprint=target_fingerprint,
            browser_family=browser_family,
        )
        if row is None:
            return
        row.failure_count = int(row.failure_count or 0) + 1
        row.consecutive_failures = int(row.consecutive_failures or 0) + 1
        if row.consecutive_failures >= 3:
            row.approval_status = "REVOKED"
        self.session.flush()

    def candidates(
        self,
        *,
        environment_id: str,
        origin: str,
        route_pattern: str,
        target_fingerprint: str,
        browser_family: str,
        limit: int = 3,
    ) -> list[ElementMemory]:
        rows = list(
            self.session.scalars(
                select(ElementMemory).where(
                    ElementMemory.tenant_id == self.tenant_id,
                    ElementMemory.environment_id == environment_id,
                    ElementMemory.origin == origin,
                    ElementMemory.route_pattern == route_pattern,
                    ElementMemory.target_fingerprint == target_fingerprint,
                    ElementMemory.browser_family == browser_family,
                    ElementMemory.approval_status == "APPROVED",
                )
            ).all()
        )
        scored = []
        for row in rows:
            attempts = int(row.success_count or 0) + int(row.failure_count or 0)
            if attempts < 2:
                continue  # too little evidence to trust a rate (§8.3)
            rate = int(row.success_count or 0) / attempts
            scored.append((rate, row))
        scored.sort(key=lambda item: item[0], reverse=True)
        return [row for _, row in scored[:limit]]

    def approve(self, memory_id: str) -> ElementMemory:
        row = self.require(memory_id)
        if row.consecutive_failures > 0:
            raise ApiError(ErrorCode.CONFLICT, "A failing candidate cannot be approved")
        row.approval_status = "APPROVED"
        self.session.flush()
        return row

    def revoke(self, memory_id: str) -> ElementMemory:
        row = self.require(memory_id)
        row.approval_status = "REVOKED"
        self.session.flush()
        return row

    def list_for_project(self, project_id: str, *, limit: int = 100) -> list[ElementMemory]:
        return list(
            self.session.scalars(
                select(ElementMemory)
                .where(ElementMemory.tenant_id == self.tenant_id, ElementMemory.project_id == project_id)
                .order_by(ElementMemory.updated_at.desc())
                .limit(limit)
            ).all()
        )

    def _find(
        self,
        *,
        environment_id: str,
        origin: str,
        route_pattern: str,
        strategy: str,
        selector: str,
        target_fingerprint: str,
        browser_family: str,
    ) -> ElementMemory | None:
        return self.session.scalar(
            select(ElementMemory).where(
                ElementMemory.tenant_id == self.tenant_id,
                ElementMemory.environment_id == environment_id,
                ElementMemory.origin == origin,
                ElementMemory.route_pattern == route_pattern,
                ElementMemory.strategy == strategy,
                ElementMemory.selector == selector,
                ElementMemory.target_fingerprint == target_fingerprint,
                ElementMemory.browser_family == browser_family,
            )
        )
