"""Reservation clean-up used by both the worker and the reconciler (§9.3, §9.4).

Freeing a slot is deliberately conservative: capacity is only returned once the browser process that
held it is confirmed gone. Otherwise the reservation stays QUARANTINED and its slot stays withheld
for an operator, because an un-terminated browser can still be talking to the target site.
"""

from __future__ import annotations

from sqlalchemy import select

from ..config import Settings
from ..db.base import get_database
from ..db.models import ExecutionReservation, TestExecution
from ..domain.enums import CleanupStatus, ReservationStatus


def reservation_for(session, execution_id: str) -> ExecutionReservation | None:
    return session.scalar(
        select(ExecutionReservation)
        .where(
            ExecutionReservation.execution_id == execution_id,
            ExecutionReservation.status != ReservationStatus.RELEASED.value,
        )
        .order_by(ExecutionReservation.generation.desc())
        .limit(1)
    )


def release_for_execution(session, execution_id: str, *, cleanup_confirmed: bool, reason: str | None = None) -> str:
    """Returns what happened to the slot: `released`, `quarantined` or `none`."""
    from ..repositories.reservations import PoolRepository, ReservationRepository

    row = reservation_for(session, execution_id)
    if row is None:
        return "none"
    tenant_id = row.tenant_id
    reservations = ReservationRepository(session, tenant_id)
    pools = PoolRepository(session, tenant_id)
    if not cleanup_confirmed:
        reservations.quarantine(row.id, reason=reason or "browser termination unconfirmed")
        execution = session.get(TestExecution, execution_id)
        if execution is not None:
            execution.cleanup_status = CleanupStatus.QUARANTINED.value
        return "quarantined"
    reservations.release(row.id)
    pools.release(row.pool_id)
    return "released"


def quarantine_for_execution(session, execution_id: str, *, reason: str) -> str:
    return release_for_execution(session, execution_id, cleanup_confirmed=False, reason=reason)


def release_orphaned_reservations(settings: Settings | None = None) -> int:
    """Release slots whose execution already reached FINISHED with a clean teardown (§9.3)."""
    from ..repositories.reservations import PoolRepository, ReservationRepository

    count = 0
    with get_database().session() as session:
        rows = session.execute(
            select(ExecutionReservation, TestExecution.cleanup_status)
            .join(
                TestExecution,
                (TestExecution.tenant_id == ExecutionReservation.tenant_id)
                & (TestExecution.id == ExecutionReservation.execution_id),
            )
            .where(
                ExecutionReservation.status != ReservationStatus.RELEASED.value,
                TestExecution.status == "FINISHED",
            )
        ).all()
        for reservation, cleanup_status in rows:
            if cleanup_status == CleanupStatus.QUARANTINED.value:
                continue
            ReservationRepository(session, reservation.tenant_id).release(reservation.id)
            PoolRepository(session, reservation.tenant_id).release(reservation.pool_id)
            count += 1
        session.commit()
    return count
