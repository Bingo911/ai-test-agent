"""M0: the MCP transport contract, verified against the locked SDK rather than a hand-written envelope.

These tests exist because every failure mode here is a wiring failure a unit test cannot see: a `Mount`
that would redirect `/mcp`, the REST CORS policy refusing `Mcp-Protocol-Version`, a session manager
nobody started, a probe that answers 200 from a half-initialised transport, or an unknown `/api/v1`
path that stopped producing the platform's own 404. Both protocol tiers are driven with the real client
because the envelope each one requires is a property of that SDK version, not of this code.
"""

from __future__ import annotations

import json
import re
import threading
from collections.abc import Callable
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any

import httpx2
import pytest
from backend.app.config import Settings
from backend.app.db.bootstrap import ensure_development_workspace
from backend.app.main import create_app
from backend.app.mcp.auth import ALL_SCOPES, SCOPE_CONNECT, SCOPE_READ, SCOPE_RUN, VerifiedPrincipal
from backend.app.mcp.callcontext import PRINCIPAL_STATE_KEY, REQUEST_ID_STATE_KEY
from backend.app.mcp.server import McpServices, available_tool_names, build_mcp_server
from backend.app.mcp.transport import MCP_PATH, METADATA_PREFIX, McpScopeGateMiddleware, metadata_paths
from mcp.client.client import Client
from mcp.client.streamable_http import streamable_http_client
from starlette.testclient import TestClient

from .mcp_live import GateContext, gate

HOST = "127.0.0.1:8000"
BASE = f"http://{HOST}"
MODERN = "2026-07-28"
LEGACY = "2025-11-25"
ORIGIN = "http://localhost:5173"


def _mcp_settings(database: Any, tmp_path: Any, **overrides: Any) -> Settings:
    """Settings that pass §12.2 startup validation with MCP on, against the database this fixture built.

    Built explicitly rather than from `get_settings()`: the whole stack has to be constructible from an
    injected Settings object, which is also what lets two app instances with different configuration
    coexist in one process without borrowing each other's limits.

    The development workspace is seeded because the tools under test are real reads, and a read answers
    `UNAUTHENTICATED` for an identity the platform has never provisioned.
    """
    overrides.setdefault("mcp_enabled", True)
    settings = Settings(
        app_env="test",
        database_url=database.url,
        data_dir=tmp_path / "data",
        queue_backend="inprocess",
        object_store="local",
        ai_enabled=False,
        redis_url="",
        log_level="WARNING",
        **overrides,
    )
    settings.validate_runtime()
    settings.ensure_dirs()
    with database.session() as session:
        ensure_development_workspace(session, settings)
        session.commit()
    return settings


def _auth(settings: Settings) -> dict[str, str]:
    return {"Authorization": f"Bearer {settings.dev_engineer_token}"}


class _AsgiTransport:
    """The SDK's `Transport` is only an async context manager yielding streams; this one is the ASGI app.

    `base_url` supplies the Host header the SDK's DNS-rebinding protection checks, so the client presents
    the real host. `testserver` would be refused, and that refusal is itself a case under test below.
    """

    def __init__(self, app: Any, token: str) -> None:
        self._client = httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app),
            headers={"Authorization": f"Bearer {token}"},
            base_url=BASE,
        )
        self._stack: AsyncExitStack | None = None

    async def __aenter__(self) -> Any:
        self._stack = AsyncExitStack()
        await self._stack.__aenter__()
        await self._stack.enter_async_context(self._client)
        return await self._stack.enter_async_context(
            streamable_http_client(f"{BASE}{MCP_PATH}", http_client=self._client)
        )

    async def __aexit__(self, *exc_info: Any) -> Any:
        assert self._stack is not None
        return await self._stack.__aexit__(*exc_info)


@asynccontextmanager
async def _live_client(settings: Settings, app: Any, *, mode: str = "auto"):
    """A started bundle plus one connected client, both on the event loop this test is running on.

    The manager is entered here instead of by a `TestClient` portal because the services' semaphores bind
    to the loop that first uses them: transport traffic and the stack must share one loop.
    """
    bundle = app.state.mcp
    await bundle.start()
    try:
        async with Client(_AsgiTransport(app, settings.dev_engineer_token), mode=mode) as client:  # type: ignore[arg-type]
            yield client
    finally:
        await bundle.stop()


