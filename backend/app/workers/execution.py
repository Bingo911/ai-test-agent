"""The browser execution worker (§9.2, §9.4, §9.5).

This is the only place that starts a browser. It claims a reservation, takes the lease, runs the IR
step by step through the executor adapter, and always ends by finalising the execution and giving
the slot back — the outcome is decided exactly once, whichever of the worker and the reconciler
reaches the database first.
"""

from __future__ import annotations

import asyncio
import os
import socket
import threading
from typing import Any

from ..config import Settings, get_settings
from ..db.base import get_database, monotonic_ms
from ..db.models import TestExecution
from ..domain.enums import ArtifactStatus, ExecutionStatus, Outcome, StepStatus
from ..domain.errors import ApiError, ErrorCode
from ..domain.state_machine import derive_outcome
from ..evidence.sink import DatabaseEvidenceSink
from ..executors.contracts import HumanRequired, RunContext, SessionConfig
from ..executors.memory import DatabaseElementMemory, NoElementMemory
from ..executors.playwright.adapter import KIND_TO_ERROR, PlaywrightExecutor
from ..executors.vision import AiVisionResolver
from ..human.gate import HumanGate, HumanGateError, LeaseGone
from ..ir.models import TestIR
from ..observability import get_logger
from ..orchestrator.events import append_event
from ..orchestrator.finalize import finalize
from ..orchestrator.heartbeat import LeaseKeeper
from ..orchestrator.reservations import release_for_execution
from ..repositories.artifacts import ArtifactRepository
from ..repositories.executions import ExecutionRepository
from ..repositories.reservations import ReservationRepository
from ..repositories.resources import AttachmentRepository
from ..services.object_store import get_object_store, safe_filename
from ..services.secret_store import get_secret_store
from .environment import ExecutionPlan, build_plan

log = get_logger(__name__)

#: §9.6: a browser that fails to start is retried twice with a fixed backoff, then the run errors.
BROWSER_START_ATTEMPTS = 3
BROWSER_START_BACKOFF_SECONDS = (0.0, 1.0, 3.0)

#: Navigation gets its own budget rather than the generic step timeout (§9.5).
NAVIGATION_ACTIONS = frozenset({"open"})


class RunResult:
    """What one run concluded with, ready for `finalize`."""

    __slots__ = ("detail", "error_code", "outcome", "statuses")

    def __init__(self, outcome: str, error_code: str | None, detail: dict[str, Any], statuses: list[Any]) -> None:
        self.outcome = outcome
        self.error_code = error_code
        self.detail = detail
        self.statuses = statuses


#: Runs this process is holding right now; read by the worker announcement (§9.3).
_active_runs = 0
_active_lock = threading.Lock()


def active_run_count() -> int:
    with _active_lock:
        return _active_runs


def default_worker_id() -> str:
    """One identity per process: the lease holder, the human-session owner and the announce row agree."""
    return f"{socket.gethostname()[:40]}-{os.getpid()}"


def _enter_run() -> None:
    global _active_runs
    with _active_lock:
        _active_runs += 1


def _leave_run() -> None:
    global _active_runs
    with _active_lock:
        _active_runs = max(0, _active_runs - 1)


