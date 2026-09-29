"""Creating and steering executions (§9.1, §13.2).

This is the only place that turns a compiled revision into a queued run, and it does it in one
transaction: the execution row, its step projections and the `execution.queued` outbox event commit
together. The API never writes to a queue directly — the scheduler picks the run up from the table.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from ..config import Settings, get_settings
from ..db.base import new_id
from ..db.models import EnvironmentRevision, TestExecution
from ..domain.enums import AnalysisStatus, ArtifactStatus, CompileStatus, ExecutionStatus, Outcome, Sensitivity
from ..domain.errors import ApiError, ErrorCode
from ..ir.models import TestIR
from ..orchestrator.events import ANALYSIS_REQUEST, EXECUTION_QUEUED
from ..orchestrator.finalize import ANALYSABLE_OUTCOMES, finalize
from ..orchestrator.reservations import release_for_execution
from ..repositories.cases import CaseRepository, CompileRepository
from ..repositories.executions import ExecutionRepository
from ..repositories.outbox import OutboxRepository
from ..repositories.platform import AccessRepository
from ..repositories.resources import EnvironmentRepository
from ..workers.environment import build_snapshot
from .secret_store import SecretStore

TRIGGER_MANUAL = "manual"
TRIGGER_RETRY = "retry"
TRIGGER_SCHEDULED = "scheduled"


class ExecutionService:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    # ------------------------------------------------------------------- create

    def create(
        self,
        *,
        tenant_id: str,
        project_id: str,
        case_id: str,
        revision_id: str | None = None,
        environment_id: str | None = None,
        environment_revision_id: str | None = None,
        compile_artifact_id: str | None = None,
        requested_by: str | None = None,
        run_variables: dict[str, Any] | None = None,
        evidence_mode: str | None = None,
        browser: str | None = None,
        trigger: str = TRIGGER_MANUAL,
        retry_of_execution_id: str | None = None,
    ) -> TestExecution:
        """Freeze the inputs, store the run and wake the scheduler — atomically."""
        from ..db.base import get_database

        database = get_database()
        with database.session(tenant_id) as session:
            cases = CaseRepository(session, tenant_id)
            case = cases.by_id(case_id)
            if case is None:
                raise ApiError(ErrorCode.NOT_FOUND, "Case not found in this tenant")
            revision_id = revision_id or case.current_revision_id
            if not revision_id:
                raise ApiError(ErrorCode.SEMANTIC_ERROR, "The case has no revision to run")
            revision = cases.require_revision(revision_id)
            if revision.case_id != case.id:
                raise ApiError(ErrorCode.SEMANTIC_ERROR, "That revision belongs to a different case")

            compiles = CompileRepository(session, tenant_id)
            if compile_artifact_id:
                compile_artifact = compiles.by_id(compile_artifact_id)
                if compile_artifact is None or compile_artifact.revision_id != revision.id:
                    raise ApiError(
                        ErrorCode.NOT_FOUND,
                        "That compile artifact is not a product of this revision",
                        details={"compile_artifact_id": compile_artifact_id},
                    )
                if not _is_executable(compile_artifact):
                    raise ApiError(
                        ErrorCode.COMPILE_REVIEW_REQUIRED,
                        "This compile artifact needs a human confirmation before it can run"
                        if compile_artifact.status == CompileStatus.NEEDS_REVIEW.value
                        else "This compile did not produce an executable IR",
                        details={"compile_artifact_id": compile_artifact.id, "status": compile_artifact.status},
                    )
            else:
                compile_artifact = compiles.executable_for_revision(revision.id)
            if compile_artifact is None:
                raise ApiError(
                    ErrorCode.EXECUTION_NOT_RUNNABLE,
                    "Compile an executable IR for this revision before running it",
                    details={"revision_id": revision.id},
                )
            ir_payload = dict(compile_artifact.ir or {})
            ir = _validate_ir(ir_payload)
            environment, environment_revision = self._environment(
                session,
                tenant_id,
                project_id,
                environment_id,
                environment_revision_id,
            )
            self._check_variables(ir, run_variables or {})
            browser = self._browser(browser, environment_revision)
            # A request or environment may pick NORMAL, but an IR that fills secret fields cannot (§10.4).
            mode = SecretStore.evidence_mode_for(
                ir_payload, requested=self._evidence_mode(evidence_mode, environment_revision)
            )

            executions = ExecutionRepository(session, tenant_id)
            execution = executions.create(
                project_id=project_id,
                case_id=case.id,
                revision_id=revision.id,
                compile_artifact_id=compile_artifact.id,
                environment_id=environment.id if environment else None,
                environment_revision_id=environment_revision.id,
                ir=ir_payload,
                ir_digest=compile_artifact.ir_digest or ir.digest(),
                snapshot=build_snapshot(
                    case=case,
                    revision=revision,
                    environment_revision=environment_revision,
                    project=AccessRepository(session, tenant_id).project(tenant_id, project_id),
                    run_variables=run_variables,
                    evidence_mode=mode,
                ),
                requested_by=requested_by,
                browser=browser,
                evidence_mode=mode,
                trigger=trigger,
                retry_of_execution_id=retry_of_execution_id,
            )
            # The step rows mirror the IR document order so a reader sees the whole plan up front.
            executions.add_steps(execution, [dict(step) for step in (ir_payload.get("steps") or [])])
            executions.transition(
                execution.id, to_status=ExecutionStatus.QUEUED.value, event_payload={"trigger": trigger}
            )
            OutboxRepository(session, tenant_id).enqueue(
                aggregate_id=execution.id,
                event_type=EXECUTION_QUEUED,
                payload={"execution_id": execution.id, "tenant_id": tenant_id, "project_id": project_id},
                discriminator=f"{EXECUTION_QUEUED}:{execution.id}",
            )
            session.commit()
            return execution

    def _environment(
        self,
        session,
        tenant_id: str,
        project_id: str,
        environment_id: str | None,
        environment_revision_id: str | None = None,
    ) -> tuple[Any, EnvironmentRevision]:
        repos = EnvironmentRepository(session, tenant_id)
        if environment_revision_id:
            # A named revision wins: the caller froze the network policy it reviewed (§3.2).
            revision = repos.require_revision(environment_revision_id)
            environment = repos.by_id(revision.environment_id)
            if environment is None or environment.project_id != project_id:
                raise ApiError(ErrorCode.NOT_FOUND, "Environment revision not found in this project")
            return environment, revision
        environment = None
        if environment_id:
            environment = repos.by_id(environment_id)
            if environment is None or environment.project_id != project_id:
                raise ApiError(ErrorCode.NOT_FOUND, "Environment not found in this project")
        else:
            environment = repos.by_name(project_id, "local") or (repos.list(project_id) or [None])[0]
        if environment is None:
            raise ApiError(ErrorCode.SEMANTIC_ERROR, "This project has no environment to run against")
        revision = repos.current_revision(environment)
        if revision is None:
            raise ApiError(
                ErrorCode.SEMANTIC_ERROR, f"Environment '{environment.environment_name}' has no published revision"
            )
        return environment, revision

    @staticmethod
    def _check_variables(ir: TestIR, run_variables: dict[str, Any]) -> None:
        declared = set(ir.variables)
        unknown = sorted(set(run_variables) - declared)
        if unknown:
            raise ApiError(
                ErrorCode.VARIABLE_UNDECLARED,
                f"Variables not declared by the case: {', '.join(unknown)}",
                details={"unknown": unknown, "declared": sorted(declared)},
            )
        missing = sorted(
            name for name, definition in ir.variables.items() if definition.required and name not in run_variables
        )
        if missing:
            raise ApiError(
                ErrorCode.VARIABLE_MISSING,
                f"Required variables are missing: {', '.join(missing)}",
                details={"missing": missing},
            )

    def _browser(self, browser: str | None, environment_revision: EnvironmentRevision) -> str:
        allowed = [
            str(item) for item in (environment_revision.config or {}).get("browsers") or []
        ] or self.settings.browser_channel_list
        # An unnamed browser means "whatever this environment runs", not "the platform default", which the
        # environment may not even allow.
        requested = browser or (allowed[0] if allowed else "")
        if requested not in allowed:
            raise ApiError(
                ErrorCode.BROWSER_NOT_SUPPORTED,
                f"Browser '{requested}' is not allowed here",
                details={"allowed": allowed},
            )
        return requested

    @staticmethod
    def _evidence_mode(evidence_mode: str | None, environment_revision: EnvironmentRevision) -> str:
        configured = str(
            ((environment_revision.config or {}).get("evidence") or {}).get("mode") or Sensitivity.NORMAL.value
        ).upper()
        mode = (evidence_mode or configured).upper()
        if mode not in (Sensitivity.NORMAL.value, Sensitivity.SENSITIVE.value):
            raise ApiError(ErrorCode.VALIDATION_ERROR, f"Unknown evidence mode '{mode}'")
        return mode

    # -------------------------------------------------------------- observation

    def get(self, *, tenant_id: str, execution_id: str) -> TestExecution:
        from ..db.base import get_database

        with get_database().session(tenant_id) as session:
            return ExecutionRepository(session, tenant_id).load(execution_id)

    def list(
        self,
        *,
        tenant_id: str,
        project_id: str,
        case_id: str | None = None,
        environment_id: str | None = None,
        statuses: tuple[str, ...] = (),
        outcomes: tuple[str, ...] = (),
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[TestExecution], int]:
        from ..db.base import get_database

        with get_database().session(tenant_id) as session:
            return ExecutionRepository(session, tenant_id).list(
                project_id=project_id,
                case_id=case_id,
                environment_id=environment_id,
                statuses=list(statuses),
                outcomes=list(outcomes),
                since=since,
                until=until,
                limit=limit,
                offset=offset,
            )

    def events(
        self, *, tenant_id: str, execution_id: str, after_seq: int = 0, limit: int = 200
    ) -> list[dict[str, Any]]:
        """The journal the SSE stream replays from (§13.4)."""
        from ..db.base import get_database

        with get_database().session(tenant_id) as session:
            rows = ExecutionRepository(session, tenant_id).events_after(execution_id, after_seq, limit=limit)
            return [
                {
                    "seq": row.seq,
                    "event": row.event_type,
                    "occurred_at": row.occurred_at.isoformat(),
                    "payload": row.payload,
                }
                for row in rows
            ]

    def probe(self, *, tenant_id: str, execution_id: str) -> dict[str, Any]:
        """The two numbers a stream needs per poll, without loading the IR or the snapshot."""
        from ..db.base import get_database

        with get_database().session(tenant_id) as session:
            repo = ExecutionRepository(session, tenant_id)
            execution = repo.load(execution_id)
            return {
                "status": execution.status,
                "outcome": execution.outcome,
                "state_version": int(execution.state_version or 0),
                "last_event_seq": int(execution.last_event_seq or 0),
                "first_event_seq": repo.first_event_seq(execution_id),
            }

    # ----------------------------------------------------------------- commands

    def cancel(
        self, *, tenant_id: str, execution_id: str, requested_by: str | None, reason: str = "requested"
    ) -> TestExecution:
        """Mark the intent only: whoever holds the lease stops at the next step boundary (§9.1)."""
        from ..db.base import get_database

        with get_database().session(tenant_id) as session:
            repo = ExecutionRepository(session, tenant_id)
            execution = repo.request_cancel(execution_id, requested_by=requested_by, reason=reason)
            if execution.status == ExecutionStatus.FINALIZING.value:
                # `request_cancel` only reaches FINALIZING when no worker ever claimed the run, so
                # there is nobody to hand the conclusion to: close it now instead of leaving it to
                # the reconciler's archive deadline.
                finalize(
                    session,
                    execution,
                    outcome=Outcome.CANCELLED.value,
                    error_code=ErrorCode.CANCELLED.value,
                    artifact_status=ArtifactStatus.COMPLETE.value,
                )
                release_for_execution(
                    session, execution.id, cleanup_confirmed=True, reason="cancelled before it started"
                )
            session.commit()
            return execution

    def retry(
        self,
        *,
        tenant_id: str,
        execution_id: str,
        requested_by: str | None,
        environment_revision_id: str | None = None,
    ) -> TestExecution:
        """A retry is a *new* execution: the first one's evidence stays untouched (§12.4).

        §13.2 allows a rerun to name a newer environment revision; every other input is replayed
        from the frozen snapshot so the two runs stay comparable.
        """
        previous = self.get(tenant_id=tenant_id, execution_id=execution_id)
        if previous.status != ExecutionStatus.FINISHED.value:
            raise ApiError(ErrorCode.CONFLICT, "Only a finished execution can be retried")
        snapshot = dict(previous.snapshot or {})
        return self.create(
            tenant_id=tenant_id,
            project_id=previous.project_id,
            case_id=previous.case_id,
            revision_id=previous.revision_id,
            environment_id=previous.environment_id,
            environment_revision_id=environment_revision_id or previous.environment_revision_id,
            compile_artifact_id=previous.compile_artifact_id,
            requested_by=requested_by,
            run_variables=dict(snapshot.get("run_variables") or {}),
            evidence_mode=previous.evidence_mode,
            browser=previous.browser,
            trigger=TRIGGER_RETRY,
            retry_of_execution_id=previous.id,
        )

    def analyze(self, *, tenant_id: str, execution_id: str, requested_by: str | None = None) -> str:
        """Re-analysis asks the model again without re-running the browser (§12.3)."""
        from ..db.base import get_database

        with get_database().session(tenant_id) as session:
            repo = ExecutionRepository(session, tenant_id)
            execution = repo.load(execution_id)
            if execution.status != ExecutionStatus.FINISHED.value:
                raise ApiError(ErrorCode.CONFLICT, "Only a finished execution can be analysed")
            # A pass has no failure to diagnose; analysing one would invent a failing step (§12.3).
            if execution.outcome not in ANALYSABLE_OUTCOMES:
                raise ApiError(
                    ErrorCode.CONFLICT,
                    f"Only a FAILED, ERROR or TIMED_OUT run can be analysed (this one is {execution.outcome})",
                )
            OutboxRepository(session, tenant_id).enqueue(
                aggregate_id=execution.id,
                event_type=ANALYSIS_REQUEST,
                payload={"execution_id": execution.id, "tenant_id": tenant_id, "requested_by": requested_by},
                discriminator=f"{ANALYSIS_REQUEST}:{execution.id}:{new_id()}",
            )
            execution.analysis_status = AnalysisStatus.PENDING.value
            session.flush()
            session.commit()
            return execution.id


def _validate_ir(payload: dict[str, Any]) -> TestIR:
    try:
        return TestIR.model_validate(payload)
    except Exception as exc:
        raise ApiError(ErrorCode.IR_VALIDATION_FAILED, f"Stored IR no longer validates: {type(exc).__name__}") from exc


def _is_executable(artifact: Any) -> bool:
    """A clean compile runs; a NEEDS_REVIEW one runs only once a human confirmed it (§3.2)."""
    if artifact.status == CompileStatus.SUCCEEDED.value:
        return True
    return artifact.status == CompileStatus.NEEDS_REVIEW.value and artifact.confirmed_at is not None


def execution_summary(execution: TestExecution) -> dict[str, Any]:
    """The list-view shape: enough to render a row without loading the report (§13.2)."""
    return {
        "id": execution.id,
        "project_id": execution.project_id,
        "case_id": execution.case_id,
        "case_name": (execution.snapshot or {}).get("case_name"),
        "revision_no": (execution.snapshot or {}).get("revision_no"),
        "status": execution.status,
        "outcome": execution.outcome,
        "error_code": execution.error_code,
        "trigger": execution.trigger,
        "browser": execution.browser,
        "browser_version": execution.browser_version,
        "evidence_mode": execution.evidence_mode,
        "artifact_status": execution.artifact_status,
        "analysis_status": execution.analysis_status,
        "queued_at": _iso(execution.queued_at),
        "started_at": _iso(execution.started_at),
        "ended_at": _iso(execution.ended_at),
        "active_ms": int(execution.active_ms or 0),
        "human_ms": int(execution.human_ms or 0),
        "human_tasks_used": int(execution.human_tasks_used or 0),
        "cancel_requested": execution.cancel_requested_at is not None,
        "retry_of": execution.retry_of_execution_id,
    }


def _iso(value: Any) -> str | None:
    return value.isoformat() if hasattr(value, "isoformat") else None
