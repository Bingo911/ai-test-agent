"""The MCP tool registry, the per-call adapter pipeline and the resources one app instance owns
(§4.4, §5.1, §5.4, §6.1, §10).

Three planes stay separate on purpose:

* the transport answers *protocol* questions - bad JSON-RPC, unknown method, unusable protocol
  version - and this module never dresses such a failure up as a business result;
* the gate middleware answers *may this caller ask for this operation*: the per-tool OAuth scope and
  the unknown-argument check the SDK does not perform for itself;
* the tool adapter answers *what happened to the operation*, always as one bounded envelope.

Handlers are annotated `-> CallToolResult`. With that annotation the SDK builds no output schema and
passes the value through unchanged, which is what lets this layer own both content channels, the
`isError` mapping and the byte accounting instead of leaving them to generated validation.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, TypeVar

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.utilities.func_metadata import func_metadata
from mcp.shared.exceptions import MCPError
from mcp_types import INVALID_REQUEST, CallToolResult, ToolAnnotations
from pydantic import BaseModel, ConfigDict, ValidationError

from ..config import Settings
from ..db.base import Database
from ..domain.errors import ApiError
from ..observability import get_logger
from .auth import SCOPE_CONNECT, SCOPE_READ
from .callcontext import (
    ADMISSION_STATE_KEY,
    CURRENT_ADMISSION,
    GLOBAL_SLOT_STATE_KEY,
    McpCall,
    call_from_request,
    request_id_from,
    scope_state,
)
from .errors import AdapterCode, NextAction, ToolFailure
from .limits import Admission, GlobalSlot, McpLimiter, RedisAdmitter, subject_key_of
from .schemas import Answer, Envelope, call_tool_result, oversized

log = get_logger(__name__)

SERVER_NAME = "ai-test-agent"
SERVER_INSTRUCTIONS = (
    "Deterministic Markdown web test cases: author, compile, run and read the report. "
    "Every write needs an idempotency_key and a tenant_id. A test that ended FAILED or ERROR is "
    "ordinary data in a successful response, not a tool error."
)

#: §11 - the execution slot may be waited on for at most this long before the call is refused. This is
#: part of the contract rather than a knob: the surrounding budgets are configured, and this wait is
#: what makes a saturated process refuse quickly instead of quietly queueing.
BACKEND_SLOT_WAIT_SECONDS = 1.0

T = TypeVar("T")


class McpServices:
    """The resources the MCP stack owns for one app instance: settings, executor, database, admission
    (§4.4, §11).

    Injected by the composition root and closed on shutdown. Nothing here is reached through
    `get_database()` or `get_settings()`, which is what keeps a second app instance built with other
    Settings from borrowing the first instance's connections.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        database: Database | None = None,
        limiter: McpLimiter | None = None,
    ) -> None:
        self.settings = settings
        self.database = database
        self.limiter = limiter if limiter is not None else McpLimiter(settings)
        self.executor = ThreadPoolExecutor(
            max_workers=max(1, settings.mcp_max_inflight_total),
            thread_name_prefix="mcp-work",
        )
        # The limit is a slot count, not a queue: work is submitted only once a slot is held, so the
        # executor never accumulates a backlog of calls that should already have been refused.
        self._slots = asyncio.Semaphore(max(1, settings.mcp_max_inflight_total))
        self._closed = False

    @classmethod
    def for_settings(cls, settings: Settings, *, admitter: RedisAdmitter | None = None) -> McpServices:
        """Build the services with their own Engine against the same database (§4.4).

        Separate pooling, one dataset: an MCP query must not take a REST connection slot, and neither
        pool may be reached through the module-global `get_database()`.

        `admitter` is the multi-replica seam: two app instances in one process can be handed two
        `RedisAdmitter`s over one store, which is the only way a test can show a shared bucket and a
        shared lease set rather than two per-process counters. Left out, the limiter builds its own
        client from the settings, which is what a real replica does.
        """
        database = Database(
            settings.resolved_database_url(),
            pool_size=settings.mcp_db_pool_size,
            max_overflow=0,
            pool_timeout=1,
            sqlite_busy_timeout_ms=1000,
        )
        return cls(settings, database=database, limiter=McpLimiter(settings, admitter=admitter))

    @property
    def closed(self) -> bool:
        return self._closed

    async def run_blocking(self, work: Callable[[], T], *, admission: Admission | None = None) -> T:
        """Run one synchronous unit of work on the MCP executor under a bounded in-flight slot.

        The slot is released by the thread's own completion callback and never by the waiting
        coroutine: a client that disconnects mid-transaction must not free the slot for a replacement
        while the database work is still running (§4.4).

        The lease is taken after the execution slot and before the thread exists, because a lease is a
        statement about an in-flight call: registering one for work that never started would hold a
        cross-replica slot for nothing, and a lease the store did not confirm must not become a
        database task (§11).
        """
        if self._closed:
            raise ToolFailure(
                AdapterCode.DEPENDENCY_UNAVAILABLE,
                "MCP is starting or stopping",
                retryable=True,
                retry_after_ms=1000,
                next_action=NextAction.retry_same_key_or_query,
            )
        admitted = CURRENT_ADMISSION.get() if admission is None else admission
        try:
            await asyncio.wait_for(self._slots.acquire(), timeout=BACKEND_SLOT_WAIT_SECONDS)
        except asyncio.TimeoutError as exc:
            raise ToolFailure(
                AdapterCode.COMMAND_BUSY,
                "All MCP execution slots are busy",
                retryable=True,
                retry_after_ms=1000,
                next_action=NextAction.retry_same_key_or_query,
            ) from exc
        if admitted is not None:
            try:
                await self.limiter.register_lease(admitted)
            except BaseException:
                self._slots.release()
                raise
            # The thread holds a reference of its own from the moment it is possible to start it: a
            # caller that gives up must not report the admission slots as free underneath it.
            admitted.retain()
        loop = asyncio.get_running_loop()
        try:
            future = loop.run_in_executor(self.executor, work)
        except BaseException:
            # The thread never started, so this slot is still ours to hand back.
            self._slots.release()
            if admitted is not None:
                admitted.drop()
            raise

        def _finish(_future: Any) -> None:
            self._slots.release()
            if admitted is not None:
                admitted.drop()

        future.add_done_callback(_finish)
        # Shielded so a cancelled caller cannot abandon a transaction that already began; the thread
        # keeps its slot until it finishes, which is the only accounting the capacity bound allows.
        return await asyncio.shield(future)

    async def close(self) -> None:
        """Refuse new work, then wait for the threads that are genuinely still running (§4.3).

        `shutdown(wait=True)` is the honest form: reporting the resources as released while a database
        thread is still committing would let the process exit underneath it.
        """
        if self._closed:
            return
        self._closed = True
        # Admission unwinds first: new calls are refused, the cleanup queue gets its bounded drain, and
        # the Redis client closes before the threads those leases describe are waited on.
        await self.limiter.close()
        await asyncio.get_running_loop().run_in_executor(None, self.executor.shutdown, True)
        if self.database is not None:
            self.database.dispose()


