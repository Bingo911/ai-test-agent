"""Mounting the MCP stack inside the existing FastAPI app (MCP design §4.1, §4.3, §5.3).

The dispatcher is deliberately a small exact-path switch rather than a `Mount`:

* a root `Mount("/", ...)` would swallow every REST route, and Starlette's mount redirects would
  turn `/mcp` into a 307 that several MCP clients refuse to follow;
* the MCP branch must sit *outside* the REST CORS stack, because the REST policy does not allow the
  protocol headers (`MCP-Protocol-Version`, `Mcp-Method`, `Mcp-Name`) and would reject a browser
  client before the SDK ever saw the request;
* everything that is not an exact MCP path must reach the original router untouched, so an unknown
  `/api/v1/...` still gets the platform's own 404 envelope and `X-Request-ID`.

Non-HTTP scopes - above all `lifespan` - are forwarded to the host app, which stays the owner of the
SDK session manager. A mounted Starlette sub-app never runs its own lifespan, so relying on it would
leave the server permanently un-initialised.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from starlette.middleware.cors import CORSMiddleware
from starlette.routing import get_route_path

from ..config import Settings
from ..observability import current_request, get_logger
from .auth import ALL_SCOPES, SCOPE_CONNECT, SCOPE_READ, AuthError, McpAuthenticator
from .callcontext import (
    CALL_STARTED_STATE_KEY,
    GLOBAL_SLOT_STATE_KEY,
    PRINCIPAL_STATE_KEY,
    REQUEST_ID_STATE_KEY,
    TENANT_HINT_STATE_KEY,
)
from .errors import ToolFailure
from .limits import McpLimiter
from .readiness import ReadinessSampler
from .server import McpServices, build_mcp_server

log = get_logger(__name__)

MCP_PATH = "/mcp"
METADATA_PREFIX = "/.well-known/oauth-protected-resource"

#: §4.1 - the headers a browser MCP client needs to send; the REST list is intentionally narrower.
MCP_ALLOW_HEADERS = (
    "Content-Type",
    "Authorization",
    "MCP-Protocol-Version",
    "Mcp-Method",
    "Mcp-Name",
    "Mcp-Session-Id",
    "X-Tenant-Id",
    "X-Request-ID",
)
MCP_EXPOSE_HEADERS = (
    "MCP-Protocol-Version",
    "Mcp-Session-Id",
    "Content-Location",
    "WWW-Authenticate",
    "X-Request-ID",
    "Retry-After",
)


def metadata_paths(settings: Settings) -> tuple[str, ...]:
    """The RFC 9728 path form for this resource plus the root alias (§5.3).

    Both spellings are legal and clients disagree about which to try first, so a deployment that
    publishes the resource at `/public/mcp` still answers `.../oauth-protected-resource/public/mcp`.
    """
    resource_path = urlparse(settings.mcp_public_url).path.rstrip("/") or MCP_PATH
    forms = {METADATA_PREFIX, f"{METADATA_PREFIX}{resource_path}"}
    if resource_path != MCP_PATH:
        forms.add(f"{METADATA_PREFIX}{MCP_PATH}")
    return tuple(sorted(forms))


def protected_resource_metadata(settings: Settings) -> dict[str, Any]:
    """The static metadata document; `resource` comes from configuration, never from the Host header (§5.3)."""
    issuer = (settings.oidc_issuer or "").rstrip("/")
    return {
        "resource": settings.mcp_public_url.rstrip("/"),
        "authorization_servers": [issuer] if issuer else [],
        "scopes_supported": list(ALL_SCOPES),
        "bearer_methods_supported": ["header"],
        "resource_name": "AI Test Agent",
    }


def _origin_of(resource_url: str) -> str:
    parsed = urlparse(resource_url)
    return f"{parsed.scheme}://{parsed.netloc}"


def challenge_header(settings: Settings, error: str, description: str, scope_hint: str | None) -> str:
    """A `Bearer` challenge pointing at the metadata document the client should fetch (§5.3).

    The quoted `error_description` is stripped of quotes: a message we do not control must not be
    able to terminate the attribute early and inject another challenge parameter.
    """
    path = urlparse(settings.mcp_public_url).path.rstrip("/")
    metadata_url = f"{_origin_of(settings.mcp_public_url)}{METADATA_PREFIX}{path}"
    safe = description.replace('"', "'").replace("\r", " ").replace("\n", " ")[:200]
    parts = [f'resource_metadata="{metadata_url}"', f'error="{error}"', f'error_description="{safe}"']
    if scope_hint:
        parts.append(f'scope="{scope_hint}"')
    return "Bearer " + ", ".join(parts)


class McpCorrelationMiddleware:
    """Assigns the call's correlation id and always unwinds the contextvar it set (§5.1)."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Callable, send: Callable) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        from ..api.deps import new_request_id

        supplied = str((scope.get("state") or {}).get(REQUEST_ID_STATE_KEY) or "").strip()
        request_id = supplied or new_request_id()
        scope["state"] = {
            **(scope.get("state") or {}),
            REQUEST_ID_STATE_KEY: request_id,
            CALL_STARTED_STATE_KEY: time.monotonic(),
        }
        token = current_request.set(request_id)
        try:
            await self.app(scope, receive, _send_with_request_id(send, request_id))
        finally:
            # An unpaired set would leak this call's id into whatever reuses the task afterwards.
            current_request.reset(token)


