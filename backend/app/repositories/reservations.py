"""Worker pools, execution reservations and leases (§9.3, §11.3).

Reservations are the single accounting point for concurrency: RESERVED, ACTIVE and QUARANTINED all
occupy tenant, project and pool capacity, and only RELEASED frees it. Rows are locked in the order
tenant -> project -> pool so two schedulers cannot both hand out the last slot.
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import func, select, update

from ..db.base import new_id, utcnow
from ..db.models import ExecutionReservation, TestExecution, WorkerLease, WorkerPool
from ..domain.enums import OCCUPYING_RESERVATION_STATUSES, ReservationStatus
from ..domain.errors import ApiError, ErrorCode
from .base import Scoped


class PoolRepository(Scoped[WorkerPool]):
    """Platform-level tables: only the scheduler touches them, never a tenant-facing route."""

    model = WorkerPool

    def by_name(self, name: str) -> WorkerPool | None:
        return self.session.scalar(select(WorkerPool).where(WorkerPool.name == name))

    def list(self) -> list[WorkerPool]:
        return list(self.session.scalars(select(WorkerPool).order_by(WorkerPool.name)).all())

    def occupying_count(self, pool_id: str) -> int:
        return int(
            self.session.scalar(
                select(func.count())
                .select_from(ExecutionReservation)
                .where(
                    ExecutionReservation.pool_id == pool_id,
                    ExecutionReservation.status.in_(list(OCCUPYING_RESERVATION_STATUSES)),
                )
            )
            or 0
        )

    def reserve(self, pool_id: str, *, expected_reserved_count: int | None = None) -> bool:
        """Atomically move reserved_count up under a pool capacity check."""
        locked = self.session.scalar(select(WorkerPool).where(WorkerPool.id == pool_id).with_for_update())
        if locked is None:
            raise ApiError(ErrorCode.NOT_FOUND, f"Worker pool {pool_id} missing")
        if expected_reserved_count is not None and locked.reserved_count != expected_reserved_count:
            return False
        if locked.reserved_count >= locked.capacity:
            return False
        locked.reserved_count = int(locked.reserved_count) + 1
        self.session.flush()
        return True

    def release(self, pool_id: str) -> None:
        locked = self.session.scalar(select(WorkerPool).where(WorkerPool.id == pool_id).with_for_update())
        if locked is not None and locked.reserved_count > 0:
            locked.reserved_count = int(locked.reserved_count) - 1
            self.session.flush()

    def recount(self, pool_id: str) -> int:
        """Recompute from reservations; the accounting invariant repair path (§11.3)."""
        actual = self.occupying_count(pool_id)
        locked = self.session.scalar(select(WorkerPool).where(WorkerPool.id == pool_id).with_for_update())
        if locked is not None:
            locked.reserved_count = actual
            self.session.flush()
        return actual


class ReservationRepository(Scoped[ExecutionReservation]):
    model = ExecutionReservation

    def create(
        self, *, tenant_id: str, project_id: str, execution_id: str, pool_id: str, ttl_seconds: int
    ) -> ExecutionReservation:
        reservation = ExecutionReservation(
            id=new_id(),
            tenant_id=tenant_id,
            project_id=project_id,
            execution_id=execution_id,
            pool_id=pool_id,
            generation=1,
            status=ReservationStatus.RESERVED.value,
            expires_at=utcnow() + timedelta(seconds=ttl_seconds),
        )
        self.session.add(reservation)
        self.session.flush()
        return reservation

    def current_for_execution(self, execution_id: str) -> ExecutionReservation | None:
        return self.session.scalar(
            select(ExecutionReservation)
            .where(
                ExecutionReservation.tenant_id == self.tenant_id,
                ExecutionReservation.execution_id == execution_id,
                ExecutionReservation.status != ReservationStatus.RELEASED.value,
            )
            .order_by(ExecutionReservation.generation.desc())
            .limit(1)
        )

    def by_id(self, reservation_id: str, *, for_update: bool = False) -> ExecutionReservation | None:
        stmt = select(ExecutionReservation).where(
            ExecutionReservation.tenant_id == self.tenant_id, ExecutionReservation.id == reservation_id
        )
        if for_update:
            stmt = stmt.with_for_update()
        return self.session.scalar(stmt)

    def activate(self, reservation_id: str, *, generation: int) -> ExecutionReservation | None:
        """RESERVED -> ACTIVE. A message for an older generation must not activate (§9.3)."""
        locked = self.by_id(reservation_id, for_update=True)
        if locked is None or locked.status != ReservationStatus.RESERVED.value:
            return None
        if locked.generation != generation:
            return None
        locked.status = ReservationStatus.ACTIVE.value
        locked.expires_at = None
        self.session.flush()
        return locked

    def requeue(self, reservation_id: str, *, ttl_seconds: int) -> ExecutionReservation | None:
        """Recycle an unclaimed reservation: release the slot, bump generation, reserve again."""
        locked = self.by_id(reservation_id, for_update=True)
        if locked is None or locked.status != ReservationStatus.RESERVED.value:
            return None
        locked.status = ReservationStatus.RELEASED.value
        locked.released_at = utcnow()
        self.session.flush()
        replacement = ExecutionReservation(
            id=new_id(),
            tenant_id=locked.tenant_id,
            project_id=locked.project_id,
            execution_id=locked.execution_id,
            pool_id=locked.pool_id,
            generation=locked.generation + 1,
            status=ReservationStatus.RESERVED.value,
            expires_at=utcnow() + timedelta(seconds=ttl_seconds),
        )
        self.session.add(replacement)
        self.session.flush()
        return replacement

    def quarantine(self, reservation_id: str, *, reason: str) -> None:
        locked = self.by_id(reservation_id, for_update=True)
        if locked is None or locked.status == ReservationStatus.RELEASED.value:
            return
        locked.status = ReservationStatus.QUARANTINED.value
        locked.released_at = None
        self.session.flush()
        self.last_quarantine_reason = reason  # surfaced by the reconciler log, not persisted

    def release(self, reservation_id: str) -> None:
        locked = self.by_id(reservation_id, for_update=True)
        if locked is None or locked.status == ReservationStatus.RELEASED.value:
            return
        locked.status = ReservationStatus.RELEASED.value
        locked.released_at = utcnow()
        self.session.flush()

    def expired_unclaimed(self) -> list[ExecutionReservation]:
        return list(
            self.session.scalars(
                select(ExecutionReservation).where(
                    ExecutionReservation.tenant_id == self.tenant_id,
                    ExecutionReservation.status == ReservationStatus.RESERVED.value,
                    ExecutionReservation.expires_at.isnot(None),
                    ExecutionReservation.expires_at <= utcnow(),
                )
            ).all()
        )

    def orphaned(self) -> list[ExecutionReservation]:
        """Reservations whose execution already finished — the crash-consistency check (§9.3)."""
        rows = self.session.execute(
            select(ExecutionReservation)
            .join(
                TestExecution,
                (TestExecution.tenant_id == ExecutionReservation.tenant_id)
                & (TestExecution.id == ExecutionReservation.execution_id),
            )
            .where(
                ExecutionReservation.tenant_id == self.tenant_id,
                ExecutionReservation.status != ReservationStatus.RELEASED.value,
                TestExecution.status == "FINISHED",
            )
        ).all()
        return [row[0] for row in rows]

    def project_quota_in_use(self, project_id: str) -> int:
        return int(
            self.session.scalar(
                select(func.count())
                .select_from(ExecutionReservation)
                .where(
                    ExecutionReservation.tenant_id == self.tenant_id,
                    ExecutionReservation.project_id == project_id,
                    ExecutionReservation.status.in_(list(OCCUPYING_RESERVATION_STATUSES)),
                )
            )
            or 0
        )


class WorkerLeaseRepository(Scoped[WorkerLease]):
    model = WorkerLease

    def heartbeat(
        self,
        worker_id: str,
        *,
        pool_id: str,
        capacity: int,
        active_count: int,
        capabilities: dict,
        draining: bool = False,
    ) -> WorkerLease:
        locked = self.session.scalar(select(WorkerLease).where(WorkerLease.worker_id == worker_id).with_for_update())
        if locked is None:
            locked = WorkerLease(
                id=new_id(),
                worker_id=worker_id,
                pool_id=pool_id,
                capacity=capacity,
                active_count=active_count,
                capabilities=capabilities,
                draining=draining,
                heartbeat_at=utcnow(),
            )
            self.session.add(locked)
        else:
            locked.pool_id = pool_id
            locked.capacity = capacity
            locked.active_count = active_count
            locked.capabilities = capabilities
            locked.draining = draining
            locked.heartbeat_at = utcnow()
        self.session.flush()
        return locked

    def live_workers(self, *, ttl_seconds: int) -> list[WorkerLease]:
        cutoff = utcnow() - timedelta(seconds=ttl_seconds)
        return list(
            self.session.scalars(
                select(WorkerLease).where(WorkerLease.heartbeat_at >= cutoff, WorkerLease.draining.is_(False))
            ).all()
        )

    def mark_draining(self, worker_id: str) -> None:
        self.session.execute(update(WorkerLease).where(WorkerLease.worker_id == worker_id).values(draining=True))
        self.session.commit()