@dataclass(frozen=True)
class ToolSpec:
    """One advertised tool: its discovery metadata, the scope it needs and its bound handler (§5.3)."""

    name: str
    description: str
    scope: str
    handler: Callable[..., Awaitable[CallToolResult]]
    annotations: ToolAnnotations | None = None


ToolFactory = Callable[[McpServices], ToolSpec]
_TOOL_FACTORIES: dict[str, ToolFactory] = {}

#: Strict argument models derived from each registered handler's own signature, filled at build time.
_STRICT_ARGUMENTS: dict[str, type[BaseModel]] = {}


def register_tool(name: str) -> Callable[[ToolFactory], ToolFactory]:
    def bind(factory: ToolFactory) -> ToolFactory:
        _TOOL_FACTORIES[name] = factory
        return factory

    return bind


def available_tool_names() -> tuple[str, ...]:
    return tuple(_TOOL_FACTORIES)


def _strict_argument_model(handler: Callable[..., Any]) -> type[BaseModel]:
    """A strict view of the SDK's own generated argument model, taken from the handler signature.

    The SDK derives its advertised JSON Schema from the handler but silently drops unknown keys, so a
    misspelled `expcted_row_version` would otherwise reach the body as a missing field. Reusing the
    generated model keeps the signature as the single source of truth for names, types, defaults and
    bounds; only the extra-key policy differs from the SDK's copy. `context` is the injected SDK
    context and never a client argument.
    """
    generated = func_metadata(handler, skip_names=("context",)).arg_model
    return type(
        f"Strict{generated.__name__}",
        (generated,),
        {"model_config": ConfigDict(extra="forbid", arbitrary_types_allowed=True)},
    )