class McpAdmissionMiddleware:
    """Takes the one global admission slot a call holds from arrival (§11).

    This sits before authentication on purpose: identity is not trustworthy until the token has been
    verified, so the only bound that can be applied to an unverified request is the process-wide one.
    The slot is handed to the tool's admission inside the gate when the subject becomes known, and if
    no tool call follows - a handshake, a refusal, a bad path - this layer gives it back itself.
    """

    def __init__(self, app: Any, *, limiter: McpLimiter) -> None:
        self.app = app
        self.limiter = limiter

    async def __call__(self, scope: dict[str, Any], receive: Callable, send: Callable) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        try:
            slot = self.limiter.open_global()
        except ToolFailure as failure:
            state = scope.get("state") or {}
            await self._refuse(send, str(state.get(REQUEST_ID_STATE_KEY) or ""), failure)
            return
        scope["state"] = {**(scope.get("state") or {}), GLOBAL_SLOT_STATE_KEY: slot}
        try:
            await self.app(scope, receive, send)
        finally:
            slot.finish()

    async def _refuse(self, send: Callable, request_id: str, failure: ToolFailure) -> None:
        payload = json.dumps(
            {"error": {"code": failure.code, "message": failure.message, "request_id": request_id}}
        ).encode("utf-8")
        headers = {"Retry-After": "1"} if failure.retry_after_ms else {}
        await _send_json(send, 503, payload, headers, request_id)


def authentication_deadline(state: Mapping[str, Any], settings: Settings) -> float:
    """When verification must stop, on the loop's clock and measured from the call's own arrival.

    Two ceilings apply and the tighter one wins. Authentication has its own allowance, but a call that
    arrived 14.5 s ago has 0.5 s of its budget left, and spending two more seconds on a JWKS fetch would
    hand the caller a deadline the rest of the platform has already promised not to exceed (§5.1.1, §11).
    Every other deadline in the stack is relative to `CALL_STARTED_STATE_KEY` for the same reason: one
    clock per call, so a layer that starts its own adds its budget to the caller's instead of spending it.
    """
    budget = settings.mcp_auth_timeout_seconds
    started = state.get(CALL_STARTED_STATE_KEY)
    if started is not None:
        budget = min(budget, started + settings.mcp_tool_timeout_seconds - time.monotonic())
    return asyncio.get_running_loop().time() + max(0.0, budget)


class McpAuthMiddleware:
    """Verifies the bearer token and holds the `aita:connect` floor before the SDK sees a message (§5.3).

    Which scope a specific tool needs is checked later, inside the adapter: the operation being
    authorised is only known once the SDK has parsed the body and confirmed the mirrored `Mcp-Name`
    matches it, and trusting the header alone would let a client present as a different tool.
    """

    def __init__(self, app: Any, *, authenticator: McpAuthenticator, settings: Settings) -> None:
        self.app = app
        self.authenticator = authenticator
        self.settings = settings

    async def __call__(self, scope: dict[str, Any], receive: Callable, send: Callable) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        # A preflight carries no command and performs no action, so CORS answers it above us.
        if scope.get("method") == "OPTIONS":
            await self.app(scope, receive, send)
            return
        headers = _header_map(scope)
        state = dict(scope.get("state") or {})
        try:
            principal = await self.authenticator.authenticate(
                headers.get("authorization"), deadline=authentication_deadline(state, self.settings)
            )
            principal.require_scopes(SCOPE_CONNECT)
        except AuthError as exc:
            await self._refuse(send, state.get(REQUEST_ID_STATE_KEY, ""), exc)
            return
        state[PRINCIPAL_STATE_KEY] = principal
        hint = (headers.get("x-tenant-id") or "").strip()
        if hint:
            state[TENANT_HINT_STATE_KEY] = hint
        scope["state"] = state
        await self.app(scope, receive, send)

    async def _refuse(self, send: Callable, request_id: str, exc: AuthError) -> None:
        await _auth_refusal(send, request_id, exc, self.settings)


