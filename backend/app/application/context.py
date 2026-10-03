"""The transport-neutral caller an application case is run for (§3.2, §5.1).

This is deliberately not the FastAPI `Context` and not the MCP call object: a shared case must be
callable from either adapter, and it must not be able to reach for a `Request` or an SDK type by
accident. What crosses the boundary is the resolved database identity, the correlation id, the
entrypoint the request came in through, and the deadline the caller already committed to.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Literal

from ..config import Settings
from ..domain.enums import Permission
from ..domain.rbac import Identity

Entrypoint = Literal["rest", "mcp"]


@dataclass(frozen=True)
class CallContext:
    """One caller's authority, rebuilt per request and never cached (§14.1)."""

    identity: Identity
    request_id: str
    settings: Settings
    entrypoint: Entrypoint = "rest"
    #: OAuth scopes the credential carried. A REST caller has none, and that must not grant anything.
    scopes: tuple[str, ...] = ()
    #: Only a ticket-authenticated caller has one: the worker lease generation the ticket was minted in.
    session_epoch: int | None = None
    #: `time.monotonic()` by which the whole call must finish, measured from admission (§11).
    deadline: float | None = None
    #: Facts the adapter proved about the transport, kept out of every business decision (§5.1).
    transport: dict[str, Any] = field(default_factory=dict)

    @property
    def tenant_id(self) -> str:
        return self.identity.tenant_id

    @property
    def actor_id(self) -> str:
        return self.identity.user_id

    @property
    def subject(self) -> str:
        """What the credential signed, as opposed to the platform user it resolves to."""
        return self.identity.subject

    def can(self, permission: Permission, project_id: str | None = None) -> bool:
        return self.identity.can(permission, project_id)

    def visible_project(self, project_id: str) -> bool:
        return self.identity.role_in(project_id) is not None

    def remaining_seconds(self) -> float | None:
        return None if self.deadline is None else self.deadline - time.monotonic()

    def bounded_ms(self, limit_ms: int) -> int:
        """The smaller of a fixed ceiling and what is left on this call, so an inner wait cannot
        outlive the deadline that started counting at admission (§11).

        Returns `limit_ms` when the call carries no deadline, which is the REST case.
        """
        remaining = self.remaining_seconds()
        if remaining is None:
            return limit_ms
        return max(1, min(limit_ms, round(remaining * 1000)))