def _validate_arguments(name: str, arguments: Mapping[str, Any]) -> None:
    model = _STRICT_ARGUMENTS.get(name)
    if model is None:
        return
    try:
        model.model_validate(dict(arguments))
    except ValidationError as exc:
        fields = sorted({str(".".join(str(part) for part in error["loc"])) or "arguments" for error in exc.errors()})
        raise ToolFailure(
            AdapterCode.VALIDATION_ERROR,
            "The arguments do not match this tool's schema",
            details={"invalid_fields": fields[:20]},
            next_action=NextAction.fix_input,
        ) from exc


class McpToolGateMiddleware:
    """Per-tool scope, admission and unknown-argument checks, inside the SDK but ahead of every handler
    (§5.3, §6.1, §11).

    This runs after the SDK has parsed the body and confirmed the mirrored `Mcp-Name`, so a client
    cannot present itself as a different tool than the one it is calling. The transport refuses a call
    whose token lacks the scope earlier, with the HTTP 403 and challenge a client can act on; this layer
    is the check that still holds when the transport could not read the operation from the body, so an
    under-scoped call is refused once rather than reaching a handler.

    Admission lives here rather than in the transport because a tool call is the first point where the
    operation, the verified subject and the selected tenant are all known - and a handshake or a
    notification is not a command to rate limit.
    """

    def __init__(self, services: McpServices, specs: Mapping[str, ToolSpec]) -> None:
        self.services = services
        self.specs = dict(specs)

    async def __call__(self, ctx: Any, call_next: Callable[[Any], Awaitable[Any]]) -> Any:
        # Notifications and handshake methods have no tool to authorise; `initialize` in particular is
        # handled inline, so awaiting a server-to-client request inside it could deadlock the socket.
        if ctx.method == "resources/read" and ctx.request_id is not None:
            return await self._read_resource(ctx, call_next)
        if ctx.method != "tools/call" or ctx.request_id is None:
            return await call_next(ctx)
        params = ctx.params if isinstance(ctx.params, Mapping) else {}
        name = params.get("name")
        spec = self.specs.get(name) if isinstance(name, str) else None
        if spec is None:
            # An unknown tool stays the SDK's protocol error: reporting a typo as a business failure
            # would claim the platform broke when the call never named a real operation.
            return await call_next(ctx)

        request_id = request_id_from(ctx.request)
        admission: Admission | None = None
        token = None
        try:
            call = call_from_request(ctx.request, self.services.settings)
            call.require_scopes(spec.scope)
            arguments = params.get("arguments")
            if arguments is not None and not isinstance(arguments, Mapping):
                raise ToolFailure(AdapterCode.VALIDATION_ERROR, "arguments must be an object")
            _validate_arguments(spec.name, arguments or {})
            admission = self._admit(ctx, call)
            if admission is not None:
                await self.services.limiter.admit_async(admission)
                token = CURRENT_ADMISSION.set(admission)
                scope_state(ctx.request)[ADMISSION_STATE_KEY] = admission
        except ToolFailure as failure:
            if admission is not None:
                admission.drop()
            return call_tool_result(Envelope.failure(request_id, failure))
        try:
            return await call_next(ctx)
        finally:
            # The request's own reference goes here, in the same frame that took it: a thread the call
            # started keeps its own reference, which is what keeps the slots busy until it finishes.
            if token is not None:
                CURRENT_ADMISSION.reset(token)
            if admission is not None:
                admission.drop()

    async def _read_resource(self, ctx: Any, call_next: Callable[[Any], Awaitable[Any]]) -> Any:
        """The scope floor for a document read (§7.1), refused as a protocol error and never as content.

        A `resources/read` result carries no envelope - its wire type is a list of contents - so the
        refusal this layer makes for a tool call has nowhere to go here. What it can do is refuse the
        request outright, and name the scope the credential is missing in the error's `data`, which is
        the same information the HTTP challenge carries when the transport is the one that can tell.

        There is no admission lease and no database on this path: the documents hold no tenant, no
        project and no business data, so they take no execution slot and leave no audit row (§11).
        """
        try:
            call = call_from_request(ctx.request, self.services.settings)
            call.require_scopes(SCOPE_CONNECT, SCOPE_READ)
        except ToolFailure as failure:
            raise MCPError(
                code=INVALID_REQUEST,
                message=failure.message,
                data={"code": failure.code, **(failure.details or {})},
            ) from failure
        params = ctx.params if isinstance(ctx.params, Mapping) else {}
        log.info(
            "mcp_resource_read",
            extra={"fields": {"uri": str(params.get("uri") or ""), "request_id": call.request_id}},
        )
        return await call_next(ctx)

    def _admit(self, ctx: Any, call: McpCall) -> Admission:
        """Take the subject slot for a verified caller, adopting the transport's global slot."""
        state = scope_state(ctx.request)
        slot = state.get(GLOBAL_SLOT_STATE_KEY)
        if not isinstance(slot, GlobalSlot):
            # A call that reached a tool without a global slot came through a transport this build did
            # not wire; admitting it would count nothing against the process bound, so refuse it here.
            raise ToolFailure(
                AdapterCode.COMMAND_BUSY,
                "MCP admission is not wired for this request",
                retryable=True,
                retry_after_ms=50,
                next_action=NextAction.retry_same_key_or_query,
            )
        admission = self.services.limiter.admit(
            subject_key=subject_key_of(call.issuer, call.subject),
            tenant_id=call.tenant_hint,
            call_id=call.request_id,
            remaining_seconds=call.remaining_seconds,
        )
        slot.hand_to(admission)
        return admission


