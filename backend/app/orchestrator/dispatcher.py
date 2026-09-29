"""Outbox dispatcher: hand durable events to the broker, at least once (§9.4).

A row is claimed with a conditional update before it is published, and only marked published after
the broker accepted it — so a crash between the two replays the same event, which consumers absorb
through the lease and terminal-state checks in `workers/execution.py`.
"""

from __future__ import annotations

from ..config import Settings, get_settings
from ..db.base import get_database
from ..observability import get_logger
from .events import task_for_event
from .queue import Queue, get_queue

log = get_logger(__name__)


class OutboxDispatcher:
    def __init__(self, settings: Settings | None = None, queue: Queue | None = None) -> None:
        self.settings = settings or get_settings()
        self._queue = queue

    @property
    def queue(self) -> Queue:
        if self._queue is None:
            self._queue = get_queue()
        return self._queue

    def tick(self, *, limit: int = 20) -> int:
        """Publish the currently visible outbox rows; returns how many left the table."""
        from ..repositories.outbox import OutboxRepository

        published = 0
        with get_database().session() as session:
            repo = OutboxRepository(session, "")
            rows = repo.claim_pending(limit=limit)
            targets = [(row.id, row.tenant_id, row.event_type, dict(row.payload or {}), row.attempts) for row in rows]
            session.commit()
        for row_id, tenant_id, event_type, payload, attempts in targets:
            try:
                task = task_for_event(event_type)
                if task:
                    self.queue.publish(task, payload)
                with get_database().session(tenant_id or None) as session:
                    OutboxRepository(session, tenant_id).mark_published(row_id)
                published += 1
            except Exception as exc:
                log.warning(
                    "outbox publish failed",
                    extra={"context": {"row_id": row_id, "event_type": event_type, "error": str(exc)}},
                )
                with get_database().session(tenant_id or None) as session:
                    OutboxRepository(session, tenant_id).mark_retry(row_id, error=str(exc), attempts=attempts)
        return published

    def prune(self, *, older_than_hours: int = 24) -> int:
        from ..repositories.outbox import OutboxRepository

        with get_database().session() as session:
            return OutboxRepository(session, "").forget_published(older_than_hours=older_than_hours)
