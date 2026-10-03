"""SQLAlchemy tables for detailed design §11.2.

Identifiers are UUID strings (`String(36)`) rather than a dialect-specific UUID column so the
same schema runs unchanged on PostgreSQL (production, authoritative store) and SQLite
(development and the automated test suite). Composite `(tenant_id, id)` unique keys are the
target of child foreign keys, so a cross-tenant reference cannot be written even by a buggy
service layer.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy import (
    text as sql_text,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.schema import ForeignKeyConstraint

from ..domain.enums import (
    AnalysisStatus,
    ArtifactStatus,
    CleanupStatus,
    CommandStatus,
    CompileStatus,
    DispatchState,
    ExecutionStatus,
    HumanTaskStatus,
    Outcome,
    ReservationStatus,
    ScanStatus,
    Sensitivity,
    StepStatus,
    UploadStatus,
)
from .base import Base, UTCDateTime, new_id, utcnow
from .encrypted_types import EncryptedJSON as JSON
from .encrypted_types import EncryptedText as Text

STATUSES = [item.value for item in ExecutionStatus]
OUTCOMES = [item.value for item in Outcome]
STEP_STATUSES = [item.value for item in StepStatus]
COMPILE_STATUSES = [item.value for item in CompileStatus]
HUMAN_STATUSES = [item.value for item in HumanTaskStatus]
RESERVATION_STATUSES = [item.value for item in ReservationStatus]
ANALYSIS_STATUSES = [item.value for item in AnalysisStatus]
ARTIFACT_STATUSES = [item.value for item in ArtifactStatus]


class TimestampMixin:
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), default=utcnow, onupdate=utcnow, server_default=func.now()
    )


class TenantMixin:
    tenant_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)


class ProjectMixin(TenantMixin):
    project_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)


class Tenant(TimestampMixin, Base):
    __tablename__ = "tenant"
    __table_args__ = (CheckConstraint("status IN ('ACTIVE','SUSPENDED')", name="ck_tenant_status"),)

    name: Mapped[str] = mapped_column(String(200), nullable=False, unique=True)
    display_name: Mapped[str | None] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(16), default="ACTIVE", nullable=False)
    quota: Mapped[dict] = mapped_column(JSON, default=dict)


class AppUser(TimestampMixin, Base):
    """Platform-level identity table; no tenant column (§11.1)."""

    __tablename__ = "app_user"
    __table_args__ = (
        UniqueConstraint("issuer", "subject", name="uq_app_user_identity"),
        CheckConstraint("status IN ('ACTIVE','DISABLED')", name="ck_app_user_status"),
    )

    issuer: Mapped[str] = mapped_column(String(300), nullable=False)
    subject: Mapped[str] = mapped_column(String(200), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(200))
    email: Mapped[str | None] = mapped_column(String(300))
    status: Mapped[str] = mapped_column(String(16), default="ACTIVE", nullable=False)


class TenantMembership(TimestampMixin, TenantMixin, Base):
    __tablename__ = "tenant_membership"
    __table_args__ = (UniqueConstraint("tenant_id", "user_id", name="uq_tenant_membership"),)

    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("app_user.id", ondelete="CASCADE"), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)


class Project(TimestampMixin, TenantMixin, Base):
    __tablename__ = "project"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_project_tenant_id"),
        Index(
            "uq_project_active_name",
            "tenant_id",
            "name",
            unique=True,
            sqlite_where=sql_text("archived_at IS NULL"),
            postgresql_where=sql_text("archived_at IS NULL"),
        ),
    )

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text)
    quota: Mapped[dict] = mapped_column(JSON, default=dict)
    settings: Mapped[dict] = mapped_column(JSON, default=dict)
    archived_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    row_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)


class ProjectMembership(TimestampMixin, ProjectMixin, Base):
    __tablename__ = "project_membership"
    __table_args__ = (UniqueConstraint("tenant_id", "project_id", "user_id", name="uq_project_membership"),)

    user_id: Mapped[str] = mapped_column(String(36), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)


class PermissionGrant(TimestampMixin, ProjectMixin, Base):
    __tablename__ = "permission_grant"
    __table_args__ = (UniqueConstraint("tenant_id", "project_id", "user_id", "permission", name="uq_permission_grant"),)

    user_id: Mapped[str] = mapped_column(String(36), nullable=False)
    permission: Mapped[str] = mapped_column(String(48), nullable=False)
    granted_by: Mapped[str | None] = mapped_column(String(36))
    reason: Mapped[str | None] = mapped_column(String(300))
    expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class Environment(TimestampMixin, ProjectMixin, Base):
    __tablename__ = "environment"
    __table_args__ = (UniqueConstraint("tenant_id", "id", name="uq_environment_tenant_id"),)

    environment_name: Mapped[str] = mapped_column("name", String(120), nullable=False)
    current_revision_id: Mapped[str | None] = mapped_column(String(36))
    archived_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    row_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)


class EnvironmentRevision(TimestampMixin, ProjectMixin, Base):
    __tablename__ = "environment_revision"
    __table_args__ = (
        UniqueConstraint("tenant_id", "environment_id", "version", name="uq_environment_revision"),
        UniqueConstraint("tenant_id", "id", name="uq_environment_revision_tenant_id"),
        ForeignKeyConstraint(
            ("tenant_id", "environment_id"),
            ("environment.tenant_id", "environment.id"),
            name="fk_environment_revision_tenant",
            ondelete="CASCADE",
        ),
    )

    environment_id: Mapped[str] = mapped_column(String(36), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    secret_bindings: Mapped[dict] = mapped_column(JSON, default=dict)
    digest: Mapped[str] = mapped_column(String(80), nullable=False)
    created_by: Mapped[str | None] = mapped_column(String(36))


class SecretVersion(TimestampMixin, ProjectMixin, Base):
    __tablename__ = "secret_version"
    __table_args__ = (
        UniqueConstraint("tenant_id", "project_id", "logical_name", "version", name="uq_secret_version"),
        CheckConstraint("status IN ('ACTIVE','REVOKED')", name="ck_secret_version_status"),
    )

    logical_name: Mapped[str] = mapped_column(String(120), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    provider: Mapped[str] = mapped_column(String(24), default="local_fernet", nullable=False)
    provider_ref: Mapped[str | None] = mapped_column(String(300))
    ciphertext: Mapped[str | None] = mapped_column(Text)
    key_id: Mapped[str | None] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(String(16), default="ACTIVE", nullable=False)
    created_by: Mapped[str | None] = mapped_column(String(36))


class TestCase(TimestampMixin, ProjectMixin, Base):
    __tablename__ = "test_case"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_test_case_tenant_id"),
        Index("ix_test_case_project_updated", "tenant_id", "project_id", "updated_at"),
        #: The MCP case page keys on `(created_at DESC, id DESC)` (§6.2, §11), which the `updated_at`
        #: ordering above cannot serve; without this the page sorts the whole project to find 20 rows.
        Index("ix_test_case_project_created", "tenant_id", "project_id", "created_at", "id"),
    )

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    current_revision_id: Mapped[str | None] = mapped_column(String(36))
    archived_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    row_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    created_by: Mapped[str | None] = mapped_column(String(36))


class CaseRevision(TimestampMixin, ProjectMixin, Base):
    __tablename__ = "case_revision"
    __table_args__ = (
        UniqueConstraint("tenant_id", "case_id", "version", name="uq_case_revision_version"),
        UniqueConstraint("tenant_id", "id", name="uq_case_revision_tenant_id"),
        ForeignKeyConstraint(
            ("tenant_id", "case_id"), ("test_case.tenant_id", "test_case.id"), name="fk_case_revision_tenant"
        ),
    )

    case_id: Mapped[str] = mapped_column(String(36), ForeignKey("test_case.id", ondelete="CASCADE"), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    markdown: Mapped[str] = mapped_column(Text, nullable=False)
    source_digest: Mapped[str] = mapped_column(String(80), nullable=False)
    dsl_version: Mapped[str] = mapped_column(String(24), default="1.0", nullable=False)
    title: Mapped[str | None] = mapped_column(String(200))
    created_by: Mapped[str | None] = mapped_column(String(36))


class Tag(TimestampMixin, ProjectMixin, Base):
    __tablename__ = "tag"
    __table_args__ = (UniqueConstraint("tenant_id", "project_id", "name", name="uq_tag_name"),)

    name: Mapped[str] = mapped_column("name", String(60), nullable=False)


class CaseTag(TimestampMixin, TenantMixin, Base):
    __tablename__ = "case_tag"
    __table_args__ = (UniqueConstraint("tenant_id", "case_id", "tag_id", name="uq_case_tag"),)

    case_id: Mapped[str] = mapped_column(String(36), ForeignKey("test_case.id", ondelete="CASCADE"), nullable=False)
    tag_id: Mapped[str] = mapped_column(String(36), ForeignKey("tag.id", ondelete="CASCADE"), nullable=False)


class CompileArtifact(TimestampMixin, ProjectMixin, Base):
    __tablename__ = "compile_artifact"
    __table_args__ = (
        UniqueConstraint("dedupe_key", name="uq_compile_artifact_dedupe"),
        UniqueConstraint("tenant_id", "id", name="uq_compile_artifact_tenant_id"),
        Index("ix_compile_artifact_revision_latest", "tenant_id", "revision_id", "created_at", "id"),
        CheckConstraint(f"status IN {tuple(COMPILE_STATUSES)}", name="ck_compile_status"),
        ForeignKeyConstraint(
            ("tenant_id", "revision_id"),
            ("case_revision.tenant_id", "case_revision.id"),
            name="fk_compile_artifact_tenant",
            ondelete="CASCADE",
        ),
    )

    revision_id: Mapped[str] = mapped_column(String(36), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default=CompileStatus.PENDING.value, nullable=False)
    ir: Mapped[dict | None] = mapped_column(JSON)
    ir_digest: Mapped[str | None] = mapped_column(String(80))
    source_digest: Mapped[str] = mapped_column(String(80), nullable=False)
    compiler_version: Mapped[str] = mapped_column(String(24), nullable=False)
    compiler_mode: Mapped[str] = mapped_column(String(24), default="deterministic", nullable=False)
    model: Mapped[str | None] = mapped_column(String(120))
    prompt_version: Mapped[str | None] = mapped_column(String(60))
    diagnostics: Mapped[list] = mapped_column(JSON, default=list)
    review_items: Mapped[list] = mapped_column(JSON, default=list)
    usage: Mapped[dict] = mapped_column(JSON, default=dict)
    error_code: Mapped[str | None] = mapped_column(String(48))
    confirmed_by: Mapped[str | None] = mapped_column(String(36))
    confirmed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    dedupe_key: Mapped[str] = mapped_column(String(80), nullable=False)


class Attachment(TimestampMixin, ProjectMixin, Base):
    __tablename__ = "attachment"
    __table_args__ = (
        CheckConstraint(f"scan_status IN {tuple(item.value for item in ScanStatus)}", name="ck_attachment_scan"),
        Index("ix_attachment_digest", "tenant_id", "project_id", "digest"),
    )

    filename: Mapped[str] = mapped_column(String(300), nullable=False)
    media_type: Mapped[str] = mapped_column(String(120), nullable=False)
    size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    digest: Mapped[str] = mapped_column(String(80), nullable=False)
    object_key: Mapped[str] = mapped_column(String(500), nullable=False)
    scan_status: Mapped[str] = mapped_column(String(16), default=ScanStatus.PENDING.value, nullable=False)
    scan_detail: Mapped[str | None] = mapped_column(String(300))
    created_by: Mapped[str | None] = mapped_column(String(36))


class TestExecution(TimestampMixin, ProjectMixin, Base):
    __tablename__ = "test_execution"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_execution_tenant_id"),
        Index("ix_execution_project_created", "tenant_id", "project_id", "created_at"),
        Index("ix_execution_queue", "status", "queued_at"),
        Index("ix_execution_lease", "status", "lease_until"),
        CheckConstraint(f"status IN {tuple(STATUSES)}", name="ck_execution_status"),
        CheckConstraint(
            f"outcome IS NULL OR outcome IN {tuple(OUTCOMES)}",
            name="ck_execution_outcome_value",
        ),
        CheckConstraint(
            "(status IN ('FINALIZING','FINISHED') AND outcome IS NOT NULL)"
            " OR (status NOT IN ('FINALIZING','FINISHED') AND outcome IS NULL)",
            name="ck_execution_outcome_stage",
        ),
        CheckConstraint(f"analysis_status IN {tuple(ANALYSIS_STATUSES)}", name="ck_execution_analysis"),
        ForeignKeyConstraint(
            ("tenant_id", "compile_artifact_id"),
            ("compile_artifact.tenant_id", "compile_artifact.id"),
            name="fk_execution_artifact_tenant",
        ),
        ForeignKeyConstraint(
            ("tenant_id", "revision_id"),
            ("case_revision.tenant_id", "case_revision.id"),
            name="fk_execution_revision_tenant",
        ),
        CheckConstraint(f"artifact_status IN {tuple(ARTIFACT_STATUSES)}", name="ck_execution_artifact"),
    )

    case_id: Mapped[str] = mapped_column(String(36), nullable=False)
    revision_id: Mapped[str] = mapped_column(String(36), nullable=False)
    compile_artifact_id: Mapped[str] = mapped_column(String(36), nullable=False)
    environment_id: Mapped[str | None] = mapped_column(String(36))
    environment_revision_id: Mapped[str | None] = mapped_column(String(36))
    status: Mapped[str] = mapped_column(String(20), default=ExecutionStatus.CREATED.value, nullable=False)
    outcome: Mapped[str | None] = mapped_column(String(20))
    error_code: Mapped[str | None] = mapped_column(String(48))
    error_detail: Mapped[dict | None] = mapped_column(JSON)
    snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    ir: Mapped[dict] = mapped_column(JSON, nullable=False)
    ir_digest: Mapped[str | None] = mapped_column(String(80))
    trigger: Mapped[str] = mapped_column(String(24), default="manual", nullable=False)
    requested_by: Mapped[str | None] = mapped_column(String(36))
    owner_worker_id: Mapped[str | None] = mapped_column(String(80))
    lease_epoch: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    lease_until: Mapped[datetime | None] = mapped_column(UTCDateTime())
    state_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    last_event_seq: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    cancel_requested_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    retry_of_execution_id: Mapped[str | None] = mapped_column(String(36))
    browser: Mapped[str] = mapped_column(String(24), default="chromium", nullable=False)
    browser_version: Mapped[str | None] = mapped_column(String(60))
    evidence_mode: Mapped[str] = mapped_column(String(16), default=Sensitivity.NORMAL.value, nullable=False)
    cleanup_status: Mapped[str] = mapped_column(String(20), default=CleanupStatus.UNKNOWN.value, nullable=False)
    artifact_status: Mapped[str] = mapped_column(String(16), default=ArtifactStatus.PENDING.value, nullable=False)
    analysis_status: Mapped[str] = mapped_column(String(20), default=AnalysisStatus.NOT_REQUIRED.value, nullable=False)
    queued_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    ended_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    active_ms: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    human_ms: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    human_tasks_used: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    finalizing_deadline_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class StepExecution(TimestampMixin, ProjectMixin, Base):
    __tablename__ = "step_execution"
    __table_args__ = (
        UniqueConstraint("tenant_id", "execution_id", "step_id", name="uq_step_execution"),
        ForeignKeyConstraint(
            ("tenant_id", "execution_id"),
            ("test_execution.tenant_id", "test_execution.id"),
            name="fk_step_execution_tenant",
        ),
        Index("ix_step_execution_order", "execution_id", "step_no"),
        #: The MCP step page orders `(step_no ASC, id ASC)` behind a tenant and execution filter (§6.2, §11).
        #: The index above serves the step number but not the tiebreaker, because `id` is a string primary
        #: key here and not the rowid SQLite would otherwise complete a secondary index with - so every
        #: continuation of a keyset cursor sorts what it read rather than reading it in order.
        Index("ix_step_execution_page", "tenant_id", "execution_id", "step_no", "id"),
        CheckConstraint(f"status IN {tuple(STEP_STATUSES)}", name="ck_step_status"),
        CheckConstraint(f"dispatch_state IN {tuple(item.value for item in DispatchState)}", name="ck_step_dispatch"),
    )

    execution_id: Mapped[str] = mapped_column(String(36), nullable=False)
    step_id: Mapped[str] = mapped_column(String(12), nullable=False)
    step_no: Mapped[int] = mapped_column(Integer, nullable=False)
    action: Mapped[str] = mapped_column(String(24), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default=StepStatus.PENDING.value, nullable=False)
    dispatch_state: Mapped[str] = mapped_column(String(20), default=DispatchState.NOT_STARTED.value, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    ended_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    error_code: Mapped[str | None] = mapped_column(String(48))
    error_detail: Mapped[dict | None] = mapped_column(JSON)
    locator_attempts: Mapped[list] = mapped_column(JSON, default=list)
    locator_strategy: Mapped[str | None] = mapped_column(String(24))
    resume_phase: Mapped[str | None] = mapped_column(String(20))
    remaining_timeout_ms: Mapped[int | None] = mapped_column(Integer)
    artifact_ids: Mapped[list] = mapped_column(JSON, default=list)
    lease_epoch: Mapped[int | None] = mapped_column(BigInteger)


class HumanTask(TimestampMixin, ProjectMixin, Base):
    __tablename__ = "human_task"
    __table_args__ = (
        Index(
            "uq_active_human_task",
            "tenant_id",
            "execution_id",
            unique=True,
            sqlite_where=sql_text(f"status IN {tuple(HUMAN_STATUSES[:3])}"),
            postgresql_where=sql_text(f"status IN {tuple(HUMAN_STATUSES[:3])}"),
        ),
        UniqueConstraint("pause_token", name="uq_human_pause_token"),
        CheckConstraint(f"status IN {tuple(HUMAN_STATUSES)}", name="ck_human_status"),
        Index("ix_human_open", "tenant_id", "project_id", "status"),
        ForeignKeyConstraint(
            ("tenant_id", "execution_id"),
            ("test_execution.tenant_id", "test_execution.id"),
            name="fk_human_task_tenant",
        ),
    )

    execution_id: Mapped[str] = mapped_column(String(36), nullable=False)
    step_id: Mapped[str] = mapped_column(String(12), nullable=False)
    reason: Mapped[str] = mapped_column(String(48), nullable=False)
    detail: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default=HumanTaskStatus.PENDING.value, nullable=False)
    assignee_id: Mapped[str | None] = mapped_column(String(36))
    control_lease_until: Mapped[datetime | None] = mapped_column(UTCDateTime())
    deadline: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    session_epoch: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    pause_token: Mapped[str] = mapped_column(String(48), nullable=False)
    resume_condition: Mapped[dict | None] = mapped_column(JSON)
    resume_phase: Mapped[str | None] = mapped_column(String(20))
    resume_requested_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    outcome_note: Mapped[str | None] = mapped_column(String(300))


class ExecutionCommand(TimestampMixin, ProjectMixin, Base):
    __tablename__ = "execution_command"
    __table_args__ = (
        UniqueConstraint("tenant_id", "execution_id", "dedupe_key", name="uq_execution_command"),
        Index("ix_command_open", "status", "created_at"),
        CheckConstraint(f"status IN {tuple(item.value for item in CommandStatus)}", name="ck_command_status"),
        ForeignKeyConstraint(
            ("tenant_id", "execution_id"),
            ("test_execution.tenant_id", "test_execution.id"),
            name="fk_execution_command_tenant",
        ),
    )

    execution_id: Mapped[str] = mapped_column(String(36), nullable=False)
    human_task_id: Mapped[str | None] = mapped_column(String(36))
    command_type: Mapped[str] = mapped_column(String(24), nullable=False)
    dedupe_key: Mapped[str] = mapped_column(String(80), nullable=False)
    status: Mapped[str] = mapped_column(String(16), default=CommandStatus.PENDING.value, nullable=False)
    requested_by: Mapped[str | None] = mapped_column(String(36))
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    result: Mapped[dict | None] = mapped_column(JSON)
    expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    processed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class ExecutionEvent(TimestampMixin, TenantMixin, Base):
    __tablename__ = "execution_event"
    __table_args__ = (
        UniqueConstraint("tenant_id", "execution_id", "seq", name="uq_execution_event"),
        ForeignKeyConstraint(
            ("tenant_id", "execution_id"),
            ("test_execution.tenant_id", "test_execution.id"),
            name="fk_execution_event_tenant",
        ),
    )

    execution_id: Mapped[str] = mapped_column(String(36), nullable=False)
    seq: Mapped[int] = mapped_column(BigInteger, nullable=False)
    event_type: Mapped[str] = mapped_column(String(48), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    occurred_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, nullable=False)


class Artifact(TimestampMixin, ProjectMixin, Base):
    __tablename__ = "artifact"
    __table_args__ = (
        UniqueConstraint("object_key", name="uq_artifact_object_key"),
        Index("ix_artifact_execution", "tenant_id", "execution_id", "kind"),
        #: A step page asks for the evidence of its own twenty steps (§8.2), and the index above cannot reach
        #: them without reading the run's whole evidence list first. A 200-step run with a screenshot and a
        #: trace per step is four thousand rows read to answer with one hundred of them.
        Index("ix_artifact_step_evidence", "tenant_id", "execution_id", "step_id", "created_at", "id"),
        CheckConstraint(f"upload_status IN {tuple(item.value for item in UploadStatus)}", name="ck_artifact_upload"),
        ForeignKeyConstraint(
            ("tenant_id", "execution_id"),
            ("test_execution.tenant_id", "test_execution.id"),
            name="fk_artifact_tenant",
        ),
    )

    execution_id: Mapped[str] = mapped_column(String(36), nullable=False)
    step_id: Mapped[str | None] = mapped_column(String(12))
    kind: Mapped[str] = mapped_column(String(24), nullable=False)
    name: Mapped[str | None] = mapped_column(String(120))
    object_key: Mapped[str] = mapped_column(String(500), nullable=False)
    sha256: Mapped[str | None] = mapped_column(String(80))
    size: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    media_type: Mapped[str] = mapped_column(String(120), default="application/octet-stream", nullable=False)
    sensitivity: Mapped[str] = mapped_column(String(16), default=Sensitivity.NORMAL.value, nullable=False)
    upload_status: Mapped[str] = mapped_column(String(16), default=UploadStatus.PENDING.value, nullable=False)
    publish_allowed: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    artifact_metadata: Mapped[dict] = mapped_column("metadata", JSON, default=dict)
    retention_until: Mapped[datetime | None] = mapped_column(UTCDateTime())


class FailureAnalysis(TimestampMixin, ProjectMixin, Base):
    __tablename__ = "failure_analysis"
    __table_args__ = (
        UniqueConstraint("tenant_id", "execution_id", "revision", name="uq_analysis_revision"),
        CheckConstraint(f"status IN {tuple(ANALYSIS_STATUSES)}", name="ck_analysis_status"),
        ForeignKeyConstraint(
            ("tenant_id", "execution_id"),
            ("test_execution.tenant_id", "test_execution.id"),
            name="fk_failure_analysis_tenant",
        ),
    )

    execution_id: Mapped[str] = mapped_column(String(36), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default=AnalysisStatus.PENDING.value, nullable=False)
    failure_type: Mapped[str | None] = mapped_column(String(32))
    reason: Mapped[str | None] = mapped_column(Text)
    suggestion: Mapped[str | None] = mapped_column(Text)
    confidence: Mapped[float | None] = mapped_column(Float)
    evidence_refs: Mapped[list] = mapped_column(JSON, default=list)
    is_hypothesis: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    model: Mapped[str | None] = mapped_column(String(120))
    prompt_version: Mapped[str | None] = mapped_column(String(60))
    error_code: Mapped[str | None] = mapped_column(String(48))
    source: Mapped[str] = mapped_column(String(16), default="rules", nullable=False)
    usage: Mapped[dict] = mapped_column(JSON, default=dict)


class ElementMemory(TimestampMixin, ProjectMixin, Base):
    __tablename__ = "element_memory"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "environment_id",
            "origin",
            "route_pattern",
            "target_fingerprint",
            "strategy",
            "selector",
            name="uq_element_memory_key",
        ),
        CheckConstraint("approval_status IN ('UNREVIEWED','APPROVED','REVOKED')", name="ck_memory_approval"),
    )

    environment_id: Mapped[str] = mapped_column(String(36), nullable=False)
    origin: Mapped[str] = mapped_column(String(300), nullable=False)
    route_pattern: Mapped[str] = mapped_column(String(300), nullable=False)
    browser_family: Mapped[str] = mapped_column(String(24), default="chromium", nullable=False)
    target_fingerprint: Mapped[str] = mapped_column(String(80), nullable=False)
    description: Mapped[str | None] = mapped_column(String(300))
    strategy: Mapped[str] = mapped_column(String(16), nullable=False)
    selector: Mapped[str] = mapped_column(String(500), nullable=False)
    success_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    failure_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    approval_status: Mapped[str] = mapped_column(String(16), default="UNREVIEWED", nullable=False)
    last_verified_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    app_version: Mapped[str | None] = mapped_column(String(60))


#: The `worker_lease.capabilities` key naming the queues a worker process was started for (§13.5).
QUEUES_KEY = "queues"


class WorkerLease(TimestampMixin, Base):
    """Platform-level scheduling table (§11.1); no tenant column.

    `capabilities[QUEUES_KEY]` lists the queue names the process was started for. It is written by the
    announcer and read by the readiness probe, which may not infer that an unlabelled worker covers a
    queue it never claimed (§13.5).
    """

    __tablename__ = "worker_lease"
    __table_args__ = (UniqueConstraint("worker_id", name="uq_worker_lease_id"),)

    worker_id: Mapped[str] = mapped_column(String(80), nullable=False)
    pool_id: Mapped[str | None] = mapped_column(String(36))
    capabilities: Mapped[dict] = mapped_column(JSON, default=dict)
    capacity: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    active_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    heartbeat_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, nullable=False)
    draining: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class WorkerPool(TimestampMixin, Base):
    __tablename__ = "worker_pool"
    __table_args__ = (UniqueConstraint("name", name="uq_worker_pool_name"),)

    name: Mapped[str] = mapped_column(String(120), nullable=False)
    network_policy_id: Mapped[str | None] = mapped_column(String(120))
    capabilities: Mapped[dict] = mapped_column(JSON, default=dict)
    capacity: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    reserved_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class ExecutionReservation(TimestampMixin, ProjectMixin, Base):
    __tablename__ = "execution_reservation"
    __table_args__ = (
        Index(
            "uq_reservation_active",
            "execution_id",
            unique=True,
            sqlite_where=sql_text("status <> 'RELEASED'"),
            postgresql_where=sql_text("status <> 'RELEASED'"),
        ),
        CheckConstraint(f"status IN {tuple(RESERVATION_STATUSES)}", name="ck_reservation_status"),
        Index("ix_reservation_pool", "pool_id", "status"),
        ForeignKeyConstraint(
            ("tenant_id", "execution_id"),
            ("test_execution.tenant_id", "test_execution.id"),
            name="fk_reservation_tenant",
        ),
    )

    execution_id: Mapped[str] = mapped_column(String(36), nullable=False)
    pool_id: Mapped[str] = mapped_column(String(36), nullable=False)
    generation: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default=ReservationStatus.RESERVED.value, nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    released_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class Outbox(TimestampMixin, TenantMixin, Base):
    __tablename__ = "outbox"
    __table_args__ = (
        UniqueConstraint("dedupe_key", name="uq_outbox_dedupe"),
        Index(
            "ix_pending_outbox",
            "next_attempt_at",
            "created_at",
            sqlite_where=sql_text("published_at IS NULL"),
            postgresql_where=sql_text("published_at IS NULL"),
        ),
    )

    aggregate_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    event_type: Mapped[str] = mapped_column(String(48), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    dedupe_key: Mapped[str] = mapped_column(String(120), nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    next_attempt_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    last_error: Mapped[str | None] = mapped_column(String(300))


class IdempotencyRecord(TimestampMixin, TenantMixin, Base):
    __tablename__ = "idempotency_record"
    __table_args__ = (UniqueConstraint("tenant_id", "actor_id", "route", "key", name="uq_idempotency"),)

    actor_id: Mapped[str] = mapped_column(String(36), nullable=False)
    route: Mapped[str] = mapped_column(String(120), nullable=False)
    key: Mapped[str] = mapped_column(String(120), nullable=False)
    request_digest: Mapped[str] = mapped_column(String(80), nullable=False)
    resource_id: Mapped[str | None] = mapped_column(String(36))
    response: Mapped[dict | None] = mapped_column(JSON)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)


class AuditLog(TimestampMixin, TenantMixin, Base):
    """Append-only audit trail (§11.2)."""

    __tablename__ = "audit_log"
    __table_args__ = (Index("ix_audit_tenant_created", "tenant_id", "created_at"),)

    actor_id: Mapped[str | None] = mapped_column(String(36))
    project_id: Mapped[str | None] = mapped_column(String(36))
    operation: Mapped[str] = mapped_column(String(80), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(48), nullable=False)
    resource_id: Mapped[str | None] = mapped_column(String(36))
    request_id: Mapped[str | None] = mapped_column(String(60))
    detail: Mapped[dict] = mapped_column(JSON, default=dict)


class SchemaMigration(TimestampMixin, Base):
    """One row per structure version the deploy job applied to this database (§13.6, AC-25).

    Deployment bookkeeping rather than tenant data: it carries no `tenant_id`, because the question a
    starting process asks is "is this the structure my models expect", which is the same question for
    every tenant. The row is what makes that answerable without a process that may not change the
    database reading the catalogue.
    """

    __tablename__ = "schema_migration"
    __table_args__ = (UniqueConstraint("version", name="uq_schema_migration_version"),)

    version: Mapped[int] = mapped_column(Integer, nullable=False)
    contract: Mapped[str] = mapped_column(String(60), nullable=False)
    applied_by: Mapped[str] = mapped_column(String(60), nullable=False)
    note: Mapped[str | None] = mapped_column(String(300))
