"""Scheduler: fair pickup of QUEUED runs and transactional slot reservation (§9.1, §9.3).

The API never enqueues a run directly — it only wakes the scheduler. A run reaches a worker only
after this module has, in one transaction, taken a pool slot, written the reservation and produced
the `execution.execute` outbox row that carries the reservation id and generation.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select

from ..config import Settings, get_settings
from ..db.base import get_database, utcnow
from ..db.models import ExecutionReservation, Project, Tenant, TestExecution, WorkerPool
from ..domain.enums import OCCUPYING_RESERVATION_STATUSES, ExecutionStatus
from ..observability import get_logger
from .events import EXECUTION_EXECUTE

log = get_logger(__name__)


class Scheduler:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def tick(self, *, limit: int | None = None) -> list[str]:
        """Schedule at most `limit` runs; returns the execution ids that gained a reservation."""
        batch = limit if limit is not None else max(1, self.settings.worker_slots)
        scheduled: list[str] = []
        database = get_database()
        with database.session() as session:
            candidates = self._queued(session, batch=batch, postgres=database.is_postgres)
            per_tenant = self._tenant_budget(session, candidates)
            for execution in candidates:
                if len(scheduled) >= batch:
                    break
                key = (execution.tenant_id, execution.project_id)
                if per_tenant.get(key, 0) <= 0:
                    continue
                if self._reserve_and_enqueue(session, execution):
                    scheduled.append(execution.id)
                    per_tenant[key] = per_tenant[key] - 1
            session.commit()
        if scheduled:
            log.info(
                "scheduled executions", extra={"context": {"count": len(scheduled), "execution_ids": scheduled[:20]}}
            )
        return scheduled

    # ------------------------------------------------------------------- selection

    def _queued(self, session, *, batch: int, postgres: bool) -> list[TestExecution]:
        """Round-robin across tenants so one busy project cannot hold the whole queue."""
        occupied = (
            select(ExecutionReservation.execution_id)
            .where(ExecutionReservation.status.in_(list(OCCUPYING_RESERVATION_STATUSES)))
            .scalar_subquery()
        )
        statement = (
            select(TestExecution)
            .where(
                TestExecution.status == ExecutionStatus.QUEUED.value,
                TestExecution.cancel_requested_at.is_(None),
                # A run that already holds a slot is not a candidate, whatever its status says.
                ~TestExecution.id.in_(occupied),
            )
            .order_by(TestExecution.queued_at.asc(), TestExecution.id.asc())
            .limit(batch * 4)
        )
        if postgres:
            statement = statement.with_for_update(skip_locked=True)
        rows = list(session.scalars(statement).all())
        seen: dict[str, int] = {}
        interleaved: list[TestExecution] = []
        for row in rows:
            rank = seen.get(row.tenant_id, 0)
            seen[row.tenant_id] = rank + 1
            interleaved.append((rank, row.queued_at or utcnow(), row.id, row))
        interleaved.sort(key=lambda item: (item[0], item[1], item[3].project_id, item[2]))
        return [item[3] for item in interleaved]

    def _tenant_budget(self, session, candidates: list[TestExecution]) -> dict[tuple[str, str], int]:
        """How many more runs each (tenant, project) pair may start right now."""
        budgets: dict[tuple[str, str], int] = {}
        tenant_ids = {row.tenant_id for row in candidates}
        project_ids = {row.project_id for row in candidates}
        in_use_tenant = dict(
            session.execute(
                select(ExecutionReservation.tenant_id, func.count())
                .where(
                    ExecutionReservation.tenant_id.in_(tenant_ids or {"-"}),
                    ExecutionReservation.status.in_(list(OCCUPYING_RESERVATION_STATUSES)),
                )
                .group_by(ExecutionReservation.tenant_id)
            ).all()
        )
        in_use_project = dict(
            session.execute(
                select(ExecutionReservation.project_id, func.count())
                .where(
                    ExecutionReservation.project_id.in_(project_ids or {"-"}),
                    ExecutionReservation.status.in_(list(OCCUPYING_RESERVATION_STATUSES)),
                )
                .group_by(ExecutionReservation.project_id)
            ).all()
        )
        quotas: dict[str, dict[str, Any]] = {}
        for tenant in session.scalars(select(Tenant).where(Tenant.id.in_(tenant_ids or {"-"}))).all():
            quotas[tenant.id] = dict(tenant.quota or {})
        for project in session.scalars(select(Project).where(Project.id.in_(project_ids or {"-"}))).all():
            quotas[project.id] = dict(project.quota or {})
        for row in candidates:
            key = (row.tenant_id, row.project_id)
            if key in budgets:
                continue
            tenant_cap = int(quotas.get(row.tenant_id, {}).get("max_concurrent_executions", self.settings.worker_slots))
            project_cap = int(quotas.get(row.project_id, {}).get("max_concurrent_executions", max(1, tenant_cap // 2)))
            tenant_room = tenant_cap - int(in_use_tenant.get(row.tenant_id, 0))
            project_room = project_cap - int(in_use_project.get(row.project_id, 0))
            budgets[key] = max(0, min(tenant_room, project_room))
        return budgets

    # ------------------------------------------------------------------ reservation

    def _pool_for(self, session, execution: TestExecution) -> WorkerPool | None:
        pools = list(session.scalars(select(WorkerPool).order_by(WorkerPool.name)).all())
        for pool in pools:
            browsers = (pool.capabilities or {}).get("browsers") or []
            if pool.reserved_count < pool.capacity and (not browsers or execution.browser in browsers):
                return pool
        return None

    def _reserve_and_enqueue(self, session, execution: TestExecution) -> bool:
        from sqlalchemy.exc import IntegrityError

        from ..repositories.outbox import OutboxRepository
        from ..repositories.reservations import PoolRepository, ReservationRepository

        reservations = ReservationRepository(session, execution.tenant_id)
        if reservations.current_for_execution(execution.id) is not None:
            return False
        pool = self._pool_for(session, execution)
        if pool is None:
            return False
        pools = PoolRepository(session, execution.tenant_id)
        if not pools.reserve(pool.id):
            return False
        try:
            with session.begin_nested():
                reservation = reservations.create(
                    tenant_id=execution.tenant_id,
                    project_id=execution.project_id,
                    execution_id=execution.id,
                    pool_id=pool.id,
                    ttl_seconds=self.settings.reservation_ttl_seconds,
                )
        except IntegrityError:
            # Two schedulers picked the same run: the partial unique index lets exactly one win,
            # and the loser must hand its pool counter straight back (§11.3).
            pools.release(pool.id)
            log.info("reservation lost a race", extra={"context": {"execution_id": execution.id}})
            return False
        OutboxRepository(session, execution.tenant_id).enqueue(
            aggregate_id=execution.id,
            event_type=EXECUTION_EXECUTE,
            payload={
                "execution_id": execution.id,
                "reservation_id": reservation.id,
                "generation": reservation.generation,
                "tenant_id": execution.tenant_id,
                "project_id": execution.project_id,
                "state_version": execution.state_version,
            },
            discriminator=f"{EXECUTION_EXECUTE}:{reservation.id}:{reservation.generation}",
        )
        return True
