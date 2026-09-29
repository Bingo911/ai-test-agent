"""Human assistance tasks and durable control commands (§10.3, §14.2)."""

from __future__ import annotations

import secrets as _secrets
from datetime import timedelta
from typing import Any

from sqlalchemy import or_, select, update

from ..db.base import coerce_utc, new_id, utcnow
from ..db.models import ExecutionCommand, HumanTask
from ..domain.enums import ACTIVE_HUMAN_STATUSES, CommandStatus, HumanTaskStatus
from ..domain.errors import ApiError, ErrorCode
from .base import Scoped


def new_pause_token() -> str:
    return _secrets.token_urlsafe(24)


class HumanTaskRepository(Scoped[HumanTask]):
    model = HumanTask

    def create(
        self,
        *,
        project_id: str,
        execution_id: str,
        step_id: str,
        reason: str,
        detail: str | None,
        session_epoch: int,
        resume_phase: str | None,
        resume_condition: dict[str, Any] | None,
        timeout_seconds: int,
    ) -> HumanTask:
        active = self.active_for(execution_id)
        if active is not None:
            raise ApiError(
                ErrorCode.CONFLICT,
                "An execution can only hold one open human task",
                details={"human_task_id": active.id, "status": active.status},
            )
        task = HumanTask(
            id=new_id(),
            tenant_id=self.tenant_id,
            project_id=project_id,
            execution_id=execution_id,
            step_id=step_id,
            reason=reason,
            detail=detail,
            status=HumanTaskStatus.PENDING.value,
            deadline=utcnow() + timedelta(seconds=timeout_seconds),
            session_epoch=session_epoch,
            pause_token=new_pause_token(),
            resume_condition=resume_condition,
            resume_phase=resume_phase,
        )
        self.session.add(task)
        self.session.flush()
        return task

    def active_for(self, execution_id: str) -> HumanTask | None:
        return self.session.scalar(
            select(HumanTask)
            .where(
                HumanTask.tenant_id == self.tenant_id,
                HumanTask.execution_id == execution_id,
                HumanTask.status.in_(list(ACTIVE_HUMAN_STATUSES)),
            )
            .order_by(HumanTask.created_at.desc())
            .limit(1)
        )

    def for_execution(self, execution_id: str) -> list[HumanTask]:
        """Every pause of one run, in order: the report shows the whole human timeline (§12.2)."""
        return list(
            self.session.scalars(
                select(HumanTask)
                .where(HumanTask.tenant_id == self.tenant_id, HumanTask.execution_id == execution_id)
                .order_by(HumanTask.created_at.asc())
            ).all()
        )

    def open(self, *, project_id: str | None = None, limit: int = 50) -> list[HumanTask]:
        conditions = [
            HumanTask.tenant_id == self.tenant_id,
            HumanTask.status.in_(list(ACTIVE_HUMAN_STATUSES)),
        ]
        if project_id:
            conditions.append(HumanTask.project_id == project_id)
        return list(
            self.session.scalars(
                select(HumanTask).where(*conditions).order_by(HumanTask.created_at.asc()).limit(limit)
            ).all()
        )

    def claim(self, task_id: str, *, actor_id: str, control_ttl_seconds: int) -> HumanTask:
        """Single controller per task: the claim is a CAS on status + assignee (§14.2)."""
        locked = self.by_id(task_id, for_update=True)
        if locked is None:
            raise ApiError(ErrorCode.NOT_FOUND, "Human task not found in this tenant")
        if locked.status == HumanTaskStatus.CLAIMED.value and locked.assignee_id == actor_id:
            locked.control_lease_until = utcnow() + timedelta(seconds=control_ttl_seconds)
            self.session.flush()
            return locked
        if locked.status not in (HumanTaskStatus.PENDING.value, HumanTaskStatus.CLAIMED.value):
            raise ApiError(ErrorCode.CONFLICT, f"Human task is {locked.status}")
        control_live = (
            locked.status == HumanTaskStatus.CLAIMED.value
            and locked.control_lease_until is not None
            and coerce_utc(locked.control_lease_until) > utcnow()
            and locked.assignee_id != actor_id
        )
        if control_live:
            raise ApiError(ErrorCode.HUMAN_TASK_TAKEN, "Another operator already holds the control lease")
        result = self.session.execute(
            update(HumanTask)
            .where(
                HumanTask.tenant_id == self.tenant_id,
                HumanTask.id == task_id,
                HumanTask.status == locked.status,
            )
            .values(
                status=HumanTaskStatus.CLAIMED.value,
                assignee_id=actor_id,
                control_lease_until=utcnow() + timedelta(seconds=control_ttl_seconds),
            )
        )
        if result.rowcount != 1:
            raise ApiError(ErrorCode.HUMAN_TASK_TAKEN, "The task was claimed by someone else")
        locked.status = HumanTaskStatus.CLAIMED.value
        locked.assignee_id = actor_id
        locked.control_lease_until = utcnow() + timedelta(seconds=control_ttl_seconds)
        self.session.flush()
        return locked

    def hold_control_lease(self, task_id: str, *, actor_id: str, ttl_seconds: int) -> bool:
        result = self.session.execute(
            update(HumanTask)
            .where(
                HumanTask.tenant_id == self.tenant_id,
                HumanTask.id == task_id,
                HumanTask.assignee_id == actor_id,
                HumanTask.status == HumanTaskStatus.CLAIMED.value,
            )
            .values(control_lease_until=utcnow() + timedelta(seconds=ttl_seconds))
        )
        self.session.commit()
        return result.rowcount == 1

    def release_control(self, task_id: str, *, actor_id: str) -> HumanTask | None:
        """Disconnect keeps the task open for re-claim without extending the deadline (§10.3)."""
        locked = self.by_id(task_id, for_update=True)
        if locked is None or locked.assignee_id != actor_id:
            return None
        locked.control_lease_until = None
        self.session.flush()
        return locked

    def request_resume(self, task_id: str, *, actor_id: str) -> HumanTask:
        locked = self.by_id(task_id, for_update=True)
        if locked is None:
            raise ApiError(ErrorCode.NOT_FOUND, "Human task not found in this tenant")
        if locked.status != HumanTaskStatus.CLAIMED.value or locked.assignee_id != actor_id:
            raise ApiError(ErrorCode.FORBIDDEN, "Only the current controller may request a resume")
        if coerce_utc(locked.deadline) <= utcnow():
            raise ApiError(ErrorCode.HUMAN_WAIT_TIMEOUT, "The human task deadline already passed")
        locked.status = HumanTaskStatus.RESUME_REQUESTED.value
        locked.resume_requested_at = utcnow()
        self.session.flush()
        return locked

    def return_to_claimed(self, task_id: str, *, note: str) -> HumanTask | None:
        """A rejected resume keeps the operator in control: the deadline is not extended (§10.3)."""
        locked = self.by_id(task_id, for_update=True)
        if locked is None or locked.status != HumanTaskStatus.RESUME_REQUESTED.value:
            return locked
        locked.status = HumanTaskStatus.CLAIMED.value
        locked.resume_requested_at = None
        locked.outcome_note = note
        self.session.flush()
        return locked

    def finish(self, task_id: str, *, status: str, note: str | None = None) -> HumanTask | None:
        locked = self.by_id(task_id, for_update=True)
        if locked is None or locked.status in (
            HumanTaskStatus.COMPLETED.value,
            HumanTaskStatus.EXPIRED.value,
            HumanTaskStatus.CANCELLED.value,
        ):
            return locked
        locked.status = status
        locked.completed_at = utcnow()
        locked.outcome_note = note
        locked.control_lease_until = None
        self.session.flush()
        return locked

    def expire_overdue(self) -> list[HumanTask]:
        rows = list(
            self.session.scalars(
                select(HumanTask).where(
                    HumanTask.tenant_id == self.tenant_id,
                    HumanTask.status.in_(list(ACTIVE_HUMAN_STATUSES)),
                    HumanTask.deadline <= utcnow(),
                )
            ).all()
        )
        for row in rows:
            row.status = HumanTaskStatus.EXPIRED.value
            row.completed_at = utcnow()
            row.outcome_note = "deadline exceeded"
        self.session.flush()
        return rows

    def pending_resume_requests(self) -> list[HumanTask]:
        return list(
            self.session.scalars(
                select(HumanTask).where(
                    HumanTask.tenant_id == self.tenant_id,
                    HumanTask.status == HumanTaskStatus.RESUME_REQUESTED.value,
                )
            ).all()
        )


