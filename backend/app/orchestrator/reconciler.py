"""Reconciler: the database-side recovery loop (§9.4, §9.6).

It only ever performs idempotent clean-up — revoke a stale lease, close a task, finish a run whose
archive deadline passed — and never executes the remaining test steps. A run whose browser was
already started is conservatively ended as ERROR/SESSION_LOST instead of being handed to a new
worker, because the platform cannot un-send a request that already left for the target site.
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select

from ..config import Settings, get_settings
from ..db.base import get_database, is_past, utcnow
from ..db.models import ExecutionReservation, HumanTask, TestExecution, WorkerPool
from ..domain.enums import (
    ACTIVE_HUMAN_STATUSES,
    CleanupStatus,
    ExecutionStatus,
    Outcome,
    ReservationStatus,
)
from ..domain.errors import ErrorCode
from ..observability import get_logger
from .events import EXECUTION_EXECUTE
from .finalize import finalize
from .reservations import quarantine_for_execution, release_for_execution, release_orphaned_reservations

log = get_logger(__name__)


class Reconciler:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def tick(self) -> dict[str, int]:
        report = {
            "queue_timeout": self.expire_queued(),
            "lease_revoked": self.revoke_stale_leases(),
            "archive_finished": self.finish_overdue_archive(),
            "reservations_requeued": self.requeue_expired_reservations(),
            "reservations_released": release_orphaned_reservations(self.settings),
            "human_expired": self.expire_human_tasks(),
            "pools_recounted": self.recount_pools(),
        }
        if any(report.values()):
            log.info("reconciled", extra={"context": report})
        return report

    # --------------------------------------------------------------- executions

    def expire_queued(self) -> int:
        """QUEUED past `queue_timeout` ends TIMED_OUT; a run must not queue forever (§9.5)."""
        from ..repositories.executions import ExecutionRepository

        deadline = utcnow() - timedelta(seconds=self.settings.queue_timeout_seconds)
        count = 0
        with get_database().session() as session:
            rows = session.execute(
                select(TestExecution.tenant_id, TestExecution.id).where(
                    TestExecution.status == ExecutionStatus.QUEUED.value,
                    TestExecution.queued_at.isnot(None),
                    TestExecution.queued_at < deadline,
                )
            ).all()
            for tenant_id, execution_id in rows:
                repo = ExecutionRepository(session, tenant_id)
                execution = repo.load(execution_id)
                if not is_past(execution.queued_at, now=deadline):
                    continue
                finalize(
                    session,
                    execution,
                    outcome=Outcome.TIMED_OUT.value,
                    error_code=ErrorCode.QUEUE_TIMEOUT.value,
                    error_detail={"queued_at": _iso(execution.queued_at)},
                    artifact_status="PENDING",
                    enqueue_analysis=False,
                )
                # Nothing was ever started for a queued run, so its slot is safe to free now.
                release_for_execution(session, execution_id, cleanup_confirmed=True, reason="never claimed")
                session.commit()
                count += 1
        return count

    def revoke_stale_leases(self) -> int:
        """A lapsed lease loses its write rights before anything else is decided (§9.4)."""
        from ..repositories.executions import ExecutionRepository

        count = 0
        with get_database().session() as session:
            rows = session.execute(
                select(TestExecution.tenant_id, TestExecution.id, TestExecution.lease_epoch).where(
                    TestExecution.status.in_([ExecutionStatus.RUNNING.value, ExecutionStatus.WAIT_HUMAN.value]),
                    TestExecution.lease_until.isnot(None),
                )
            ).all()
            for tenant_id, execution_id, epoch in rows:
                repo = ExecutionRepository(session, tenant_id)
                execution = repo.load(execution_id)
                if not is_past(execution.lease_until):
                    continue
                self._bump_epoch(session, execution_id, int(epoch or 0))
                finalize(
                    session,
                    execution,
                    outcome=Outcome.ERROR.value,
                    error_code=ErrorCode.SESSION_LOST.value,
                    error_detail={"lease_epoch": int(epoch or 0), "worker": execution.owner_worker_id},
                    artifact_status=execution.artifact_status or "PARTIAL",
                )
                # The old browser has not been confirmed dead, so its slot stays withheld.
                quarantine_for_execution(session, execution_id, reason="lease expired while the holder was unconfirmed")
                session.commit()
                count += 1
        return count

    def _bump_epoch(self, session, execution_id: str, epoch: int) -> None:
        from sqlalchemy import update

        session.execute(
            update(TestExecution)
            .where(
                TestExecution.id == execution_id,
                TestExecution.lease_epoch == epoch,
                TestExecution.status.in_([ExecutionStatus.RUNNING.value, ExecutionStatus.WAIT_HUMAN.value]),
            )
            .values(lease_epoch=epoch + 1, state_version=TestExecution.state_version + 1)
        )
        session.flush()

    def finish_overdue_archive(self) -> int:
        """FINALIZING past its deadline closes with whatever evidence exists; gaps become PARTIAL."""
        from ..repositories.executions import ExecutionRepository

        count = 0
        with get_database().session() as session:
            rows = session.execute(
                select(TestExecution.tenant_id, TestExecution.id).where(
                    TestExecution.status == ExecutionStatus.FINALIZING.value,
                    TestExecution.finalizing_deadline_at.isnot(None),
                )
            ).all()
            for tenant_id, execution_id in rows:
                repo = ExecutionRepository(session, tenant_id)
                execution = repo.load(execution_id)
                if not is_past(execution.finalizing_deadline_at):
                    continue
                artifact_status = execution.artifact_status
                if artifact_status not in ("PARTIAL", "FAILED", "COMPLETE"):
                    artifact_status = "PARTIAL"
                finalize(
                    session,
                    execution,
                    outcome=str(execution.outcome or Outcome.ERROR.value),
                    artifact_status=artifact_status,
                )
                release_for_execution(
                    session,
                    execution_id,
                    cleanup_confirmed=execution.cleanup_status != CleanupStatus.QUARANTINED.value,
                )
                session.commit()
                count += 1
        return count

    # -------------------------------------------------------------- reservations

    def requeue_expired_reservations(self) -> int:
        """Only an *unclaimed* reservation may be recycled; an ACTIVE one belongs to its lease (§9.3)."""
        from ..repositories.executions import ExecutionRepository
        from ..repositories.outbox import OutboxRepository
        from ..repositories.reservations import PoolRepository, ReservationRepository

        count = 0
        with get_database().session() as session:
            pairs = [
                (row[0], row[1])
                for row in session.execute(
                    select(ExecutionReservation.tenant_id, ExecutionReservation.id).where(
                        ExecutionReservation.status == ReservationStatus.RESERVED.value,
                        ExecutionReservation.expires_at.isnot(None),
                    )
                ).all()
            ]
            for tenant_id, reservation_id in pairs:
                reservations = ReservationRepository(session, tenant_id)
                row = reservations.by_id(reservation_id, for_update=True)
                if row is None or row.status != ReservationStatus.RESERVED.value or not is_past(row.expires_at):
                    continue
                execution = ExecutionRepository(session, row.tenant_id).load(row.execution_id)
                if execution.status != ExecutionStatus.QUEUED.value or execution.cancel_requested_at is not None:
                    release_for_execution(
                        session, row.execution_id, cleanup_confirmed=True, reason="no longer schedulable"
                    )
                    continue
                replacement = reservations.requeue(row.id, ttl_seconds=self.settings.reservation_ttl_seconds)
                if replacement is None:
                    continue
                OutboxRepository(session, row.tenant_id).enqueue(
                    aggregate_id=row.execution_id,
                    event_type=EXECUTION_EXECUTE,
                    payload={
                        "execution_id": row.execution_id,
                        "reservation_id": replacement.id,
                        "generation": replacement.generation,
                        "tenant_id": row.tenant_id,
                        "project_id": row.project_id,
                        "state_version": execution.state_version,
                    },
                    discriminator=f"{EXECUTION_EXECUTE}:{replacement.id}:{replacement.generation}",
                )
                PoolRepository(session, row.tenant_id).recount(row.pool_id)
                count += 1
            session.commit()
        return count

    # -------------------------------------------------------------------- human

    def expire_human_tasks(self) -> int:
        """A human task past its deadline ends the run TIMED_OUT/HUMAN_WAIT_TIMEOUT (§10.3 step 6)."""
        from ..repositories.executions import ExecutionRepository
        from ..repositories.human import HumanTaskRepository

        count = 0
        with get_database().session() as session:
            pairs = [
                (row[0], row[1])
                for row in session.execute(
                    select(HumanTask.tenant_id, HumanTask.id).where(
                        HumanTask.status.in_(list(ACTIVE_HUMAN_STATUSES)), HumanTask.deadline.isnot(None)
                    )
                ).all()
            ]
            for tenant_id, task_id in pairs:
                tasks = HumanTaskRepository(session, tenant_id)
                task = tasks.by_id(task_id)
                if task is None or task.status not in ACTIVE_HUMAN_STATUSES or not is_past(task.deadline):
                    continue
                tasks.finish(task.id, status="EXPIRED", note="nobody claimed it in time")
                execution = ExecutionRepository(session, task.tenant_id).load(task.execution_id)
                finalize(
                    session,
                    execution,
                    outcome=Outcome.TIMED_OUT.value,
                    error_code=ErrorCode.HUMAN_WAIT_TIMEOUT.value,
                    error_detail={"human_task_id": task.id, "reason": task.reason},
                    artifact_status=execution.artifact_status or "PARTIAL",
                    close_human_tasks=False,
                )
                # The worker that owned the browser is still holding it; the slot waits for confirmation.
                release_for_execution(session, task.execution_id, cleanup_confirmed=False)
                session.commit()
                count += 1
        return count

    # -------------------------------------------------------------------- pools

    def recount_pools(self) -> int:
        """Repair `reserved_count` from the reservations that actually occupy capacity (§11.3).

        Returns how many pools were *wrong*, not how many were looked at: the tick logs whenever a
        counter is non-zero, so reporting the pool count would write an entry every few seconds and
        bury the repairs that actually happened.
        """
        from ..repositories.reservations import PoolRepository

        repaired = 0
        with get_database().session() as session:
            pools = list(session.scalars(select(WorkerPool)).all())
            if pools:
                # Pools are a platform table, so the repository is built with no tenant scope.
                repo = PoolRepository(session, "")
                for pool in pools:
                    before = int(pool.reserved_count or 0)
                    actual = repo.recount(pool.id)
                    repaired += int(before != actual)
                session.commit()
        return repaired


def _iso(value: object) -> str | None:
    return value.isoformat() if hasattr(value, "isoformat") else None