@pytest.fixture
def mcp_settings(database: Any, tmp_path: Any) -> Settings:
    return _mcp_settings(database, tmp_path)


# --------------------------------------------------------------------------------------
# the two protocol tiers, driven by the real client
# --------------------------------------------------------------------------------------


#: §5.4 pins the surface itself: fourteen fixed tools, named here in the order this server registers them.
#: The list is written out rather than counted because a rename is as much a contract break as an addition.
CATALOG = (
    "aita_get_context",
    "aita_list_projects",
    "aita_list_environments",
    "aita_list_cases",
    "aita_get_case",
    "aita_create_case",
    "aita_add_case_revision",
    "aita_compile_case_revision",
    "aita_get_compilation",
    "aita_get_execution",
    "aita_get_execution_steps",
    "aita_get_report",
    "aita_run_test",
    "aita_cancel_execution",
)


@pytest.mark.parametrize(("mode", "expected"), [("auto", MODERN), ("legacy", LEGACY)])
async def test_a_tool_round_trips_on_both_protocol_tiers(mcp_settings, mode, expected):
    app = create_app(mcp_settings)
    async with _live_client(mcp_settings, app, mode=mode) as client:
        assert client.protocol_version == expected
        tools = await client.list_tools()
        assert [tool.name for tool in tools.tools] == list(CATALOG)

        result = await client.call_tool("aita_get_context", {})
        assert result.is_error is False, result.content[0].text
        envelope = result.structured_content
        assert envelope["ok"] is True
        assert envelope["error"] is None
        assert envelope["schema_version"] == "1.0"
        assert envelope["request_id"]
        data = envelope["data"]
        assert data["tenant_id"] is None
        assert isinstance(data["items"], list)
        assert data["capabilities"]
        # Both channels carry the same document, so neither can slip past the byte budget unnoticed.
        assert json.loads(result.content[0].text) == envelope


async def test_the_catalog_is_fourteen_static_tools_that_name_nothing_of_the_caller(mcp_settings):
    """§5.4: discovery is a fixed document, so it cannot be the way a project id, a path or a token leaves.

    The count and the names are written down here rather than compared with the registry alone, because a
    registry that grew by one tool silently would otherwise agree with itself and still break the contract.
    """
    app = create_app(mcp_settings)
    assert len(CATALOG) == 14, "the design registers exactly fourteen tools"
    assert list(available_tool_names()) == list(CATALOG)

    async with _live_client(mcp_settings, app) as client:
        listed = await client.list_tools()

    wire = json.dumps([tool.model_dump(mode="json") for tool in listed.tools], ensure_ascii=False)
    assert [tool.name for tool in listed.tools] == list(CATALOG)
    assert all(tool.description.strip() for tool in listed.tools)
    assert not re.search(r"[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}", wire)
    assert mcp_settings.dev_engineer_token not in wire
    assert "sqlite://" not in wire
    assert "http://" not in wire


async def test_a_read_runs_its_database_work_on_the_mcp_executor(mcp_settings):
    """§4.4: the thread that runs the query, not the thread that formatted the answer.

    The executor assertion used to come back inside a probe tool's own payload, which proved only that
    the probe had been called on that thread. Watching the discovery query run is the claim that matters:
    a tool that reached the database from the event loop would block every other session in the process.
    """
    from backend.app.application import discovery

    threads: list[str] = []
    original = discovery.tenant_page

    def spy(*args: Any, **kwargs: Any) -> Any:
        threads.append(threading.current_thread().name)
        return original(*args, **kwargs)

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(discovery, "tenant_page", spy)
    app = create_app(mcp_settings)
    try:
        async with _live_client(mcp_settings, app) as client:
            assert (await client.call_tool("aita_get_context", {})).structured_content["ok"] is True
    finally:
        monkeypatch.undo()

    assert threads, "the read never reached the discovery query"
    assert all(name.startswith("mcp-work") for name in threads)


