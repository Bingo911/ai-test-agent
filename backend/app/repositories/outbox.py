"""Transactional outbox (§9.4).

Business writes and their wake-up events commit in the same transaction; the dispatcher claims rows
with a conditional UPDATE, so a crashed dispatcher can re-deliver without a second row ever being
published. Consumers still deduplicate, because delivery is at-least-once.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select, update

from ..config import get_settings
from ..db.base import new_id, utcnow
from ..db.models import Outbox
from .base import Scoped


def outbox_dedupe_key(event_type: str, aggregate_id: str, discriminator: str) -> str:
    digest = hashlib.sha256(f"{event_type}|{aggregate_id}|{discriminator}".encode()).hexdigest()
    return f"{event_type}:{aggregate_id}:{digest[:32]}"


class OutboxRepository(Scoped[Outbox]):
    model = Outbox

    def enqueue(
        self,
        *,
        aggregate_id: str,
        event_type: str,
        payload: dict[str, Any],
        discriminator: str,
        available_at: datetime | None = None,
    ) -> Outbox | None:
        """Idempotent insert: the same logical event never queues twice (§11.3)."""
        dedupe_key = outbox_dedupe_key(event_type, aggregate_id, discriminator)
        existing = self.session.scalar(select(Outbox).where(Outbox.dedupe_key == dedupe_key))
        if existing is not None:
            if existing.published_at is None and existing.next_attempt_at > (available_at or utcnow()):
                existing.next_attempt_at = available_at or utcnow()
                self.session.flush()
            return existing
        row = Outbox(
            id=new_id(),
            tenant_id=self.tenant_id,
            aggregate_id=aggregate_id,
            event_type=event_type,
            payload=payload,
            dedupe_key=dedupe_key,
            attempts=0,
            next_attempt_at=available_at or utcnow(),
        )
        self.session.add(row)
        self.session.flush()
        return row

    def claim_pending(self, *, limit: int = 20) -> list[Outbox]:
        now = utcnow()
        candidates = list(
            self.session.scalars(
                select(Outbox)
                .where(Outbox.published_at.is_(None), Outbox.next_attempt_at <= now)
                .order_by(Outbox.next_attempt_at.asc(), Outbox.created_at.asc())
                .limit(limit)
                .with_for_update(skip_locked=True)
            ).all()
        )
        claimed: list[Outbox] = []
        for row in candidates:
            result = self.session.execute(
                update(Outbox)
                .where(
                    Outbox.id == row.id,
                    Outbox.published_at.is_(None),
                    Outbox.attempts == row.attempts,
                )
                .values(attempts=row.attempts + 1, next_attempt_at=now + timedelta(seconds=self._visibility_backoff()))
            )
            if result.rowcount == 1:
                row.attempts = row.attempts + 1
                claimed.append(row)
        return claimed

    @staticmethod
    def _visibility_backoff() -> int:
        return max(5, int(get_settings().outbox_poll_interval_seconds * 10))

    def mark_published(self, row_id: str) -> None:
        self.session.execute(update(Outbox).where(Outbox.id == row_id).values(published_at=utcnow(), last_error=None))
        self.session.commit()

    def mark_retry(self, row_id: str, *, error: str, attempts: int) -> None:
        self.session.execute(
            update(Outbox)
            .where(Outbox.id == row_id)
            .values(
                last_error=error[:300],
                next_attempt_at=utcnow() + timedelta(seconds=min(300, 2 ** min(attempts, 8))),
            )
        )
        self.session.commit()

    def forget_published(self, *, older_than_hours: int = 24) -> int:
        cutoff = utcnow() - timedelta(hours=older_than_hours)
        result = self.session.execute(
            update(Outbox)
            .where(Outbox.published_at.isnot(None), Outbox.published_at < cutoff)
            .values(payload={}, last_error="pruned")
        )
        self.session.commit()
        return int(result.rowcount or 0)

    def pending_count(self) -> int:
        return int(
            self.session.scalar(select(func.count()).select_from(Outbox).where(Outbox.published_at.is_(None))) or 0
        )

    def failing_publishes(self) -> int:
        """Rows a dispatcher has tried and cannot publish yet (§13.5 capability evidence).

        `mark_published` clears the error, so a non-zero count means the publish path is failing now -
        a broker that is down, or a queue that refuses the message. The count of pending rows is not
        this signal and never will be: an empty backlog says nothing about the broker (§13.5).
        """
        return int(
            self.session.scalar(
                select(func.count())
                .select_from(Outbox)
                .where(Outbox.published_at.is_(None), Outbox.last_error.isnot(None))
            )
            or 0
        )

    def stalled_publishes(self, *, older_than_seconds: int) -> int:
        """Rows past their own attempt deadline and still unpublished.

        This is the positive half of the evidence: a dispatcher that stopped running leaves rows whose
        window has closed, which a failing-publish count cannot see because nothing was ever attempted.
        """
        cutoff = utcnow() - timedelta(seconds=older_than_seconds)
        return int(
            self.session.scalar(
                select(func.count())
                .select_from(Outbox)
                .where(
                    Outbox.published_at.is_(None),
                    Outbox.last_error.is_(None),
                    func.coalesce(Outbox.next_attempt_at, Outbox.created_at) <= cutoff,
                )
            )
            or 0
        )

    def recent_publishes(self, *, within_seconds: int) -> int:
        """Rows published inside the window: the only evidence that the dispatcher is running now.

        The absence of a backlog is deliberately not this signal. §13.5 forbids inferring broker health
        from an empty outbox, so a capability may only be claimed from a publish that actually happened.
        """
        cutoff = utcnow() - timedelta(seconds=within_seconds)
        return int(
            self.session.scalar(select(func.count()).select_from(Outbox).where(Outbox.published_at >= cutoff)) or 0
        )
