"""The per-request MCP call context, written by the transport and read by the adapters (§5.1).

Separated from both sides because the transport owns the ASGI scope and the tools own the envelope,
while this is the one thing they must agree on: where the verified principal lives, and what a tool is
allowed to conclude from it. A tool receives immutable facts about *this* call only - never a settings
object re-read from the environment, never a database reached through a module global, and never a
principal left over from the previous call on a reused task.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from ..config import Settings
from .errors import AdapterCode, NextAction, ToolFailure

#: Where the verified principal is parked on the ASGI scope for the tool adapters to pick up.
PRINCIPAL_STATE_KEY = "mcp_principal"
#: An optional `X-Tenant-Id`; it only ever constrains the argument, it never selects a tenant (§5.1).
TENANT_HINT_STATE_KEY = "tenant_hint"
REQUEST_ID_STATE_KEY = "request_id"
#: Monotonic arrival time, so one call's deadline is measured from admission and is never restarted at
#: the handler (§11: all read and write deadlines start at global admission).
CALL_STARTED_STATE_KEY = "mcp_started_at"
#: The global admission slot the transport took before authentication (§11).
GLOBAL_SLOT_STATE_KEY = "mcp_global_slot"
#: The admission this call's subject slot, bucket and lease live in, written by the gate (§11).
ADMISSION_STATE_KEY = "mcp_admission"

#: The admission of the call being handled, so a synchronous tool body does not have to thread one
#: through every helper to keep its lease accounting honest.
CURRENT_ADMISSION: ContextVar[Any] = ContextVar("mcp_current_admission", default=None)


def scope_state(request: Any) -> dict[str, Any]:
    scope = getattr(request, "scope", None)
    if not isinstance(scope, dict):
        return {}
    state = scope.get("state")
    return state if isinstance(state, dict) else {}


def request_id_of(state: Mapping[str, Any]) -> str:
    from ..api.deps import new_request_id

    return str(state.get(REQUEST_ID_STATE_KEY) or "") or new_request_id()


def request_id_from(request: Any) -> str:
    return request_id_of(scope_state(request))


@dataclass(frozen=True)
class McpCall:
    """What a tool may learn about the caller, taken only from the verified principal (§5.1).

    `subject` is what the signature proved. The platform's own `actor_id`, tenant membership and
    project roles are read from the database inside the unit of work, so nothing here pretends an
    identity has been checked against the tenant tables yet. `client_name` and `client_version` are
    client claims and stay out of this type entirely: what a caller says about itself may never
    participate in authorisation.
    """

    request_id: str
    subject: str
    credential_scopes: tuple[str, ...]
    tenant_hint: str | None
    deadline: float
    #: The issuer the signature proved, kept because admission counts a subject *within* an issuer.
    issuer: str = "local-dev"

    @property
    def remaining_seconds(self) -> float:
        return self.deadline - time.monotonic()

    def require_scopes(self, *scopes: str) -> None:
        missing = [scope for scope in scopes if scope and scope not in self.credential_scopes]
        if missing:
            raise ToolFailure(
                AdapterCode.FORBIDDEN,
                "The token does not carry the scope for this operation",
                details={"required_scopes": missing},
                next_action=NextAction.reauthorize_scope,
            )

    def check_tenant_hint(self, selected_tenant: str | None) -> None:
        """A header tenant and the argument tenant must name the same one, or the call is refused (§5.1).

        A tool whose tenant is optional passes `None`, which is the single case where the two sources
        cannot disagree.
        """
        if self.tenant_hint and selected_tenant and self.tenant_hint != selected_tenant:
            raise ToolFailure(
                AdapterCode.TENANT_SELECTION_CONFLICT,
                "The X-Tenant-Id header does not match the tenant_id argument",
                next_action=NextAction.fix_input,
            )


def call_from_request(request: Any, settings: Settings) -> McpCall:
    """Rebuild the call context from the principal the transport parked on this request's scope."""
    state = scope_state(request)
    principal = state.get(PRINCIPAL_STATE_KEY)
    if principal is None:
        # The auth middleware sits outside the dispatcher, so a call that reached a tool with no
        # principal is a wiring fault; refuse it as unauthenticated instead of trusting the scope.
        raise ToolFailure(
            AdapterCode.UNAUTHENTICATED,
            "The request reached a tool without a verified principal",
            next_action=NextAction.reauth_with_correct_audience,
        )
    started = state.get(CALL_STARTED_STATE_KEY)
    budget = settings.mcp_tool_timeout_seconds
    now = time.monotonic()
    return McpCall(
        request_id=request_id_of(state),
        subject=principal.subject,
        credential_scopes=tuple(principal.scopes),
        tenant_hint=state.get(TENANT_HINT_STATE_KEY),
        deadline=now + budget if started is None else started + budget,
        issuer=principal.issuer,
    )