async def test_limits_come_from_the_injected_settings_not_the_process_default(database, tmp_path):
    """A second app instance answers with its own budgets, which is what keeps two of them honest (§4.4)."""
    settings = _mcp_settings(database, tmp_path, mcp_max_response_bytes=5_000_000, mcp_tool_timeout_seconds=7.0)
    app = create_app(settings)
    async with _live_client(settings, app) as client:
        data = (await client.call_tool("aita_get_context", {})).structured_content["data"]
        assert data["limits"]["response_bytes"] == 5_000_000
        assert data["limits"]["tool_timeout_seconds"] == 7.0
        assert data["limits"]["metadata_response_bytes"] == settings.mcp_max_metadata_response_bytes
        assert settings.mcp_max_response_bytes != Settings().mcp_max_response_bytes


async def test_advertised_schema_does_not_forbid_unknown_arguments_itself(mcp_settings):
    """The SDK derives a schema from the handler but does not close it, so the gate owns that check (§6.1)."""
    app = create_app(mcp_settings)
    async with _live_client(mcp_settings, app) as client:
        tool = (await client.list_tools()).tools[0]
        assert tool.output_schema is None
        assert tool.input_schema.get("additionalProperties") is not False


async def test_unknown_tool_stays_a_protocol_refusal_not_a_business_one(mcp_settings):
    """A typo names no operation, so it must not arrive wrapped in an envelope the client could parse."""
    app = create_app(mcp_settings)
    async with _live_client(mcp_settings, app) as client:
        result = await client.call_tool("aita_nope", {})
        assert result.is_error is True
        assert result.structured_content is None
        with pytest.raises(json.JSONDecodeError):
            json.loads(result.content[0].text)


# --------------------------------------------------------------------------------------
# the gate: scope floor and strict arguments, ahead of every handler
# --------------------------------------------------------------------------------------


async def test_unknown_arguments_are_refused_before_the_handler_runs(mcp_settings):
    services = McpServices(mcp_settings)
    middleware, call_next, reached = gate(services)
    try:
        ctx = GateContext(VerifiedPrincipal("local-dev", "dev-engineer", ALL_SCOPES), {})
        ctx.params = {"name": "aita_get_context", "arguments": {"bogus": 1, "also_bogus": 2}}
        result = await middleware(ctx, call_next)
        assert reached == []
        assert result.is_error is True
        error = result.structured_content["error"]
        assert error["code"] == "VALIDATION_ERROR"
        assert error["details"]["invalid_fields"] == ["also_bogus", "bogus"]
        assert error["next_action"] == "fix_input"
    finally:
        await services.close()


async def test_arguments_that_are_not_an_object_are_refused(mcp_settings):
    services = McpServices(mcp_settings)
    middleware, call_next, reached = gate(services)
    try:
        ctx = GateContext(VerifiedPrincipal("local-dev", "dev-engineer", ALL_SCOPES), {})
        ctx.params = {"name": "aita_get_context", "arguments": ["not", "an", "object"]}
        result = await middleware(ctx, call_next)
        assert reached == []
        assert result.structured_content["error"]["code"] == "VALIDATION_ERROR"
    finally:
        await services.close()


async def test_missing_scope_is_an_envelope_refusal_not_a_protocol_error(mcp_settings):
    services = McpServices(mcp_settings)
    middleware, call_next, reached = gate(services)
    try:
        ctx = GateContext(
            VerifiedPrincipal("local-dev", "dev-engineer", (SCOPE_READ,)),
            {"name": "aita_get_context", "arguments": {}},
        )
        result = await middleware(ctx, call_next)
        assert reached == []
        error = result.structured_content["error"]
        assert error["code"] == "FORBIDDEN"
        assert error["details"] == {"required_scopes": ["aita:connect"]}
        assert error["next_action"] == "reauthorize_scope"
    finally:
        await services.close()


async def test_handshake_and_notifications_pass_through_unexamined(mcp_settings):
    """`initialize` is served inline by the SDK and names no tool; checking inside it could deadlock it."""
    services = McpServices(mcp_settings)
    middleware, call_next, reached = gate(services)
    try:
        ctx = GateContext(VerifiedPrincipal("local-dev", "nobody", ()), {"name": "aita_get_context"})
        ctx.method = "initialize"
        assert await middleware(ctx, call_next) == "reached"
        ctx.method = "notifications/cancelled"
        ctx.request_id = None
        assert await middleware(ctx, call_next) == "reached"
        assert reached == ["handler", "handler"]
    finally:
        await services.close()


