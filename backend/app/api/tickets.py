"""Short-lived, single-purpose tickets (§13.2, §14.3).

A download link, an operator control session and an API bearer token are three different authorities:
presenting one where another is expected must fail, so each is issued under a purpose tag and can only
be redeemed against that purpose. Tokens are random and held in memory — the plaintext never lands in
the database, and a redeemed or expired ticket is gone.
"""

from __future__ import annotations

import secrets
import threading
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from ..db.base import utcnow
from ..domain.errors import ApiError, ErrorCode

DOWNLOAD = "download"
CONTROL = "control"
#: EventSource cannot send an Authorization header, so SSE connects with one of these instead.
STREAM = "stream"
PURPOSES = (DOWNLOAD, CONTROL, STREAM)

#: A control ticket only opens a socket; after that the connection holds the authority.
DEFAULT_TTL_SECONDS = 60


@dataclass(frozen=True)
class Ticket:
    value: str
    purpose: str
    tenant_id: str
    actor_id: str
    resource_id: str
    project_id: str | None
    expires_at: Any
    #: A control ticket is void the moment the pause it was minted for is superseded by a new lease.
    session_epoch: int | None = None
    consumed_at: Any = None

    @property
    def live(self) -> bool:
        return self.consumed_at is None and self.expires_at > utcnow()


class TicketStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._tickets: dict[str, Ticket] = {}

    def issue(
        self,
        purpose: str,
        *,
        tenant_id: str,
        actor_id: str,
        resource_id: str,
        project_id: str | None = None,
        session_epoch: int | None = None,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
    ) -> Ticket:
        if purpose not in PURPOSES:  # a typo must not create an untagged, redeemable-anywhere token
            raise ApiError(ErrorCode.VALIDATION_ERROR, f"Unknown ticket purpose '{purpose}'")
        self._sweep()
        ticket = Ticket(
            value=secrets.token_urlsafe(24),
            purpose=purpose,
            tenant_id=tenant_id,
            actor_id=actor_id,
            resource_id=resource_id,
            project_id=project_id,
            session_epoch=session_epoch,
            expires_at=utcnow() + timedelta(seconds=max(5, int(ttl_seconds))),
        )
        with self._lock:
            self._tickets[ticket.value] = ticket
        return ticket

    def redeem(
        self,
        purpose: str,
        value: str,
        *,
        resource_id: str | None = None,
        tenant_id: str | None = None,
        session_epoch: int | None = None,
        once: bool = True,
    ) -> Ticket:
        """Consume a ticket. Anything short of an exact, live match is a 403 (§14.3)."""
        wanted = (value or "").strip()
        if not wanted:
            raise ApiError(ErrorCode.UNAUTHENTICATED, "A ticket is required")
        with self._lock:
            ticket = self._tickets.get(wanted)
            if ticket is not None and once:
                del self._tickets[wanted]
        if ticket is None:
            raise ApiError(ErrorCode.FORBIDDEN, "The ticket is unknown, already used, or no longer present")
        if not ticket.live:
            raise ApiError(ErrorCode.FORBIDDEN, "The ticket has expired")
        if ticket.purpose != purpose:
            # Deliberately vague about *why*: telling a caller which other purpose a token serves
            # would turn an authorization failure into an oracle.
            raise ApiError(ErrorCode.FORBIDDEN, "The ticket is not valid for this operation")
        if resource_id is not None and ticket.resource_id != resource_id:
            raise ApiError(ErrorCode.FORBIDDEN, "The ticket was issued for a different resource")
        if tenant_id is not None and ticket.tenant_id != tenant_id:
            raise ApiError(ErrorCode.FORBIDDEN, "The ticket was issued for a different tenant")
        if (
            session_epoch is not None
            and ticket.session_epoch is not None
            and ticket.session_epoch != int(session_epoch)
        ):
            # The pause was taken over by a new worker lease: this ticket drives a session that no
            # longer exists, so it must not reach the page (§10.2).
            raise ApiError(ErrorCode.FORBIDDEN, "The ticket belongs to a superseded worker session")
        return ticket

    def revoke_actor(self, actor_id: str) -> int:
        """A disabled account loses its outstanding tickets too (§13.2 `PATCH /admin/users/{id}`)."""
        with self._lock:
            doomed = [key for key, ticket in self._tickets.items() if ticket.actor_id == actor_id]
            for key in doomed:
                del self._tickets[key]
        return len(doomed)

    def _sweep(self) -> None:
        now = utcnow()
        with self._lock:
            for key in [key for key, ticket in self._tickets.items() if ticket.expires_at <= now]:
                del self._tickets[key]


_store: TicketStore | None = None


def get_ticket_store() -> TicketStore:
    global _store
    if _store is None:
        _store = TicketStore()
    return _store