async def run_tool(
    services: McpServices,
    context: Context,
    body: Callable[[McpCall, dict[str, Any]], Awaitable[Any]],
    *,
    arguments: Mapping[str, Any] | None = None,
    tenant_id: str | None = None,
) -> CallToolResult:
    """Run one tool body and render exactly one envelope, whatever happens inside it (§6.1, §10, §11).

    The exits are deliberately distinct: a `ToolFailure` is the client's to recover from, a domain
    `ApiError` keeps its own stable platform code, and anything else is logged under the request id and
    reported generically, because a stack trace, a SQL fragment or a driver message is not something a
    model gets to see.
    """
    request = context.request_context.request
    try:
        call = call_from_request(request, services.settings)
    except ToolFailure as failure:
        return call_tool_result(Envelope.failure(request_id_from(request), failure))

    try:
        call.check_tenant_hint(tenant_id)
        remaining = call.remaining_seconds
        if remaining <= 0:
            raise _deadline_exceeded(remaining)
        payload = await asyncio.wait_for(body(call, dict(arguments or {})), timeout=remaining)
    except asyncio.TimeoutError:
        return call_tool_result(Envelope.failure(call.request_id, _deadline_exceeded(call.remaining_seconds)))
    except ToolFailure as failure:
        return call_tool_result(Envelope.failure(call.request_id, failure))
    except ApiError as error:
        return call_tool_result(Envelope.failure(call.request_id, _from_api_error(error)))
    except Exception:
        log.exception("mcp_tool_internal_error", extra={"request_id": call.request_id})
        return call_tool_result(Envelope.failure(call.request_id, _internal_failure()))

    result = _envelope_for(call.request_id, payload)
    return oversized(result, call.request_id, budget=services.settings.mcp_max_response_bytes)


def _envelope_for(request_id: str, payload: Any) -> CallToolResult:
    """Render a body's return value as exactly one envelope, page fields and all (§6.1, §6.6)."""
    if isinstance(payload, Answer):
        return call_tool_result(payload.envelope(request_id))
    return call_tool_result(Envelope.success(request_id, payload))


def _deadline_exceeded(remaining: float) -> ToolFailure:
    # The work may already have committed, so the advice is to reuse the same key, not form a new
    # intent (§10); `remaining_ms` is how much of the budget the caller had.
    return ToolFailure(
        AdapterCode.TOOL_DEADLINE_EXCEEDED,
        "The operation exceeded the tool response budget",
        retryable=True,
        retry_after_ms=1000,
        details={"remaining_ms": max(0, round(remaining * 1000))},
        next_action=NextAction.retry_same_key_or_query,
    )