# --------------------------------------------------------------------------------------
# exact-path dispatch, CORS, metadata and the readiness probe
# --------------------------------------------------------------------------------------


def test_slash_and_exact_paths_reach_the_same_stack_without_a_307(mcp_settings):
    app = create_app(mcp_settings)
    with TestClient(app, base_url=BASE) as client:
        for path in (MCP_PATH, f"{MCP_PATH}/"):
            response = client.post(path, follow_redirects=False)
            # Unauthenticated, but recognised as an MCP path: a redirect would mean the dispatcher missed.
            assert response.status_code == 401, path
        # A path that merely starts with `/mcp` belongs to no MCP route and stays a REST 404.
        assert client.post("/mcpx").status_code == 404


def test_missing_or_rejected_token_carries_a_readable_challenge(mcp_settings):
    app = create_app(mcp_settings)
    with TestClient(app, base_url=BASE) as client:
        anonymous = client.post(MCP_PATH, headers={"Origin": ORIGIN})
        assert anonymous.status_code == 401
        challenge = anonymous.headers["www-authenticate"]
        assert challenge.startswith("Bearer ")
        assert f'resource_metadata="{BASE}{METADATA_PREFIX}/mcp"' in challenge
        assert 'error="invalid_request"' in challenge
        # Without these the browser reports a network fault and the client never reads the challenge.
        assert anonymous.headers["access-control-allow-origin"] == ORIGIN
        assert "WWW-Authenticate" in anonymous.headers["access-control-expose-headers"]
        assert anonymous.json()["error"]["code"] == "invalid_request"

        bad = client.post(MCP_PATH, headers={"Authorization": "Bearer not-a-known-token"})
        assert bad.status_code == 401
        assert bad.json()["error"]["code"] == "invalid_token"


def test_preflight_allows_the_headers_the_rest_cors_policy_does_not(mcp_settings):
    app = create_app(mcp_settings)
    requested = "authorization, mcp-protocol-version, mcp-name, mcp-method, x-tenant-id"
    with TestClient(app, base_url=BASE) as client:
        preflight = client.options(
            MCP_PATH,
            headers={
                "Origin": ORIGIN,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": requested,
            },
        )
        assert preflight.status_code == 200
        allowed = preflight.headers["access-control-allow-headers"].lower()
        for header in requested.split(", "):
            assert header in allowed, header
        assert "post" in preflight.headers["access-control-allow-methods"].lower()
        # A preflight carries no command: it is answered by CORS, never authenticated (§5.1).
        assert "www-authenticate" not in preflight.headers

        # The REST policy is deliberately narrower and the MCP branch sits outside it (§4.1).
        rest = client.options(
            "/api/v1/health",
            headers={
                "Origin": ORIGIN,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "mcp-protocol-version",
            },
        )
        assert "mcp-protocol-version" not in rest.headers.get("access-control-allow-headers", "").lower()


def test_host_and_origin_outside_the_allow_list_are_refused(mcp_settings):
    """The SDK owns this check, but only after auth: a valid token from a wrong host must still be refused."""
    headers = {**_auth(mcp_settings), "Content-Type": "application/json"}
    # The content type is validated first, so the request has to look like a real call to reach the host
    # check; without it the transport answers 400 and the case under test never runs.
    with TestClient(create_app(mcp_settings), base_url="http://evil.example") as client:
        assert client.post(MCP_PATH, headers=headers).status_code == 421
    with TestClient(create_app(mcp_settings), base_url=BASE) as client:
        forged = client.post(MCP_PATH, headers={**headers, "Origin": "http://evil.example"})
        assert forged.status_code == 403
        # A browser from that origin gets no CORS decoration, so it sees a refusal rather than a success.
        assert "access-control-allow-origin" not in forged.headers


def test_metadata_is_served_at_both_root_and_scoped_paths(mcp_settings):
    app = create_app(mcp_settings)
    with TestClient(app, base_url=BASE) as client:
        for path in (METADATA_PREFIX, f"{METADATA_PREFIX}/mcp"):
            response = client.get(path)
            assert response.status_code == 200, path
            document = response.json()
            # `resource` is configuration, never the Host header, so it cannot be pointed at an attacker.
            assert document["resource"] == mcp_settings.mcp_public_url.rstrip("/")
            assert document["scopes_supported"] == list(ALL_SCOPES)
            assert document["authorization_servers"] == []
            assert response.headers["cache-control"] == "public, max-age=60"
        wrong_method = client.post(f"{METADATA_PREFIX}/mcp")
        assert wrong_method.status_code == 405
        assert wrong_method.headers["allow"] == "GET, OPTIONS"