class McpScopeGateMiddleware:
    """Refuses an under-scoped call with the HTTP 403 a client can act on (§5.3, MCP-AC-10).

    A `Bearer` challenge is the one thing that tells a client which scope to go and ask for, and the gate
    inside the SDK cannot send one: by the time it runs, the status code belongs to the transport and the
    only refusal left is an envelope inside a 200. So the scope of the operation the body names is checked
    here first, while 403 is still this layer's to choose.

    The operation comes from the body, never from the mirrored `Mcp-Name` header - a header the client
    writes cannot be what decides what its token has to carry. A body this layer cannot read is passed
    through untouched, where the SDK's mirror check and the gate still hold.
    """

    def __init__(self, app: Any, *, scopes: Mapping[str, str], settings: Settings) -> None:
        self.app = app
        self.scopes = dict(scopes)
        self.settings = settings
        self.limit = max(0, settings.mcp_max_request_bytes)

    async def __call__(self, scope: dict[str, Any], receive: Callable, send: Callable) -> None:
        if scope["type"] != "http" or (scope.get("method") or "").upper() != "POST":
            await self.app(scope, receive, send)
            return
        if "json" not in _header_map(scope).get("content-type", ""):
            await self.app(scope, receive, send)
            return
        messages, body = await _peek_body(receive, self.limit)
        required = _required_scope(body, self.scopes) if body is not None else None
        state = scope.get("state") or {}
        principal = state.get(PRINCIPAL_STATE_KEY)
        if required is None or principal is None:
            await self.app(scope, _replay(messages, receive), send)
            return
        try:
            principal.require_scopes(required)
        except AuthError as exc:
            await _auth_refusal(send, str(state.get(REQUEST_ID_STATE_KEY) or ""), exc, self.settings)
            return
        await self.app(scope, _replay(messages, receive), send)


async def _peek_body(receive: Callable, limit: int) -> tuple[list[dict[str, Any]], bytes | None]:
    """Read the body once without taking it away from the app, or give up and say so.

    `None` means "this layer could not judge the call", never "the call is malformed": the messages are
    whatever was seen, in order, so the app downstream receives the same byte stream it would have.
    """
    messages: list[dict[str, Any]] = []
    collected: list[bytes] = []
    total = 0
    while True:
        message = await receive()
        messages.append(message)
        if message.get("type") != "http.request":
            return messages, None
        collected.append(message.get("body") or b"")
        total += len(collected[-1])
        if total > limit:
            # Over the cap the SDK enforces itself: forwarding the bytes is this layer's job, deciding is
            # not, and refusing here would turn a 413 the client can read into a 403 it cannot.
            return messages, None
        if not message.get("more_body", False):
            return messages, b"".join(collected)


def _replay(messages: list[dict[str, Any]], receive: Callable) -> Callable:
    async def _receive() -> dict[str, Any]:
        if messages:
            return messages.pop(0)
        return await receive()

    return _receive


def _required_scope(body: bytes, scopes: Mapping[str, str]) -> str | None:
    """The scope the operation in this body needs, or None when the body names no scoped operation."""
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    method = payload.get("method")
    if method == "resources/read":
        return SCOPE_READ
    if method != "tools/call":
        return None
    params = payload.get("params")
    name = params.get("name") if isinstance(params, dict) else None
    return scopes.get(name) if isinstance(name, str) else None


async def _auth_refusal(send: Callable, request_id: str, exc: AuthError, settings: Settings) -> None:
    headers: dict[str, str] = {}
    if exc.status in (401, 403):
        headers["WWW-Authenticate"] = challenge_header(settings, exc.code, exc.message, exc.scope_hint)
    if exc.retry_after_ms:
        headers["Retry-After"] = str(max(1, round(exc.retry_after_ms / 1000)))
    payload = json.dumps({"error": {"code": exc.code, "message": exc.message, "request_id": request_id}})
    await _send_json(send, exc.status, payload.encode("utf-8"), headers, request_id)