class CommandRepository(Scoped[ExecutionCommand]):
    """Manual control goes through a persisted command rather than the execution queue (§9.3)."""

    model = ExecutionCommand

    def enqueue(
        self,
        *,
        project_id: str,
        execution_id: str,
        command_type: str,
        dedupe_key: str,
        requested_by: str | None,
        payload: dict[str, Any] | None = None,
        human_task_id: str | None = None,
        expires_at=None,
    ) -> ExecutionCommand:
        existing = self.session.scalar(
            select(ExecutionCommand).where(
                ExecutionCommand.tenant_id == self.tenant_id,
                ExecutionCommand.execution_id == execution_id,
                ExecutionCommand.dedupe_key == dedupe_key,
            )
        )
        if existing is not None:
            return existing
        command = ExecutionCommand(
            id=new_id(),
            tenant_id=self.tenant_id,
            project_id=project_id,
            execution_id=execution_id,
            human_task_id=human_task_id,
            command_type=command_type,
            dedupe_key=dedupe_key,
            status=CommandStatus.PENDING.value,
            requested_by=requested_by,
            payload=payload or {},
            expires_at=expires_at,
        )
        self.session.add(command)
        self.session.flush()
        return command

    def pending(self, execution_id: str) -> list[ExecutionCommand]:
        return list(
            self.session.scalars(
                select(ExecutionCommand)
                .where(
                    ExecutionCommand.tenant_id == self.tenant_id,
                    ExecutionCommand.execution_id == execution_id,
                    ExecutionCommand.status == CommandStatus.PENDING.value,
                    or_(ExecutionCommand.expires_at.is_(None), ExecutionCommand.expires_at > utcnow()),
                )
                .order_by(ExecutionCommand.created_at.asc())
            ).all()
        )

    def for_task(self, human_task_id: str) -> list[ExecutionCommand]:
        """What the operator actually did during one pause, as recorded outcomes (§12.2)."""
        return list(
            self.session.scalars(
                select(ExecutionCommand)
                .where(
                    ExecutionCommand.tenant_id == self.tenant_id,
                    ExecutionCommand.human_task_id == human_task_id,
                )
                .order_by(ExecutionCommand.created_at.asc())
            ).all()
        )

    def mark(self, command_id: str, *, status: str, result: dict[str, Any] | None = None) -> None:
        locked = self.by_id(command_id, for_update=True)
        if locked is None:
            return
        locked.status = status
        locked.processed_at = utcnow()
        if result is not None:
            locked.result = result
        self.session.flush()

    def expire_stale(self) -> int:
        result = self.session.execute(
            update(ExecutionCommand)
            .where(
                ExecutionCommand.tenant_id == self.tenant_id,
                ExecutionCommand.status == CommandStatus.PENDING.value,
                ExecutionCommand.expires_at.isnot(None),
                ExecutionCommand.expires_at <= utcnow(),
            )
            .values(status=CommandStatus.EXPIRED.value, processed_at=utcnow())
        )
        return int(result.rowcount or 0)