def test_metadata_paths_cover_a_public_prefix_deployment(mcp_settings):
    """A resource published at `/public/mcp` must answer its own path and the `/mcp` alias (§5.3)."""
    settings = mcp_settings.model_copy(update={"mcp_public_url": f"{BASE}/public/mcp"})
    assert metadata_paths(settings) == (
        METADATA_PREFIX,
        f"{METADATA_PREFIX}/mcp",
        f"{METADATA_PREFIX}/public/mcp",
    )


# --------------------------------------------------------------------------------------
# the scope challenge: MCP-AC-10 says a missing scope is a 403 a client can read
# --------------------------------------------------------------------------------------


def _credential(app: Any, monkeypatch: pytest.MonkeyPatch, *scopes: str) -> None:
    """Hand the stack a token that carries only these scopes.

    A development token stands in for a full grant, so it can never be the one that is missing a scope.
    Replacing what the authenticator returns is the narrowest seam available: every layer above reads the
    principal from the request state exactly as it would for a verified OIDC token.
    """
    principal = VerifiedPrincipal("local-dev", "dev-engineer", scopes)

    async def authenticate(_authorization: str | None, *, deadline: float | None = None) -> VerifiedPrincipal:
        return principal

    monkeypatch.setattr(app.state.mcp.authenticator, "authenticate", authenticate)


