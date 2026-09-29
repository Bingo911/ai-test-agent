"""Domain enumerations mirroring the state model in detailed design §9.1."""

from __future__ import annotations

from enum import Enum


class StrEnum(str, Enum):
    """Text enum persisted as VARCHAR with CHECK constraints."""

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value


class ExecutionStatus(StrEnum):
    CREATED = "CREATED"
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    WAIT_HUMAN = "WAIT_HUMAN"
    FINALIZING = "FINALIZING"
    FINISHED = "FINISHED"


ACTIVE_STATUSES = frozenset(
    {
        ExecutionStatus.CREATED,
        ExecutionStatus.QUEUED,
        ExecutionStatus.RUNNING,
        ExecutionStatus.WAIT_HUMAN,
        ExecutionStatus.FINALIZING,
    }
)


class Outcome(StrEnum):
    PASSED = "PASSED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    TIMED_OUT = "TIMED_OUT"
    ERROR = "ERROR"


class AnalysisStatus(StrEnum):
    NOT_REQUIRED = "NOT_REQUIRED"
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class ArtifactStatus(StrEnum):
    PENDING = "PENDING"
    COMPLETE = "COMPLETE"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"


class StepStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    WAIT_HUMAN = "WAIT_HUMAN"
    PASSED = "PASSED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"
    CANCELLED = "CANCELLED"
    ERROR = "ERROR"


class DispatchState(StrEnum):
    NOT_STARTED = "NOT_STARTED"
    INTENT_RECORDED = "INTENT_RECORDED"
    ACKNOWLEDGED = "ACKNOWLEDGED"


class CompileStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    FAILED = "FAILED"


class HumanTaskStatus(StrEnum):
    PENDING = "PENDING"
    CLAIMED = "CLAIMED"
    RESUME_REQUESTED = "RESUME_REQUESTED"
    COMPLETED = "COMPLETED"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"


ACTIVE_HUMAN_STATUSES = ("PENDING", "CLAIMED", "RESUME_REQUESTED")


class ResumePhase(StrEnum):
    BEFORE_ACTION = "before_action"
    AFTER_ACTION = "after_action"
    UNKNOWN = "unknown"


class ReservationStatus(StrEnum):
    RESERVED = "RESERVED"
    ACTIVE = "ACTIVE"
    QUARANTINED = "QUARANTINED"
    RELEASED = "RELEASED"


OCCUPYING_RESERVATION_STATUSES = ("RESERVED", "ACTIVE", "QUARANTINED")


class CleanupStatus(StrEnum):
    CLEAN = "CLEAN"
    QUARANTINED = "QUARANTINED"
    UNKNOWN = "UNKNOWN"


class Sensitivity(StrEnum):
    NORMAL = "NORMAL"
    SENSITIVE = "SENSITIVE"


class UploadStatus(StrEnum):
    PENDING = "PENDING"
    READY = "READY"
    MISSING = "MISSING"
    FAILED = "FAILED"
    EXPIRED = "EXPIRED"


class ArtifactKind(StrEnum):
    SCREENSHOT = "SCREENSHOT"
    DOM = "DOM"
    CONSOLE = "CONSOLE"
    NETWORK = "NETWORK"
    TRACE = "TRACE"
    VIDEO = "VIDEO"
    LOG = "LOG"


class ScanStatus(StrEnum):
    PENDING = "PENDING"
    CLEAN = "CLEAN"
    INFECTED = "INFECTED"
    ERROR = "ERROR"


class FailureType(StrEnum):
    ASSERTION_FAILURE = "ASSERTION_FAILURE"
    LOCATOR_FAILURE = "LOCATOR_FAILURE"
    NAVIGATION_FAILURE = "NAVIGATION_FAILURE"
    TARGET_NETWORK_ERROR = "TARGET_NETWORK_ERROR"
    BROWSER_FAILURE = "BROWSER_FAILURE"
    SESSION_LOST = "SESSION_LOST"
    HUMAN_TIMEOUT = "HUMAN_TIMEOUT"
    UNKNOWN = "UNKNOWN"


class CommandStatus(StrEnum):
    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    PROCESSED = "PROCESSED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"


class CommandType(StrEnum):
    CANCEL = "CANCEL"
    PAUSE = "PAUSE"
    RESUME = "RESUME"
    HUMAN_OTP = "HUMAN_OTP"
    #: One whitelisted remote operation (click/type/key/scroll) sent to the live session (§10.2).
    CONTROL = "CONTROL"


class EventType(StrEnum):
    EXECUTION_STATUS_CHANGED = "execution.status_changed"
    STEP_STARTED = "step.started"
    STEP_FINISHED = "step.finished"
    HUMAN_CREATED = "human.created"
    HUMAN_RESUMED = "human.resumed"
    ARTIFACT_READY = "artifact.ready"
    ANALYSIS_READY = "analysis.ready"
    EXECUTION_LOG = "execution.log"


class Role(StrEnum):
    ENGINEER = "engineer"
    LEAD = "lead"
    ADMIN = "admin"


class Permission(StrEnum):
    CASE_READ = "case_read"
    CASE_WRITE = "case_write"
    CASE_COMPILE = "case_compile"
    EXECUTION_RUN = "execution_run"
    EXECUTION_CANCEL_OWN = "execution_cancel_own"
    EXECUTION_CANCEL_ANY = "execution_cancel_any"
    HUMAN_CONTROL = "human_control"
    PROJECT_MANAGE = "project_manage"
    QUALITY_READ = "quality_read"
    ADMIN_USERS = "admin_users"
    ENV_MANAGE = "env_manage"
    SECRET_MANAGE = "secret_manage"
    SENSITIVE_ARTIFACT_READ = "sensitive_artifact_read"
    AUDIT_READ = "audit_read"


ROLE_PERMISSIONS: dict[Role, frozenset[Permission]] = {
    Role.ENGINEER: frozenset(
        {
            Permission.CASE_READ,
            Permission.CASE_WRITE,
            Permission.CASE_COMPILE,
            Permission.EXECUTION_RUN,
            Permission.EXECUTION_CANCEL_OWN,
            Permission.QUALITY_READ,
        }
    ),
    Role.LEAD: frozenset(
        {
            Permission.CASE_READ,
            Permission.CASE_WRITE,
            Permission.CASE_COMPILE,
            Permission.EXECUTION_RUN,
            Permission.EXECUTION_CANCEL_OWN,
            Permission.EXECUTION_CANCEL_ANY,
            Permission.PROJECT_MANAGE,
            Permission.QUALITY_READ,
            Permission.AUDIT_READ,
        }
    ),
    Role.ADMIN: frozenset(
        {
            Permission.CASE_READ,
            Permission.CASE_WRITE,
            Permission.CASE_COMPILE,
            Permission.EXECUTION_RUN,
            Permission.EXECUTION_CANCEL_OWN,
            Permission.EXECUTION_CANCEL_ANY,
            Permission.PROJECT_MANAGE,
            Permission.QUALITY_READ,
            Permission.ADMIN_USERS,
            Permission.ENV_MANAGE,
            Permission.SECRET_MANAGE,
            Permission.AUDIT_READ,
        }
    ),
}