class ExecutionWorker:
    def __init__(
        self, *, settings: Settings | None = None, executor: Any | None = None, worker_id: str | None = None
    ) -> None:
        self.settings = settings or get_settings()
        self.executor = executor or PlaywrightExecutor(self.settings)
        self.worker_id = worker_id or default_worker_id()
        self.db = get_database()

    def active_runs(self) -> int:
        return active_run_count()

    # ------------------------------------------------------------------ entry

    def run(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Synchronous entry point: both Celery and the in-process queue call this."""
        return asyncio.run(self._run(payload))

    async def _run(self, payload: dict[str, Any]) -> dict[str, Any]:
        execution_id = str(payload["execution_id"])
        claimed = self._claim(payload)
        if claimed is None:
            return {"execution_id": execution_id, "acknowledged": True, "skipped": True}
        execution, epoch, tenant_id = claimed
        keeper: LeaseKeeper | None = None
        hard_deadline = monotonic_ms() + self.settings.task_hard_limit_seconds * 1000
        started_monotonic = monotonic_ms()
        session: Any = None
        browser_started = False
        closed_clean = True
        _enter_run()
        try:
            context = self._new_context(execution, epoch, tenant_id)
            context.hard_deadline_ms = hard_deadline
            keeper = LeaseKeeper(
                execution_id=execution_id,
                tenant_id=tenant_id,
                epoch=epoch,
                context=context,
                worker_id=self.worker_id,
                settings=self.settings,
            ).start()
            plan = self._plan(execution, tenant_id, context)
            context.evidence = self._sink(execution, plan, tenant_id)
            session = await self._start_session(plan.session_config, context)
            if session is None:
                self._finalize(
                    execution,
                    tenant_id,
                    outcome=Outcome.ERROR.value,
                    error_code=ErrorCode.BROWSER_START_FAILED.value,
                    error_detail={
                        "attempts": BROWSER_START_ATTEMPTS,
                        "last_error": context.interruption.detail.get("browser_start_error"),
                    },
                    artifact_status=ArtifactStatus.PARTIAL.value,
                )
                return {
                    "execution_id": execution_id,
                    "outcome": Outcome.ERROR.value,
                    "error_code": ErrorCode.BROWSER_START_FAILED.value,
                }
            browser_started = True
            self._materialise_attachments(execution, session, context)
            self._record_browser_version(execution, epoch, session)
            result = await self._run_steps(execution, session, context, plan, hard_deadline=hard_deadline)
            bundle = await self._close(session, context)
            closed_clean = bundle is not None
            self._finalize(
                execution,
                tenant_id,
                outcome=result.outcome,
                error_code=result.error_code,
                error_detail=result.detail,
                artifact_status=self._artifact_status(execution, tenant_id, bundle),
                extra={"active_ms": max(0, int(monotonic_ms() - started_monotonic) - context.human_waited_ms)},
            )
            return {"execution_id": execution_id, "outcome": result.outcome, "error_code": result.error_code}
        except HumanGateError as failure:
            # §10.3/§10.4: the pause itself decided the conclusion, so honour it without re-running.
            bundle = await self._close(session, context) if session is not None else None
            closed_clean = (not browser_started) or bundle is not None
            self._finalize(
                execution,
                tenant_id,
                outcome=failure.outcome,
                error_code=failure.error_code,
                error_detail=failure.detail,
                artifact_status=self._artifact_status(execution, tenant_id, bundle),
            )
            return {"execution_id": execution_id, "outcome": failure.outcome, "error_code": failure.error_code}
        except ApiError as failure:
            bundle = await self._close(session, context) if session is not None else None
            closed_clean = (not browser_started) or bundle is not None
            self._finalize(
                execution,
                tenant_id,
                outcome=Outcome.ERROR.value,
                error_code=failure.code.value,
                error_detail=failure.details,
                artifact_status=self._artifact_status(execution, tenant_id, bundle),
            )
            return {"execution_id": execution_id, "outcome": Outcome.ERROR.value, "error_code": failure.code.value}
        except Exception as exc:
            log.exception(
                "execution worker failed", extra={"context": {"execution_id": execution_id, "error": str(exc)}}
            )
            bundle = await self._close(session, context) if session is not None else None
            closed_clean = (not browser_started) or bundle is not None
            self._finalize(
                execution,
                tenant_id,
                outcome=Outcome.ERROR.value,
                error_code=ErrorCode.BROWSER_CRASHED.value,
                error_detail={"error": f"{type(exc).__name__}: {str(exc)[:500]}"},
                artifact_status=self._artifact_status(execution, tenant_id, bundle),
            )
            return {
                "execution_id": execution_id,
                "outcome": Outcome.ERROR.value,
                "error_code": ErrorCode.BROWSER_CRASHED.value,
            }
        finally:
            if keeper is not None:
                keeper.stop()
            self._release(execution, tenant_id, cleanup_confirmed=closed_clean)
            _leave_run()

    # ------------------------------------------------------------------ claiming

    def _claim(self, payload: dict[str, Any]) -> tuple[TestExecution, int, str] | None:
        """Reservation check plus lease takeover in one transaction (§9.3, §9.4)."""
        execution_id = str(payload["execution_id"])
        tenant_id = str(payload.get("tenant_id") or "")
        reservation_id = str(payload.get("reservation_id") or "")
        generation = int(payload.get("generation") or 1)
        with self.db.session(tenant_id or None) as session:
            repo = ExecutionRepository(session, tenant_id)
            execution = repo.by_id(execution_id)
            if execution is None:
                log.warning("execution vanished before it ran", extra={"context": {"execution_id": execution_id}})
                return None
            tenant_id = execution.tenant_id
            repo = ExecutionRepository(session, tenant_id)
            if execution.status in (
                ExecutionStatus.RUNNING.value,
                ExecutionStatus.WAIT_HUMAN.value,
                ExecutionStatus.FINISHED.value,
            ):
                # A duplicate delivery never starts a second browser for a run that already has one.
                log.info(
                    "duplicate execute message acknowledged",
                    extra={"context": {"execution_id": execution_id, "status": execution.status}},
                )
                return None
            reservations = ReservationRepository(session, tenant_id)
            if reservation_id:
                reservation = reservations.by_id(reservation_id, for_update=True)
                if (
                    reservation is None
                    or reservation.generation != generation
                    or reservation.status not in ("RESERVED", "ACTIVE")
                ):
                    log.info(
                        "stale reservation message dropped",
                        extra={"context": {"execution_id": execution_id, "generation": generation}},
                    )
                    return None
            if execution.status != ExecutionStatus.QUEUED.value:
                # Cancelled or finalising before it ever started: close it out and free the slot.
                finalize(
                    session,
                    execution,
                    outcome=execution.outcome or Outcome.CANCELLED.value,
                    error_code=execution.error_code or ErrorCode.CANCELLED.value,
                    artifact_status=ArtifactStatus.COMPLETE.value,
                )
                release_for_execution(session, execution_id, cleanup_confirmed=True, reason="never started")
                session.commit()
                return None
            try:
                locked, epoch = repo.mark_running(
                    execution_id,
                    worker_id=self.worker_id,
                    expected_state_version=int(payload.get("state_version") or execution.state_version),
                    generation=generation,
                )
            except ApiError as failure:
                log.info(
                    "execution claim refused",
                    extra={"context": {"execution_id": execution_id, "code": failure.code.value}},
                )
                return None
            if reservation_id:
                reservations.activate(reservation_id, generation=generation)
            session.commit()
            return locked, epoch, tenant_id

    def _release(self, execution: TestExecution, tenant_id: str, *, cleanup_confirmed: bool) -> None:
        with self.db.session(tenant_id) as session:
            release_for_execution(session, execution.id, cleanup_confirmed=cleanup_confirmed)
            session.commit()

    # ------------------------------------------------------------- context build

    def _new_context(self, execution: TestExecution, epoch: int, tenant_id: str) -> RunContext:
        context = RunContext(
            execution_id=execution.id,
            tenant_id=tenant_id,
            project_id=execution.project_id,
            environment_id=execution.environment_id or "",
            environment_revision_id=execution.environment_revision_id or "",
            lease_epoch=epoch,
            settings=self.settings,
        )
        context.dispatch_hook = self._dispatch_hook(tenant_id, execution.id)
        return context

    def _dispatch_hook(self, tenant_id: str, execution_id: str):
        """The §9.4 ladder: an intent and its acknowledgement are separate durable writes."""

        def hook(step_id: str, state: str) -> None:
            try:
                with self.db.session(tenant_id) as session:
                    ExecutionRepository(session, tenant_id).set_dispatch_state(execution_id, step_id, state=state)
                    session.commit()
            except Exception as exc:
                log.exception(
                    "dispatch state lost",
                    extra={"context": {"execution_id": execution_id, "step_id": step_id, "error": str(exc)}},
                )

        return hook

    def _plan(self, execution: TestExecution, tenant_id: str, context: RunContext) -> ExecutionPlan:
        snapshot = dict(execution.snapshot or {})
        store = get_secret_store(self.settings)

        def reveal(logical_name: str, version: int | None) -> str:
            with self.db.session(tenant_id) as session:
                return store.reveal(
                    session,
                    tenant_id=tenant_id,
                    project_id=execution.project_id,
                    logical_name=logical_name,
                    version=version,
                )

        plan = build_plan(
            settings=self.settings,
            config=snapshot.get("environment_config") or {},
            secret_bindings=snapshot.get("secret_bindings") or {},
            ir=execution.ir or {},
            requested_browser=execution.browser,
            run_variables=snapshot.get("run_variables") or {},
            evidence_mode=execution.evidence_mode or snapshot.get("evidence_mode"),
            allow_vision=bool((snapshot.get("project_settings") or {}).get("allow_vision")),
            reveal_secret=reveal,
        )
        context.values = plan.values
        context.variables = plan.variables
        context.origin = plan.origin
        context.route_pattern = plan.route_pattern
        context.evidence_mode = plan.evidence_mode
        context.memory = self._memory(execution, plan)
        context.vision = self._vision(plan)
        return plan

    def _memory(self, execution: TestExecution, plan: ExecutionPlan):
        if not execution.environment_id:
            return NoElementMemory()
        return DatabaseElementMemory(
            tenant_id=execution.tenant_id,
            project_id=execution.project_id,
            environment_id=execution.environment_id,
            browser_family=plan.browser_family,
        )

    def _vision(self, plan: ExecutionPlan):
        if not plan.session_config.allow_vision or not self.settings.ai_vision_enabled:
            return None
        from ..ai.adapter import AiAdapter

        return AiVisionResolver(self.settings, adapter=AiAdapter(self.settings, purpose="vision"))

    def _sink(self, execution: TestExecution, plan: ExecutionPlan, tenant_id: str) -> DatabaseEvidenceSink:
        return DatabaseEvidenceSink(
            tenant_id=tenant_id,
            project_id=execution.project_id,
            execution_id=execution.id,
            settings=self.settings,
            store=get_object_store(self.settings),
            evidence_mode=plan.evidence_mode,
            known_secrets=plan.known_secrets,
            event_hook=lambda event_type, payload: append_event(tenant_id, execution.id, event_type, payload),
        )

    # ------------------------------------------------------------- browser setup

    async def _start_session(self, config: SessionConfig, context: RunContext) -> Any:
        last_error: str | None = None
        for attempt, backoff in enumerate(BROWSER_START_BACKOFF_SECONDS[:BROWSER_START_ATTEMPTS]):
            if backoff:
                await asyncio.sleep(backoff)
            try:
                return await self.executor.create_session(config, context)
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {str(exc)[:300]}"
                log.warning(
                    "browser start failed",
                    extra={
                        "context": {"execution_id": context.execution_id, "attempt": attempt + 1, "error": last_error}
                    },
                )
        context.interruption.detail["browser_start_error"] = last_error
        return None

    def _materialise_attachments(self, execution: TestExecution, session: Any, context: RunContext) -> None:
        """Scanned attachments are copied into this execution's scratch directory (§14.3)."""
        wanted: list[str] = []
        for step in (execution.ir or {}).get("steps") or []:
            if isinstance(step, dict) and step.get("action") == "upload":
                wanted.extend(str(item) for item in step.get("attachment_ids") or [])
        if not wanted:
            return
        with self.db.session(execution.tenant_id) as scope:
            rows = AttachmentRepository(scope, execution.tenant_id).by_ids(wanted)
        store = get_object_store(self.settings)
        for attachment_id in wanted:
            row = rows.get(attachment_id)
            if row is None or row.scan_status != "CLEAN":
                continue
            try:
                payload = store.read_bytes(row.object_key, max_bytes=self.settings.max_attachment_bytes)
            except Exception as exc:
                log.warning(
                    "attachment could not be staged",
                    extra={"context": {"attachment_id": attachment_id, "error": str(exc)}},
                )
                continue
            target = session.scratch_dir / safe_filename(row.filename)
            target.write_bytes(payload)
            context.attachments[attachment_id] = str(target)

    def _record_browser_version(self, execution: TestExecution, epoch: int, session: Any) -> None:
        with self.db.session(execution.tenant_id) as scope:
            ExecutionRepository(scope, execution.tenant_id).countersigned_update(
                execution.id,
                epoch=epoch,
                values={"browser_version": str(getattr(session, "browser_version", "") or "")[:60]},
            )

    # --------------------------------------------------------------- step loop

    async def _run_steps(
        self,
        execution: TestExecution,
        session: Any,
        context: RunContext,
        plan: ExecutionPlan,
        *,
        hard_deadline: float,
    ) -> RunResult:
        ir = TestIR.model_validate(execution.ir)
        active_deadline = monotonic_ms() + self.settings.active_timeout_seconds * 1000
        gate = self._gate(execution, session, context)
        if gate is not None:
            context.human_hook = gate.hook

        statuses: list[StepStatus] = []
        for row in self._step_rows(execution):
            step = ir.step_by_id(row.step_id)
            if step is None:
                self._finish_step(
                    execution,
                    row.step_id,
                    status=StepStatus.ERROR.value,
                    error_code=ErrorCode.IR_VALIDATION_FAILED.value,
                )
                return RunResult(
                    Outcome.ERROR.value, ErrorCode.IR_VALIDATION_FAILED.value, {"missing_step": row.step_id}, statuses
                )
            if context.interruption.cancel:
                break
            if context.interruption.lease_lost:
                raise LeaseGone("the lease was revoked before this step")
            if monotonic_ms() >= hard_deadline:
                return RunResult(
                    Outcome.TIMED_OUT.value, ErrorCode.ACTIVE_TIMEOUT.value, {"budget": "task_hard_limit"}, statuses
                )
            if monotonic_ms() >= active_deadline:
                return RunResult(
                    Outcome.TIMED_OUT.value, ErrorCode.ACTIVE_TIMEOUT.value, {"budget": "active_timeout"}, statuses
                )

            self._start_step(execution, row.step_id, epoch=int(context.lease_epoch), step=step)
            deadline = min(active_deadline, hard_deadline, monotonic_ms() + self._step_budget_ms(step, plan))
            waited_before = context.human_waited_ms
            result = await self.executor.execute_step(session, step, context, deadline_ms=deadline)
            completed_by_human = result.status == StepStatus.WAIT_HUMAN.value
            if gate is not None and result.ok:
                # `mode=on_challenge` can only be recognised once the action has run (§10.3).
                try:
                    await gate.after_step(step, result)
                except HumanRequired:
                    completed_by_human = True
            active_deadline += context.human_waited_ms - waited_before
            await self._persist_step(execution, step, result, passed_by_human=completed_by_human)
            statuses.append(StepStatus.PASSED if completed_by_human else StepStatus(result.status))
            if completed_by_human:
                continue
            if result.status in (StepStatus.FAILED.value, StepStatus.ERROR.value, StepStatus.CANCELLED.value):
                return self._conclude(result, statuses)
        if context.interruption.cancel:
            return RunResult(
                Outcome.CANCELLED.value, ErrorCode.CANCELLED.value, {"cancelled_during_run": True}, statuses
            )
        return RunResult(Outcome.PASSED.value, None, {}, statuses)

    def _conclude(self, result: Any, statuses: list[StepStatus]) -> RunResult:
        """First failing step wins; the remaining steps are skipped by `finalize` (§9.1)."""
        outcome = derive_outcome(
            statuses,
            cancelled=result.status == StepStatus.CANCELLED.value,
            timed_out=result.failure_kind == "timeout",
            error_code=None,
        )
        error_code = KIND_TO_ERROR.get(
            str(result.failure_kind),
            ErrorCode.ASSERTION_FAILED.value
            if result.status == StepStatus.FAILED.value
            else ErrorCode.BROWSER_CRASHED.value,
        )
        detail = {
            "step_id": result.step_id,
            "failure_kind": result.failure_kind,
            "message": (result.message or "")[:500],
        }
        return RunResult(str(outcome), error_code, detail, statuses)

    def _step_budget_ms(self, step: Any, plan: ExecutionPlan) -> int:
        """Explicit step timeouts win, navigation gets its own budget, nothing exceeds the cap (§9.5)."""
        cap = int(self.settings.step_timeout_max_ms)
        if step.timeout_ms:
            return min(int(step.timeout_ms), cap)
        if step.action in NAVIGATION_ACTIONS:
            return min(int(plan.navigation_timeout_ms or self.settings.navigation_timeout_ms), cap)
        return min(int(self.settings.step_timeout_ms), cap)

    def _gate(self, execution: TestExecution, session: Any, context: RunContext) -> Any:
        """The gate is always wired: an operator pause (§13.2) has to be honourable even for a case
        that never asks for help, so the only question left to the hook is whether a reason exists."""
        return HumanGate(
            execution_id=execution.id,
            tenant_id=execution.tenant_id,
            project_id=execution.project_id,
            worker_id=self.worker_id,
            session=session,
            context=context,
            executor=self.executor,
            settings=self.settings,
        )

    # ------------------------------------------------------------ step records

    def _step_rows(self, execution: TestExecution) -> list[Any]:
        with self.db.session(execution.tenant_id) as session:
            return ExecutionRepository(session, execution.tenant_id).steps(execution.id)

    def _start_step(self, execution: TestExecution, step_id: str, *, epoch: int, step: Any) -> None:
        with self.db.session(execution.tenant_id) as session:
            ExecutionRepository(session, execution.tenant_id).start_step(
                execution.id, step_id, epoch=epoch, event_payload={"action": step.action}
            )
            session.commit()

    def _finish_step(
        self,
        execution: TestExecution,
        step_id: str,
        *,
        status: str,
        error_code: str | None = None,
        error_detail: dict[str, Any] | None = None,
        locator_strategy: str | None = None,
        locator_attempts: list[dict[str, Any]] | None = None,
        artifact_ids: list[str] | None = None,
        duration_ms: int | None = None,
        extra_event: dict[str, Any] | None = None,
    ) -> None:
        with self.db.session(execution.tenant_id) as session:
            ExecutionRepository(session, execution.tenant_id).finish_step(
                execution.id,
                step_id,
                status=status,
                error_code=error_code,
                error_detail=error_detail,
                locator_strategy=locator_strategy,
                locator_attempts=locator_attempts,
                artifact_ids=artifact_ids,
                duration_ms=duration_ms,
                extra_event=extra_event,
            )
            session.commit()

    async def _persist_step(
        self,
        execution: TestExecution,
        step: Any,
        result: Any,
        *,
        passed_by_human: bool = False,
    ) -> None:
        """Step outcome, locator audit and artifact index land together (§12.1, §8.1).

        Element memory is not written here: the locator engine credited the winning candidate when
        the step settled, and a second credit would inflate every working selector's success rate.
        """
        resolution = result.locator
        attempts = resolution.attempt_payload() if resolution else list(result.detail.get("locator_attempts") or [])
        detail = dict(result.detail or {})
        if attempts:
            detail.setdefault("locator_attempts", attempts)
        self._finish_step(
            execution,
            result.step_id,
            status=StepStatus.PASSED.value if passed_by_human else result.status,
            error_code=None
            if passed_by_human
            else (KIND_TO_ERROR.get(str(result.failure_kind)) if result.failure_kind else None),
            error_detail=None if passed_by_human else detail,
            locator_strategy=resolution.strategy if resolution else None,
            locator_attempts=attempts,
            artifact_ids=list(result.artifact_ids),
            duration_ms=int(result.duration_ms),
            extra_event={
                "action": step.action,
                "locator_source": resolution.source if resolution else None,
                **({"human_mode": detail.get("human_mode")} if passed_by_human and detail.get("human_mode") else {}),
            },
        )

    # --------------------------------------------------------------- teardown

    async def _close(self, session: Any, context: RunContext) -> Any:
        if session is None:
            return None
        try:
            bundle = await self.executor.close_session(session, context)
            if getattr(bundle, "errors", None):
                context.interruption.detail.setdefault("capture_errors", list(bundle.errors))
            return bundle
        except Exception as exc:
            log.exception(
                "session close failed", extra={"context": {"execution_id": context.execution_id, "error": str(exc)}}
            )
            context.interruption.detail["close_error"] = str(exc)[:300]
            return None

    def _artifact_status(self, execution: TestExecution, tenant_id: str, bundle: Any) -> str:
        """§12.1: auxiliary capture gaps mark PARTIAL, they never change the test conclusion."""
        if bundle is None:
            return ArtifactStatus.PARTIAL.value
        try:
            with self.db.session(tenant_id) as session:
                summary = ArtifactRepository(session, tenant_id).summary(execution.id)
        except Exception:
            return ArtifactStatus.PARTIAL.value
        if summary.get("missing") or bundle.partial:
            return ArtifactStatus.PARTIAL.value
        return ArtifactStatus.COMPLETE.value

    def _finalize(
        self,
        execution: TestExecution,
        tenant_id: str,
        *,
        outcome: str,
        error_code: str | None,
        error_detail: dict[str, Any] | None,
        artifact_status: str,
        extra: dict[str, Any] | None = None,
    ) -> None:
        with self.db.session(tenant_id) as session:
            locked = ExecutionRepository(session, tenant_id).load(execution.id)
            finalize(
                session,
                locked,
                outcome=outcome,
                error_code=error_code,
                error_detail=error_detail,
                artifact_status=artifact_status,
                extra=extra,
            )
            session.commit()


#: One worker per process: it owns the executor singleton and therefore a stable worker identity.
_worker: ExecutionWorker | None = None


def default_worker() -> ExecutionWorker:
    """The process's execution worker, rebound if the process-wide database was replaced."""
    global _worker
    if _worker is None or _worker.db is not get_database():
        _worker = ExecutionWorker()
    return _worker


def execute_task(payload: dict[str, Any]) -> dict[str, Any]:
    """Queue handler registered for `execution.execute` (§9.3)."""
    return default_worker().run(payload)