def _tools_call(tool: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    """A `tools/call` the SDK accepts: the envelope keys it requires live in `params._meta` (§5.2)."""
    return {
        "jsonrpc": "2.0",
        "id": 7,
        "method": "tools/call",
        "params": {
            "name": tool,
            "arguments": arguments or {},
            "_meta": {
                "io.modelcontextprotocol/protocolVersion": MODERN,
                "io.modelcontextprotocol/clientCapabilities": {},
            },
        },
    }


def _mcp_post(client: TestClient, settings: Settings, body: dict[str, Any], **headers: str) -> Any:
    return client.post(
        MCP_PATH,
        headers={
            **_auth(settings),
            "Origin": ORIGIN,
            "Content-Type": "application/json",
            "MCP-Protocol-Version": MODERN,
            **headers,
        },
        json=body,
    )


def test_a_tool_call_without_its_scope_is_a_403_carrying_that_scope(mcp_settings, monkeypatch):
    app = create_app(mcp_settings)
    _credential(app, monkeypatch, SCOPE_CONNECT)
    with TestClient(app, base_url=BASE) as client:
        refused = _mcp_post(client, mcp_settings, _tools_call("aita_run_test"))

    assert refused.status_code == 403, refused.text
    challenge = refused.headers["www-authenticate"]
    assert challenge.startswith("Bearer ")
    assert 'error="insufficient_scope"' in challenge
    # The scope the client has to go and ask for, named in the one place a client looks for it.
    assert 'scope="aita:run"' in challenge
    assert f'resource_metadata="{BASE}{METADATA_PREFIX}/mcp"' in challenge
    assert refused.json()["error"]["code"] == "insufficient_scope"
    # A browser client must be able to read the challenge, not just receive it (§5.1, MCP-AC-03).
    assert refused.headers["access-control-allow-origin"] == ORIGIN
    assert "WWW-Authenticate" in refused.headers["access-control-expose-headers"]


def test_a_resource_read_without_the_read_scope_is_refused_before_content(mcp_settings, monkeypatch):
    app = create_app(mcp_settings)
    _credential(app, monkeypatch, SCOPE_CONNECT)
    body = {
        "jsonrpc": "2.0",
        "id": 8,
        "method": "resources/read",
        "params": {
            "uri": "mcp://ai-test-agent/workflow",
            "_meta": {
                "io.modelcontextprotocol/protocolVersion": MODERN,
                "io.modelcontextprotocol/clientCapabilities": {},
            },
        },
    }
    with TestClient(app, base_url=BASE) as client:
        refused = _mcp_post(client, mcp_settings, body)

    assert refused.status_code == 403, refused.text
    assert 'scope="aita:read"' in refused.headers["www-authenticate"]
    assert refused.text == refused.text[:2000]


def test_the_scope_decision_reads_the_body_and_not_the_header_it_was_offered(mcp_settings, monkeypatch):
    """`Mcp-Name` is a mirror the client writes, so it cannot be what decides what a token must carry."""
    app = create_app(mcp_settings)
    _credential(app, monkeypatch, SCOPE_CONNECT, SCOPE_READ)
    with TestClient(app, base_url=BASE) as client:
        # The header claims a read-only tool while the body asks for the one that needs `aita:run`.
        refused = _mcp_post(
            client, mcp_settings, _tools_call("aita_run_test"), **{"Mcp-Name": "aita_get_context"}
        )

    assert refused.status_code == 403, refused.text
    assert 'scope="aita:run"' in refused.headers["www-authenticate"]


def test_the_same_call_reaches_the_tool_once_the_token_carries_the_scope(mcp_settings, monkeypatch):
    """The pass-through side of the same rule, with a body the SDK accepts.

    Without this the 403 cases could be green for the wrong reason: refusing a call the transport would
    have rejected anyway proves nothing about a well-formed one getting through.
    """
    app = create_app(mcp_settings)
    _credential(app, monkeypatch, SCOPE_CONNECT)
    mirrors = {"Mcp-Method": "tools/call", "Mcp-Name": "aita_get_context"}
    with TestClient(app, base_url=BASE) as client:
        reached = _mcp_post(client, mcp_settings, _tools_call("aita_get_context"), **mirrors)

    assert reached.status_code == 200, reached.text
    body = reached.json()
    assert "error" not in body, body
    assert body["result"]["structuredContent"]["ok"] is True, body


def test_a_body_this_layer_cannot_read_is_the_sdks_to_answer(mcp_settings, monkeypatch):
    """Unparseable JSON is a protocol failure, and a scope guess must not dress itself up as one."""
    app = create_app(mcp_settings)
    _credential(app, monkeypatch, SCOPE_CONNECT)
    with TestClient(app, base_url=BASE) as client:
        response = client.post(
            MCP_PATH,
            headers={**_auth(mcp_settings), "Content-Type": "application/json", "MCP-Protocol-Version": MODERN},
            content=b'{"jsonrpc": "2.0", "method": "tools/call", ',
        )

    assert response.status_code != 403, response.text
    assert "insufficient_scope" not in response.headers.get("www-authenticate", "")
    answer = response.json()
    # The SDK's own parse refusal, at its own status: this layer abstained rather than guessing.
    assert answer["error"]["code"] == -32700, answer


async def test_a_streamed_body_is_still_judged_and_still_arrives(mcp_settings):
    """The peek may not eat the call: the bytes it read have to reach the app unchanged.

    Chunked at an arbitrary offset, because a single-message body would never show a replay that dropped
    the tail, which is the failure this path can actually have.
    """
    scopes = {"aita_run_test": SCOPE_RUN}
    body = json.dumps(_tools_call("aita_run_test")).encode("utf-8")
    head, tail = body[:11], body[11:]

    def _receive(chunks: list[bytes]):
        queue: list[dict[str, Any]] = [
            {"type": "http.request", "body": chunk, "more_body": index < len(chunks) - 1}
            for index, chunk in enumerate(chunks)
        ]

        async def _next() -> dict[str, Any]:
            return queue.pop(0) if queue else {"type": "http.disconnect"}

        return _next

    def _scope(principal: VerifiedPrincipal) -> dict[str, Any]:
        return {
            "type": "http",
            "method": "POST",
            "path": MCP_PATH,
            "headers": [(b"content-type", b"application/json")],
            "state": {PRINCIPAL_STATE_KEY: principal, REQUEST_ID_STATE_KEY: "req_peek"},
        }

    forwarded: list[bytes] = []

    async def downstream(_scope_arg: dict[str, Any], receive: Callable, _send: Callable) -> None:
        while True:
            message = await receive()
            if message["type"] != "http.request":
                return
            forwarded.append(message.get("body") or b"")
            if not message.get("more_body", False):
                return

    started: list[dict[str, Any]] = []

    async def record(message: dict[str, Any]) -> None:
        started.append(message)

    gate = McpScopeGateMiddleware(downstream, scopes=scopes, settings=mcp_settings)

    await gate(
        _scope(VerifiedPrincipal("local-dev", "dev-engineer", (SCOPE_CONNECT, SCOPE_READ))),
        _receive([head, tail]),
        record,
    )
    assert started[0]["status"] == 403
    assert forwarded == []

    started.clear()
    await gate(
        _scope(VerifiedPrincipal("local-dev", "dev-engineer", (SCOPE_CONNECT, SCOPE_RUN))),
        _receive([head, tail]),
        record,
    )
    assert started == []
    assert b"".join(forwarded) == body


async def test_a_body_larger_than_the_peek_budget_reaches_the_transport_that_judges_it(mcp_settings):
    """Giving up on the scope guess has to be silent and complete: every byte, no opinion."""
    narrow = mcp_settings.model_copy(update={"mcp_max_request_bytes": 24})
    body = json.dumps(_tools_call("aita_run_test")).encode("utf-8")
    assert len(body) > narrow.mcp_max_request_bytes
    forwarded: list[bytes] = []
    answered: list[dict[str, Any]] = []

    async def downstream(_scope_arg: dict[str, Any], receive: Callable, _send: Callable) -> None:
        while True:
            message = await receive()
            if message["type"] != "http.request":
                return
            forwarded.append(message.get("body") or b"")
            if not message.get("more_body", False):
                return

    async def record(message: dict[str, Any]) -> None:
        answered.append(message)

    gate = McpScopeGateMiddleware(downstream, scopes={"aita_run_test": SCOPE_RUN}, settings=narrow)
    scope = {
        "type": "http",
        "method": "POST",
        "path": MCP_PATH,
        "headers": [(b"content-type", b"application/json")],
        "state": {
            PRINCIPAL_STATE_KEY: VerifiedPrincipal("local-dev", "dev-engineer", (SCOPE_CONNECT,)),
            REQUEST_ID_STATE_KEY: "req_oversize",
        },
    }
    queue: list[dict[str, Any]] = [
        {"type": "http.request", "body": body[:30], "more_body": True},
        {"type": "http.request", "body": body[30:], "more_body": False},
    ]

    async def receive() -> dict[str, Any]:
        return queue.pop(0) if queue else {"type": "http.disconnect"}

    await gate(scope, receive, record)
    assert answered == []
    assert b"".join(forwarded) == body


def test_readiness_refuses_to_call_a_stopped_transport_ready(mcp_settings):
    app = create_app(mcp_settings)
    client = TestClient(app, base_url=BASE)
    # No lifespan ran, so the manager was never entered: this must not read as ready.
    response = client.get("/api/v1/mcp/readiness")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "unavailable"
    assert body["components"]["transport"] == "stopped"
    assert body["components"]["auth"] == "initialised"
    # Nothing was sampled, so every dependency that costs a round trip is unknown, not healthy - including
    # the database, which the probe reads from the sample rather than with a query of its own (§13.5).
    assert body["components"]["database"] == "stale"
    assert body["components"]["schema"] == "stale"
    assert body["components"]["limiter"] == "stale"
    assert body["capabilities"]["submission_available"] is False
    assert body["capability_reasons"]["submission"] == "database_unavailable"
    assert body["capability_reasons"]["execution"] == "no_live_worker"
    # A call arriving in this state is refused, not half-served by an un-initialised manager.
    refused = client.post(MCP_PATH, headers=_auth(mcp_settings))
    assert refused.status_code == 503
    assert refused.headers["retry-after"] == "1"
    assert refused.json()["error"] == {"code": "dependency_unavailable", "message": "MCP is starting or stopping"}


def test_lifespan_starts_and_stops_the_manager_once(mcp_settings):
    app = create_app(mcp_settings)
    bundle = app.state.mcp
    assert bundle.running is False
    with TestClient(app, base_url=BASE) as client:
        assert bundle.running is True
        assert client.get("/api/v1/mcp/readiness").json() == {
            "status": "ready",
            "components": {
                "transport": "running",
                "auth": "initialised",
                "database": "ok",
                "schema": "ok",
                # Only a development deployment can answer this: production is refused at startup without a
                # limiter Redis, so "not required" is never a production statement (§12.2).
                "limiter": "not_required",
            },
            "capabilities": {
                "submission_available": True,
                "execution_available": False,
                "background_available": False,
            },
            "capability_reasons": {
                "submission": "available",
                "execution": "no_live_worker",
                "background": "no_live_worker",
            },
        }
    assert bundle.running is False


async def test_double_start_is_refused_and_shutdown_releases_the_stack(mcp_settings):
    app = create_app(mcp_settings)
    bundle = app.state.mcp
    await bundle.start()
    try:
        with pytest.raises(RuntimeError):
            await bundle.start()
        assert bundle.running is True
    finally:
        await bundle.stop()
    assert bundle.running is False
    assert bundle.services.closed is True
    # The SDK's task group cannot be re-entered, so the bundle says so instead of failing later.
    with pytest.raises(RuntimeError, match="cannot be restarted"):
        await bundle.start()
    # Everything after shutdown is refused by the dispatcher, before the manager is ever consulted.
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url=BASE) as http:
        response = await http.post(MCP_PATH, headers=_auth(mcp_settings))
        assert response.status_code == 503


