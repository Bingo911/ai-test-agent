"""Execution and step persistence with the §9 conditional-update discipline.

Status changes are compare-and-set operations: the UPDATE carries the expected `state_version`,
the holder's `lease_epoch` and the allowed source statuses. Zero affected rows means a concurrent
writer (cancel API, reconciler, or a stale worker) already changed the row, and the caller must not
assume it won.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import and_, func, or_, select, update

from ..db.base import coerce_utc, is_past, new_id, utcnow
from ..db.models import ExecutionEvent, StepExecution, TestExecution
from ..domain.enums import (
    ACTIVE_STATUSES,
    DispatchState,
    ExecutionStatus,
    Outcome,
    StepStatus,
)
from ..domain.errors import ApiError, ErrorCode
from ..domain.state_machine import ALLOWED_TRANSITIONS
from .base import Scoped

TERMINAL_STATUSES = {ExecutionStatus.FINISHED.value}

#: §9.4 dispatch ladder, ordered: a step may only move forwards through it.
DISPATCH_LADDER = {
    DispatchState.NOT_STARTED: 0,
    DispatchState.INTENT_RECORDED: 1,
    DispatchState.ACKNOWLEDGED: 2,
}


class ExecutionRepository(Scoped[TestExecution]):
    model = TestExecution

    def create(
        self,
        *,
        project_id: str,
        case_id: str,
        revision_id: str,
        compile_artifact_id: str,
        environment_id: str | None,
        environment_revision_id: str | None,
        ir: dict[str, Any],
        ir_digest: str,
        snapshot: dict[str, Any],
        requested_by: str | None,
        browser: str,
        evidence_mode: str,
        trigger: str = "manual",
        retry_of_execution_id: str | None = None,
    ) -> TestExecution:
        execution = TestExecution(
            id=new_id(),
            tenant_id=self.tenant_id,
            project_id=project_id,
            case_id=case_id,
            revision_id=revision_id,
            compile_artifact_id=compile_artifact_id,
            environment_id=environment_id,
            environment_revision_id=environment_revision_id,
            status=ExecutionStatus.CREATED.value,
            outcome=None,
            snapshot=snapshot,
            ir=ir,
            ir_digest=ir_digest,
            trigger=trigger,
            requested_by=requested_by,
            browser=browser,
            evidence_mode=evidence_mode,
            retry_of_execution_id=retry_of_execution_id,
            lease_epoch=0,
            state_version=1,
            last_event_seq=0,
        )
        self.session.add(execution)
        self.session.flush()
        return execution

    def load(self, execution_id: str, *, for_update: bool = False) -> TestExecution:
        row = self.by_id(execution_id, for_update=for_update)
        if row is None:
            raise ApiError(ErrorCode.NOT_FOUND, f"Execution {execution_id} not found in this tenant")
        return row

    # ------------------------------------------------------------------ events

    def append_event(self, execution: TestExecution, event_type: str, payload: dict[str, Any]) -> int:
        """Events are numbered per execution from `last_event_seq` so SSE can resume without gaps."""
        execution.last_event_seq = int(execution.last_event_seq or 0) + 1
        self.session.add(
            ExecutionEvent(
                id=new_id(),
                tenant_id=self.tenant_id,
                execution_id=execution.id,
                seq=execution.last_event_seq,
                event_type=event_type,
                payload=payload,
                occurred_at=utcnow(),
            )
        )
        self.session.flush()
        return execution.last_event_seq

    def events_after(self, execution_id: str, after_seq: int, limit: int = 200) -> list[ExecutionEvent]:
        return list(
            self.session.scalars(
                select(ExecutionEvent)
                .where(
                    ExecutionEvent.tenant_id == self.tenant_id,
                    ExecutionEvent.execution_id == execution_id,
                    ExecutionEvent.seq > after_seq,
                )
                .order_by(ExecutionEvent.seq.asc())
                .limit(limit)
            ).all()
        )

    def first_event_seq(self, execution_id: str) -> int | None:
        """The oldest journal row still on disk; a cursor below it can never be replayed (§13.4)."""
        value = self.session.scalar(
            select(func.min(ExecutionEvent.seq)).where(
                ExecutionEvent.tenant_id == self.tenant_id, ExecutionEvent.execution_id == execution_id
            )
        )
        return None if value is None else int(value)

    # ------------------------------------------------------------- transitions

    def transition(
        self,
        execution_id: str,
        *,
        to_status: str,
        expected_state_version: int | None = None,
        expected_epoch: int | None = None,
        outcome: str | None = None,
        error_code: str | None = None,
        error_detail: dict[str, Any] | None = None,
        extra: dict[str, Any] | None = None,
        event_payload: dict[str, Any] | None = None,
    ) -> TestExecution:
        """Apply a state-machine transition as one conditional UPDATE plus its event row."""
        locked = self.load(execution_id, for_update=True)
        current = locked.status
        if current in TERMINAL_STATUSES:
            raise ApiError(ErrorCode.CONFLICT, f"Execution {execution_id} is already {current}")
        allowed = {member.value for member in ALLOWED_TRANSITIONS.get(ExecutionStatus(current), frozenset())}
        if to_status not in allowed:
            raise ApiError(
                ErrorCode.CONFLICT,
                f"Illegal transition {current} -> {to_status}",
                details={"allowed": sorted(allowed)},
            )
        if expected_state_version is not None and locked.state_version != expected_state_version:
            raise ApiError(
                ErrorCode.CONFLICT,
                "Execution state changed concurrently",
                details={"current_version": locked.state_version},
            )
        if expected_epoch is not None and locked.lease_epoch != expected_epoch:
            raise ApiError(ErrorCode.LEASE_LOST, "Lease epoch no longer current")

        values: dict[str, Any] = {"status": to_status, "state_version": int(locked.state_version or 1) + 1}
        if outcome is not None:
            values["outcome"] = outcome
        if error_code is not None:
            values["error_code"] = error_code
        if error_detail is not None:
            values["error_detail"] = error_detail
        if extra:
            values.update(extra)
        if to_status == ExecutionStatus.QUEUED.value:
            values.setdefault("queued_at", utcnow())
        if to_status == ExecutionStatus.RUNNING.value and locked.started_at is None:
            values.setdefault("started_at", utcnow())
        if to_status == ExecutionStatus.FINALIZING.value:
            if values.get("outcome") is None and locked.outcome is None:
                raise ApiError(ErrorCode.CONFLICT, f"Entering FINALIZING requires a decided outcome (got {outcome!r})")
            values.setdefault("finalizing_deadline_at", utcnow() + timedelta(seconds=self.finalizing_budget_seconds()))
        if to_status == ExecutionStatus.FINISHED.value:
            values.setdefault("ended_at", utcnow())

        result = self.session.execute(
            update(TestExecution)
            .where(
                TestExecution.tenant_id == self.tenant_id,
                TestExecution.id == execution_id,
                TestExecution.status == current,
                TestExecution.state_version == locked.state_version,
            )
            .values(**values)
        )
        if result.rowcount != 1:
            raise ApiError(ErrorCode.CONFLICT, "Execution state changed concurrently")
        for key, value in values.items():
            setattr(locked, key, value)
        self.session.flush()
        self.append_event(
            locked,
            "execution.status_changed",
            {
                "status": to_status,
                "outcome": locked.outcome,
                "error_code": locked.error_code,
                "state_version": locked.state_version,
                "lease_epoch": locked.lease_epoch,
                **(event_payload or {}),
            },
        )
        return locked

    def finalizing_budget_seconds(self) -> int:
        from ..config import get_settings

        return get_settings().finalization_timeout_seconds

    def mark_running(
        self,
        execution_id: str,
        *,
        worker_id: str,
        expected_state_version: int,
        generation: int,
    ) -> tuple[TestExecution, int]:
        """QUEUED -> RUNNING plus lease takeover (§9.1, §9.4)."""
        locked = self.load(execution_id, for_update=True)
        if locked.status == ExecutionStatus.RUNNING.value:
            # a redelivered message must never start a second browser for the same run
            raise ApiError(ErrorCode.LEASE_LOST, "This execution already has an active holder")
        if locked.status != ExecutionStatus.QUEUED.value:
            raise ApiError(ErrorCode.CONFLICT, f"Execution is {locked.status}, not QUEUED")
        if locked.cancel_requested_at is not None:
            raise ApiError(ErrorCode.CANCELLED, "Cancellation was requested before the worker claimed the run")
        epoch = int(locked.lease_epoch or 0) + 1
        from ..config import get_settings

        lease_until = utcnow() + timedelta(seconds=get_settings().lease_ttl_seconds)
        result = self.session.execute(
            update(TestExecution)
            .where(
                TestExecution.tenant_id == self.tenant_id,
                TestExecution.id == execution_id,
                TestExecution.status == ExecutionStatus.QUEUED.value,
                TestExecution.state_version == expected_state_version,
            )
            .values(
                status=ExecutionStatus.RUNNING.value,
                state_version=int(expected_state_version) + 1,
                owner_worker_id=worker_id,
                lease_epoch=epoch,
                lease_until=lease_until,
                started_at=locked.started_at or utcnow(),
            )
        )
        if result.rowcount != 1:
            raise ApiError(ErrorCode.LEASE_LOST, "Another worker already claimed this execution")
        locked.status = ExecutionStatus.RUNNING.value
        locked.state_version = int(expected_state_version) + 1
        locked.owner_worker_id = worker_id
        locked.lease_epoch = epoch
        locked.lease_until = lease_until
        self.session.flush()
        self.append_event(
            locked,
            "execution.status_changed",
            {
                "status": "RUNNING",
                "worker": worker_id,
                "lease_epoch": epoch,
                "reservation_generation": generation,
                "outcome": None,
                "state_version": locked.state_version,
            },
        )
        return locked, epoch

    def heartbeat(self, execution_id: str, *, epoch: int) -> bool:
        from ..config import get_settings

        new_until = utcnow() + timedelta(seconds=get_settings().lease_ttl_seconds)
        result = self.session.execute(
            update(TestExecution)
            .where(
                TestExecution.tenant_id == self.tenant_id,
                TestExecution.id == execution_id,
                TestExecution.lease_epoch == epoch,
                TestExecution.status.in_(
                    [status.value for status in ACTIVE_STATUSES if status != ExecutionStatus.CREATED]
                ),
            )
            .values(lease_until=new_until)
        )
        self.session.commit()
        return result.rowcount == 1

    def epoch_is_current(self, execution_id: str, *, epoch: int) -> bool:
        """Whether `epoch` is still the live holder: same lease, not terminal, no cancel intent.

        A cancel request is deliberately included: once the API has stamped it, the holder loses
        the right to keep driving the execution and must finalise it as CANCELLED (§9.5).
        """
        row = self.session.execute(
            select(
                TestExecution.lease_epoch,
                TestExecution.status,
                TestExecution.cancel_requested_at,
                TestExecution.lease_until,
            ).where(TestExecution.tenant_id == self.tenant_id, TestExecution.id == execution_id)
        ).first()
        if row is None:
            return False
        current_epoch, status, cancel_requested, lease_until = row
        if int(current_epoch) != int(epoch):
            return False
        if status in TERMINAL_STATUSES:
            return False
        # Still running, still ours, and nobody has asked it to stop.
        return cancel_requested is None and not is_past(lease_until)

    def request_cancel(self, execution_id: str, *, requested_by: str | None, reason: str) -> TestExecution:
        """Cancel only stamps intent; the holder or reconciler decides the terminal outcome (§9.5)."""
        locked = self.load(execution_id, for_update=True)
        if locked.status == ExecutionStatus.FINISHED.value:
            return locked
        if locked.status == ExecutionStatus.CREATED.value or locked.status == ExecutionStatus.QUEUED.value:
            return self.transition(
                execution_id,
                to_status=ExecutionStatus.FINALIZING.value,
                outcome=Outcome.CANCELLED.value,
                error_code=ErrorCode.CANCELLED.value,
                extra={"cancel_requested_at": utcnow(), "owner_worker_id": None},
                event_payload={"requested_by": requested_by, "reason": reason, "cancelled_before_start": True},
            )
        if locked.cancel_requested_at is None:
            locked.cancel_requested_at = utcnow()
            self.session.flush()
            self.append_event(
                locked,
                "execution.cancel_requested",
                {
                    "requested_by": requested_by,
                    "reason": reason,
                    "outcome": None,
                    "state_version": locked.state_version,
                },
            )
        return locked

    # -------------------------------------------------------------------- steps

    def add_steps(self, execution: TestExecution, steps: Sequence[dict[str, Any]]) -> None:
        for index, step in enumerate(steps, start=1):
            self.session.add(
                StepExecution(
                    id=new_id(),
                    tenant_id=self.tenant_id,
                    project_id=execution.project_id,
                    execution_id=execution.id,
                    step_id=step["id"],
                    step_no=index,
                    action=step["action"],
                    description=_step_description(step),
                    status=StepStatus.PENDING.value,
                    dispatch_state=DispatchState.NOT_STARTED.value,
                )
            )
        self.session.flush()

    def step(self, execution_id: str, step_id: str) -> StepExecution | None:
        return self.session.scalar(
            select(StepExecution).where(
                StepExecution.tenant_id == self.tenant_id,
                StepExecution.execution_id == execution_id,
                StepExecution.step_id == step_id,
            )
        )

    def steps(self, execution_id: str) -> list[StepExecution]:
        return list(
            self.session.scalars(
                select(StepExecution)
                .where(StepExecution.tenant_id == self.tenant_id, StepExecution.execution_id == execution_id)
                .order_by(StepExecution.step_no.asc())
            ).all()
        )

    def start_step(
        self,
        execution_id: str,
        step_id: str,
        *,
        epoch: int,
        event_payload: dict[str, Any] | None = None,
    ) -> StepExecution | None:
        step = self.step(execution_id, step_id)
        if step is None:
            return None
        step.status = StepStatus.RUNNING.value
        step.started_at = utcnow()
        step.lease_epoch = epoch
        self.session.flush()
        execution = self.load(execution_id, for_update=True)
        self.append_event(
            execution,
            "step.started",
            {"step_id": step_id, "action": step.action, "step_no": step.step_no, **(event_payload or {})},
        )
        return step

    def set_dispatch_state(
        self,
        execution_id: str,
        step_id: str,
        *,
        state: str,
        detail: dict[str, Any] | None = None,
        for_step: StepExecution | None = None,
    ) -> int:
        """Move a step forward through the dispatch ladder (§9.4).

        A dispatch intent and its acknowledgement land in different transactions, so the update is
        conditional: it only fires from a strictly earlier state, and a late ACK arriving after the
        step already moved on affects zero rows instead of rewinding it.
        """
        target = DispatchState(state)
        earlier = [member.value for member in DISPATCH_LADDER if DISPATCH_LADDER[member] < DISPATCH_LADDER[target]]
        if not earlier:
            return 0
        statement = (
            update(StepExecution)
            .where(
                StepExecution.tenant_id == self.tenant_id,
                StepExecution.execution_id == execution_id,
                StepExecution.step_id == step_id,
                StepExecution.dispatch_state.in_(earlier),
            )
            .values(dispatch_state=target.value)
        )
        if for_step is not None:
            statement = statement.where(StepExecution.dispatch_state == for_step.dispatch_state)
        result = self.session.execute(statement)
        if result.rowcount != 1:
            return 0
        if detail is not None:
            step = self.step(execution_id, step_id)
            merged = dict(step.error_detail or {})
            merged.setdefault("dispatch", {})[detail.get("key", "last")] = {
                k: v for k, v in detail.items() if k != "key"
            }
            step.error_detail = merged
            self.session.flush()
        return result.rowcount

    def finish_step(
        self,
        execution_id: str,
        step_id: str,
        *,
        status: str,
        error_code: str | None = None,
        error_detail: dict[str, Any] | None = None,
        locator_strategy: str | None = None,
        locator_attempts: list[dict[str, Any]] | None = None,
        artifact_ids: list[str] | None = None,
        duration_ms: int | None = None,
        event_type: str = "step.finished",
        extra_event: dict[str, Any] | None = None,
    ) -> StepExecution | None:
        step = self.step(execution_id, step_id)
        if step is None:
            return None
        step.status = status
        step.ended_at = utcnow()
        if duration_ms is not None:
            step.duration_ms = duration_ms
        elif step.started_at is not None:
            started = coerce_utc(step.started_at)
            step.duration_ms = int((step.ended_at - started).total_seconds() * 1000)
        step.error_code = error_code
        if error_detail is not None:
            step.error_detail = error_detail
        if locator_strategy is not None:
            step.locator_strategy = locator_strategy
        if locator_attempts is not None:
            step.locator_attempts = locator_attempts
        if artifact_ids is not None:
            step.artifact_ids = artifact_ids
        self.session.flush()
        execution = self.load(execution_id, for_update=True)
        self.append_event(
            execution,
            event_type,
            {
                "step_id": step_id,
                "status": status,
                "error_code": error_code,
                "locator_strategy": step.locator_strategy,
                "artifact_ids": step.artifact_ids or [],
                "duration_ms": step.duration_ms,
                **(extra_event or {}),
            },
        )
        return step

    def skip_pending_after(
        self, execution_id: str, *, from_step_no: int, status: str = StepStatus.SKIPPED.value
    ) -> list[str]:
        rows = list(
            self.session.scalars(
                select(StepExecution).where(
                    StepExecution.tenant_id == self.tenant_id,
                    StepExecution.execution_id == execution_id,
                    StepExecution.step_no > from_step_no,
                    StepExecution.status == StepStatus.PENDING.value,
                )
            ).all()
        )
        if not rows:
            return []
        execution = self.load(execution_id, for_update=True)
        for row in rows:
            row.status = status
            self.append_event(
                execution,
                "step.skipped",
                {"step_id": row.step_id, "step_no": row.step_no, "action": row.action, "reason": "earlier_step_failed"},
            )
        self.session.flush()
        return [row.step_id for row in rows]

    def update_step_projection(self, execution_id: str, step_id: str, **fields: Any) -> None:
        step = self.step(execution_id, step_id)
        if step is None:
            return
        for key, value in fields.items():
            setattr(step, key, value)
        self.session.flush()

    # ------------------------------------------------------------------- lists

    def list(
        self,
        *,
        project_id: str | None = None,
        case_id: str | None = None,
        environment_id: str | None = None,
        statuses: Sequence[str] = (),
        outcomes: Sequence[str] = (),
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[TestExecution], int]:

        conditions = [TestExecution.tenant_id == self.tenant_id]
        if project_id:
            conditions.append(TestExecution.project_id == project_id)
        if case_id:
            conditions.append(TestExecution.case_id == case_id)
        if environment_id:
            conditions.append(TestExecution.environment_id == environment_id)
        if statuses:
            conditions.append(TestExecution.status.in_(list(statuses)))
        if outcomes:
            conditions.append(TestExecution.outcome.in_(list(outcomes)))
        if since is not None:
            conditions.append(TestExecution.created_at >= since)
        if until is not None:
            conditions.append(TestExecution.created_at <= until)
        total = self.session.scalar(select(func.count()).select_from(TestExecution).where(and_(*conditions))) or 0
        rows = self.session.scalars(
            select(TestExecution)
            .where(and_(*conditions))
            .order_by(TestExecution.created_at.desc())
            .limit(limit)
            .offset(offset)
        ).all()
        return list(rows), int(total)

    def countersigned_update(self, execution_id: str, *, epoch: int, values: dict[str, Any]) -> bool:
        """Best-effort metadata write that refuses to run under a stale lease."""
        result = self.session.execute(
            update(TestExecution)
            .where(
                TestExecution.tenant_id == self.tenant_id,
                TestExecution.id == execution_id,
                TestExecution.lease_epoch == epoch,
                TestExecution.status.notin_(TERMINAL_STATUSES),
            )
            .values(**values)
        )
        self.session.commit()
        return result.rowcount == 1

    def set_analysis_status(self, execution_id: str, *, status: str) -> bool:
        """Analysis lands after the run is FINISHED, so this write is not lease-countersigned (§12.3)."""
        result = self.session.execute(
            update(TestExecution)
            .where(TestExecution.tenant_id == self.tenant_id, TestExecution.id == execution_id)
            .values(analysis_status=status)
        )
        self.session.commit()
        return result.rowcount == 1

    def queue_expired(self, *, now: datetime | None = None) -> list[TestExecution]:
        from ..config import get_settings

        settings = get_settings()
        deadline = (now or utcnow()) - timedelta(seconds=settings.queue_timeout_seconds)
        rows = self.session.scalars(
            select(TestExecution).where(
                TestExecution.tenant_id == self.tenant_id,
                TestExecution.status == ExecutionStatus.QUEUED.value,
                TestExecution.queued_at.isnot(None),
                TestExecution.queued_at < deadline,
            )
        ).all()
        return list(rows)

    def stale_active(self, *, now: datetime | None = None) -> list[TestExecution]:
        cutoff = now or utcnow()
        rows = self.session.scalars(
            select(TestExecution).where(
                TestExecution.tenant_id == self.tenant_id,
                or_(
                    TestExecution.status.in_(
                        [ExecutionStatus.RUNNING.value, ExecutionStatus.WAIT_HUMAN.value, ExecutionStatus.QUEUED.value]
                    ),
                    TestExecution.status == ExecutionStatus.FINALIZING.value,
                ),
                or_(
                    and_(
                        TestExecution.status.in_([ExecutionStatus.RUNNING.value, ExecutionStatus.WAIT_HUMAN.value]),
                        TestExecution.lease_until.isnot(None),
                        TestExecution.lease_until < cutoff,
                    ),
                    and_(
                        TestExecution.status == ExecutionStatus.FINALIZING.value,
                        TestExecution.finalizing_deadline_at.isnot(None),
                        TestExecution.finalizing_deadline_at < cutoff,
                    ),
                    and_(
                        TestExecution.status == ExecutionStatus.QUEUED.value,
                        TestExecution.queued_at.isnot(None),
                        TestExecution.queued_at < cutoff - timedelta(seconds=self._queue_timeout()),
                    ),
                ),
            )
        ).all()
        return list(rows)

    @staticmethod
    def _queue_timeout() -> int:
        from ..config import get_settings

        return get_settings().queue_timeout_seconds


def _step_description(step: dict[str, Any]) -> str | None:
    target = step.get("target")
    if isinstance(target, dict) and target.get("description"):
        return str(target["description"])
    condition = step.get("condition")
    if isinstance(condition, dict):
        condition_target = condition.get("target")
        if isinstance(condition_target, dict) and condition_target.get("description"):
            return str(condition_target["description"])
        expected = condition.get("expected")
        if isinstance(expected, dict) and expected.get("kind") == "literal":
            return f"{condition.get('kind')}: {expected.get('value')}"
        return str(condition.get("kind"))
    return None
