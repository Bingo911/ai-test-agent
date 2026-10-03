"""The tool-level error codes and the failure this adapter raises (MCP design §10).

Two failure planes stay separate on purpose. A malformed JSON-RPC message, an unknown method or a
bad protocol version is the SDK's to answer; only a request that reached a real tool gets the
envelope, because a model can recover from "that case is not executable" and cannot recover from a
protocol error dressed up as a business result.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from ..domain.errors import ErrorCode


class AdapterCode(str, Enum):
    """New codes this adapter adds; domain codes keep coming from `ErrorCode` unchanged (§10)."""

    TENANT_SELECTION_CONFLICT = "TENANT_SELECTION_CONFLICT"
    MCP_PROJECT_DISABLED = "MCP_PROJECT_DISABLED"
    DATA_POLICY_DENIED = "DATA_POLICY_DENIED"
    AI_DISABLED = "AI_DISABLED"
    COMMAND_BUSY = "COMMAND_BUSY"
    IDEMPOTENCY_RESULT_UNKNOWN = "IDEMPOTENCY_RESULT_UNKNOWN"
    TOOL_DEADLINE_EXCEEDED = "TOOL_DEADLINE_EXCEEDED"
    RESULT_TOO_LARGE = "RESULT_TOO_LARGE"
    UNAUTHENTICATED = "UNAUTHENTICATED"
    FORBIDDEN = "FORBIDDEN"
    NOT_FOUND = "NOT_FOUND"
    VALIDATION_ERROR = "VALIDATION_ERROR"
    VERSION_CONFLICT = "VERSION_CONFLICT"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    RATE_LIMITED = "RATE_LIMITED"
    DEPENDENCY_UNAVAILABLE = "DEPENDENCY_UNAVAILABLE"
    INTERNAL = "INTERNAL"


class NextAction(str, Enum):
    """What the client does next; `retryable` alone does not say whether the key may change."""

    none = "none"
    retry_same_key_or_query = "retry_same_key_or_query"
    query_current_policy_and_etag = "query_current_policy_and_etag"
    reauth_with_correct_audience = "reauth_with_correct_audience"
    reauthorize_scope = "reauthorize_scope"
    review_in_console = "review_in_console"
    poll_again = "poll_again"
    narrow_request = "narrow_request"
    fix_input = "fix_input"


#: Failures whose request may have already reached the database, so the client must reuse its key (§10).
POSSIBLY_COMMITTED = frozenset(
    {
        AdapterCode.IDEMPOTENCY_RESULT_UNKNOWN,
        AdapterCode.TOOL_DEADLINE_EXCEEDED,
        AdapterCode.DEPENDENCY_UNAVAILABLE,
        AdapterCode.COMMAND_BUSY,
    }
)


class ToolFailure(Exception):
    """Raise from a tool adapter to answer `ok=false` inside the same envelope (§6.1).

    Keeping the code, the retry hint and the whitelisted details on one object is what makes the
    `retryable` / `next_action` pairing coherent: a lost commit says "same key", a version conflict
    says "read again", and only a confirmed unwritten request may take a new key.
    """

    def __init__(
        self,
        code: AdapterCode | ErrorCode | str,
        message: str,
        *,
        retryable: bool = False,
        retry_after_ms: int | None = None,
        details: dict[str, Any] | None = None,
        next_action: NextAction = NextAction.none,
    ) -> None:
        # An existing domain code is reused verbatim (§10), so only the adapter's own codes are looked
        # up in the enum; forcing everything through `AdapterCode(...)` would reject `COMPILE_FAILED`.
        self.code = code.value if isinstance(code, (AdapterCode, ErrorCode)) else str(code)
        self.message = message
        self.retryable = retryable
        self.retry_after_ms = retry_after_ms
        self.details = details or {}
        self.next_action = next_action
        super().__init__(message)