# --------------------------------------------------------------------------------------
# the switch stays off, and the REST contract is unchanged when it is on
# --------------------------------------------------------------------------------------


def test_disabled_mcp_adds_no_bundle_no_routes_and_no_threads(database, tmp_path):
    settings = _mcp_settings(database, tmp_path, mcp_enabled=False)
    app = create_app(settings)
    assert not hasattr(app.state, "mcp")
    with TestClient(app, base_url=BASE) as client:
        assert client.get("/api/v1/mcp/readiness").status_code == 404
        assert client.post(MCP_PATH, headers=_auth(settings)).status_code == 404
        assert client.get(f"{METADATA_PREFIX}/mcp").status_code == 404


def test_rest_error_envelopes_are_unchanged_by_the_mcp_mount(mcp_settings, database, tmp_path):
    """The dispatcher wraps the whole app, so the REST 404/405 contract is what has to be re-proved."""
    enabled = create_app(mcp_settings)
    disabled = create_app(_mcp_settings(database, tmp_path, mcp_enabled=False))
    calls = [("GET", "/api/v1/does-not-exist"), ("POST", "/api/v1/health"), ("PATCH", "/api/v1/projects/x")]
    with TestClient(enabled, base_url=BASE) as on, TestClient(disabled, base_url=BASE) as off:
        for method, path in calls:
            first, second = on.request(method, path), off.request(method, path)
            assert first.status_code == second.status_code, path
            body, other = first.json()["error"], second.json()["error"]
            # The header and the body carry the same correlation id, and only it differs between runs.
            assert first.headers["x-request-id"] == body.pop("request_id"), path
            assert other.pop("request_id"), path
            assert body == other, path