def _header_map(scope: dict[str, Any]) -> dict[str, str]:
    return {key.decode("latin-1").lower(): value.decode("latin-1") for key, value in scope.get("headers") or []}


def _send_with_request_id(send: Callable, request_id: str) -> Callable[[dict[str, Any]], Awaitable[None]]:
    async def wrapped(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.start" and request_id:
            pairs = [pair for pair in (message.get("headers") or []) if pair[0].lower() != b"x-request-id"]
            pairs.append((b"x-request-id", request_id.encode("latin-1")))
            message["headers"] = pairs
        await send(message)

    return wrapped


async def _send_json(
    send: Callable, status: int, payload: bytes, extra_headers: dict[str, str], request_id: str
) -> None:
    headers = [
        (b"content-type", b"application/json; charset=utf-8"),
        (b"content-length", str(len(payload)).encode("ascii")),
        (b"x-request-id", request_id.encode("latin-1")),
    ]
    headers.extend((key.lower().encode("latin-1"), value.encode("latin-1")) for key, value in extra_headers.items())
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": payload})


@dataclass
class McpBundle:
    """The assembled MCP ASGI chain, plus the session manager the host lifespan must run (§4.3)."""

    asgi: Any
    session_manager: Any
    services: McpServices
    authenticator: McpAuthenticator
    metadata: dict[str, bytes] = field(default_factory=dict)
    sampler: Any = None
    _stack: AsyncExitStack | None = None
    _closing: bool = False
    _stopped: bool = False

    @property
    def running(self) -> bool:
        """True only while the manager is entered and the bundle is not already winding down."""
        return self._stack is not None and not self._closing

    async def start(self) -> None:
        """Enter the SDK session manager exactly once, before any MCP request is answered."""
        if self._stack is not None:
            raise RuntimeError("the MCP session manager is already running")
        if self._stopped:
            # The SDK's task group cannot be re-entered once it has been closed, so a restart attempt
            # would fail later inside the manager with a message about a different object. A process
            # that wants MCP back builds a new app instance, which is what a reload does anyway.
            raise RuntimeError("a stopped MCP session manager cannot be restarted")
        stack = AsyncExitStack()
        await stack.__aenter__()
        try:
            # The cleanup workers and the renewal loop are tasks, so they can only be started once the
            # lifespan is running: an unstarted limiter would leak every lease it never released.
            self.services.limiter.start()
            if self.sampler is not None:
                # The first sample is taken before anything reports ready, so a probe never answers from an
                # empty cache about a dependency the process has not looked at (§13.5).
                await self.sampler.start()
            await stack.enter_async_context(self.session_manager.run())
        except BaseException:
            if self.sampler is not None:
                await self.sampler.stop()
            await self.services.limiter.close()
            await stack.__aexit__(*sys.exc_info())
            raise
        self._closing = False
        self._stack = stack

    async def stop(self) -> None:
        """Refuse new commands, close the threads this adapter owns, then leave the manager (§4.3).

        The order is the one the shutdown contract requires: JWKS/Redis clients and the MCP executor
        unwind *before* the SDK manager, so a request still in the transport finds its resources
        intact, and no thread is reported as released while it is still running.
        """
        self._closing = True
        self._stopped = True
        stack = self._stack
        try:
            # The sampler is stopped first: it reads the database and the limiter's client, so it must not
            # be handed a closed one mid-sample (§13.5: 关闭时排空采样及客户端).
            if self.sampler is not None:
                await self.sampler.stop()
            await self.authenticator.close()
            await self.services.close()
        finally:
            self._stack = None
        if stack is not None:
            await stack.__aexit__(None, None, None)


def build_mcp_bundle(
    settings: Settings,
    *,
    services: McpServices | None = None,
    authenticator: McpAuthenticator | None = None,
    tool_names: Iterable[str] | None = None,
) -> McpBundle:
    """Create the SDK application and wrap it in this project's own CORS/auth boundaries."""
    services = services or McpServices.for_settings(settings)
    authenticator = authenticator or McpAuthenticator(settings)
    server, specs = build_mcp_server(settings, services=services, tool_names=tool_names)
    # Calling this is what instantiates the session manager, so it must precede `McpBundle.start()`.
    sdk_app = server.streamable_http_app(
        stateless_http=True,
        streamable_http_path=MCP_PATH,
        max_request_body_size=settings.mcp_max_request_bytes,
        transport_security=_transport_security(settings),
    )
    chain: Any = McpScopeGateMiddleware(
        sdk_app,
        scopes={name: spec.scope for name, spec in specs.items()},
        settings=settings,
    )
    chain = McpAuthMiddleware(chain, authenticator=authenticator, settings=settings)
    chain = McpAdmissionMiddleware(chain, limiter=services.limiter)
    chain = McpCorrelationMiddleware(chain)
    chain = CORSMiddleware(
        chain,
        allow_origins=list(settings.mcp_allowed_origin_list),
        allow_credentials=False,
        # DELETE is the legacy close-channel verb; a browser client that sends it must not be refused
        # by CORS before the SDK can answer with its own protocol-level rejection.
        allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
        allow_headers=list(MCP_ALLOW_HEADERS),
        expose_headers=list(MCP_EXPOSE_HEADERS),
        max_age=600,
    )
    document = json.dumps(protected_resource_metadata(settings), ensure_ascii=False, separators=(",", ":"))
    return McpBundle(
        asgi=chain,
        session_manager=server.session_manager,
        services=services,
        authenticator=authenticator,
        sampler=ReadinessSampler(settings, limiter=services.limiter, database=services.database),
        metadata={path: document.encode("utf-8") for path in metadata_paths(settings)},
    )


def _transport_security(settings: Settings) -> Any:
    from mcp.server.transport_security import TransportSecuritySettings

    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=list(settings.mcp_allowed_host_list),
        allowed_origins=list(settings.mcp_allowed_origin_list),
    )