def _internal_failure() -> ToolFailure:
    return ToolFailure(
        AdapterCode.INTERNAL,
        "The platform failed to complete this operation",
        retryable=True,
        retry_after_ms=1000,
        next_action=NextAction.retry_same_key_or_query,
    )


#: Domain failures whose recovery is "wait and use the same key", not "change your intent" (§10).
_RETRYABLE_API_CODES = frozenset(
    {
        "DEPENDENCY_UNAVAILABLE",
        "STATE_STORE_UNAVAILABLE",
        "SECRET_UNAVAILABLE",
        "LEASE_LOST",
        "QUEUE_TIMEOUT",
        "ATTACHMENT_NOT_READY",
        "RATE_LIMITED",
        "COMMAND_BUSY",
    }
)

#: Where a client goes next for a domain code the envelope should not leave implicit (§10).
_NEXT_ACTION_FOR: dict[str, NextAction] = {
    "COMPILE_FAILED": NextAction.fix_input,
    "COMPILE_REVIEW_REQUIRED": NextAction.review_in_console,
    "SEMANTIC_ERROR": NextAction.fix_input,
    "TARGET_NEEDS_LOCATOR": NextAction.fix_input,
    "VERSION_CONFLICT": NextAction.query_current_policy_and_etag,
    "IDEMPOTENCY_CONFLICT": NextAction.fix_input,
    # Both of these say "the write may already be there", so changing the key would duplicate it (§10).
    "COMMAND_BUSY": NextAction.retry_same_key_or_query,
    "IDEMPOTENCY_RESULT_UNKNOWN": NextAction.retry_same_key_or_query,
    "VARIABLE_MISSING": NextAction.fix_input,
    "DOMAIN_NOT_ALLOWED": NextAction.fix_input,
}


def _from_api_error(error: ApiError) -> ToolFailure:
    code = error.code.value
    return ToolFailure(
        error.code,
        error.message,
        retryable=code in _RETRYABLE_API_CODES,
        retry_after_ms=1000 if code in _RETRYABLE_API_CODES else None,
        details=dict(error.details or {}),
        next_action=_NEXT_ACTION_FOR.get(code, NextAction.none),
    )


def build_mcp_server(
    settings: Settings,
    *,
    services: McpServices,
    tool_names: Iterable[str] | None = None,
) -> tuple[MCPServer, dict[str, ToolSpec]]:
    """Assemble the SDK server with exactly the named tools, the three documents and the gate (§5.4, §7.1).

    The specs come back alongside the server because the transport needs the same name-to-scope view the
    gate uses, and one registry must not answer with two different answers.
    """
    # The adapters register themselves by decorator, and importing them is what fills the registry. It is
    # deferred to here rather than done at module scope because `tools.py` imports this module: an
    # import-time cycle would leave `register_tool` undefined for the very file that needs it. Doing it
    # in the factory also means no caller can build a server that silently advertises nothing.
    from . import tools  # noqa: F401
    from .resources import DOC_MIME_TYPE, resource_specs

    selected = tuple(_TOOL_FACTORIES) if tool_names is None else tuple(dict.fromkeys(tool_names))
    unknown = sorted(set(selected) - set(_TOOL_FACTORIES))
    if unknown:
        raise ValueError(f"unknown MCP tools: {', '.join(unknown)}")

    specs = {name: _TOOL_FACTORIES[name](services) for name in selected}
    server = MCPServer(
        SERVER_NAME,
        instructions=SERVER_INSTRUCTIONS,
        middleware=[McpToolGateMiddleware(services, specs)],
    )
    for name in selected:
        spec = specs[name]
        server.add_tool(spec.handler, name=spec.name, description=spec.description, annotations=spec.annotations)
        _STRICT_ARGUMENTS[spec.name] = _strict_argument_model(spec.handler)
    for document in resource_specs():
        # A static URI and a zero-argument reader: the document is rendered when it is asked for, which is
        # what lets the workflow text mark an operation this build does not advertise instead of promising
        # it (§7.1).
        server.resource(
            document.uri,
            name=document.name,
            title=document.title,
            description=document.description,
            mime_type=DOC_MIME_TYPE,
        )(document.render)
    return server, specs