# --------------------------------------------------------------------------------------
# the response boundary, pinned against the locked SDK's own result shape
# --------------------------------------------------------------------------------------


def test_response_budget_counts_both_channels():
    from backend.app.mcp.schemas import Envelope, call_tool_result, oversized, serialized_size

    payload = {"report": "x" * 8192}
    result = call_tool_result(Envelope.success("req_1", payload))
    total = serialized_size(result)
    # The same document is carried twice, so a budget judged on the text channel alone would allow double.
    assert total > 2 * len(json.dumps(payload))
    assert oversized(result, "req_1", budget=total) is result

    refusal = oversized(result, "req_1", budget=total - 1)
    assert refusal.is_error is True
    error = refusal.structured_content["error"]
    assert error["code"] == "RESULT_TOO_LARGE"
    assert error["next_action"] == "narrow_request"
    assert error["details"] == {"limit_bytes": total - 1, "actual_bytes": total}
    # A refusal has to fit the budget it is enforcing.
    assert serialized_size(refusal) <= total - 1


def test_the_server_refuses_to_build_tools_that_are_not_registered():
    with pytest.raises(ValueError, match="unknown MCP tools"):
        build_mcp_server(Settings(), services=None, tool_names=("no_such_tool",))  # type: ignore[arg-type]