class McpDispatchMiddleware:
    """Routes exact MCP paths to the MCP stack and everything else to the original app (§4.1)."""

    def __init__(self, app: Any, *, bundle: McpBundle | None = None) -> None:
        self.app = app
        self.bundle = bundle
        self.metadata = dict(bundle.metadata) if bundle else {}

    async def __call__(self, scope: dict[str, Any], receive: Callable, send: Callable) -> None:
        bundle = self.bundle
        # Lifespan and websocket scopes belong to the host FastAPI; swallowing them would stop the app
        # from starting, or break its shutdown handshake.
        if scope["type"] != "http" or bundle is None:
            await self.app(scope, receive, send)
            return
        path = get_route_path(scope)
        if path in self.metadata:
            await self._metadata(scope, send, self.metadata[path])
            return
        if path == MCP_PATH:
            await self._serve(bundle, scope, receive, send)
            return
        if path == f"{MCP_PATH}/":
            # `/mcp/` is a synonym, not a redirect. The SDK only knows `/mcp`, so a scope *copy* is
            # normalised while the caller's own scope, query string and root_path stay untouched.
            adjusted = {**scope, "path": MCP_PATH, "raw_path": MCP_PATH.encode("utf-8")}
            await self._serve(bundle, adjusted, receive, send)
            return
        await self.app(scope, receive, send)

    async def _serve(self, bundle: McpBundle, scope: dict[str, Any], receive: Callable, send: Callable) -> None:
        if not bundle.running:
            # Answering from an un-initialised manager would half-succeed: the SDK raises inside the
            # session and the client sees a protocol error instead of an operator-visible outage.
            request_id = str((scope.get("state") or {}).get(REQUEST_ID_STATE_KEY) or "")
            message = {"code": "dependency_unavailable", "message": "MCP is starting or stopping"}
            if request_id:
                message["request_id"] = request_id
            payload = json.dumps({"error": message}).encode("utf-8")
            await _send_json(send, 503, payload, {"Retry-After": "1"}, request_id)
            return
        await bundle.asgi(scope, receive, send)

    async def _metadata(self, scope: dict[str, Any], send: Callable, payload: bytes) -> None:
        method = (scope.get("method") or "GET").upper()
        if method == "GET":
            await _send_json(send, 200, payload, {"cache-control": "public, max-age=60"}, "")
            return
        if method == "OPTIONS":
            await _send_json(send, 204, b"", {"allow": "GET, OPTIONS"}, "")
            return
        await _send_json(send, 405, b"", {"allow": "GET, OPTIONS", "content-type": "application/json"}, "")
