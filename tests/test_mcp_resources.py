"""M2: the three static specification resources (§7.1).

A document is the only part of this plane a client reads without naming a tenant, so the interesting
failures are the ones a live protocol shows and a unit call hides: whether the server advertises a
resource capability at all, whether a missing scope is refused rather than answered with an empty body,
whether the workflow text promises a tool this build never registered, and whether the three documents
stay what §7.1 says they are - help, holding no business data, leaving no audit row.

The content assertions are deliberately indirect. They ask the documents to agree with the contracts the
code already enforces (`STEP_CLASSES`, the IR literals, the two error enums, the tool registry), because
a test that repeated the prose would only prove the prose had been typed twice.
"""

from __future__ import annotations

import pytest
from backend.app.config import Settings
from backend.app.db.models import AuditLog
from backend.app.ir.models import STEP_CLASSES, Condition, LocatorCandidate
from backend.app.mcp import resources
from backend.app.mcp.auth import SCOPE_CONNECT, VerifiedPrincipal
from backend.app.mcp.errors import AdapterCode, NextAction
from backend.app.mcp.server import McpServices, available_tool_names
from mcp.client.client import Client
from mcp.shared.exceptions import MCPError
from sqlalchemy import func, select

from .mcp_live import (
    DEFAULT_MARKDOWN,
    OPEN,
    GateContext,
    gate,
    live_session,
    live_settings,
    moment,
    new_case,
    new_project,
    seed_workspace,
)

ALL_URIS = (resources.DSL_URI, resources.ERRORS_URI, resources.WORKFLOW_URI)

#: The marker the workflow text puts on an operation this build has not registered (§7.1).
NOT_ADVERTISED = "_(not advertised by this build)_"


@pytest.fixture
def mcp_settings(database, tmp_path) -> Settings:
    settings = live_settings(database, tmp_path)
    seed_workspace(database, settings)
    return settings


async def _read(client: Client, uri: str) -> str:
    """One document as its text, with the wire facts that make the answer a document checked."""
    result = await client.read_resource(uri)
    contents = result.contents
    assert len(contents) == 1, contents
    content = contents[0]
    assert content.uri == uri
    assert content.mime_type == resources.DOC_MIME_TYPE
    assert content.text is not None
    return content.text


async def test_the_three_documents_are_advertised_and_nothing_else(mcp_settings) -> None:
    """§7.1 - three static resources, no dynamic URIs and no prompts."""
    async with live_session(mcp_settings) as client:
        listed = (await client.list_resources()).resources
        assert [item.uri for item in listed] == list(ALL_URIS)
        for item in listed:
            assert item.name
            assert item.title
            assert item.description
            assert item.mime_type == resources.DOC_MIME_TYPE
        # A template would be a dynamic business resource, which §7.1 withholds on purpose: one fact
        # about a case must not live behind two permission systems.
        assert (await client.list_resource_templates()).resource_templates == []


async def test_the_server_advertises_the_resource_capability(mcp_settings) -> None:
    """A client that cannot see `resources` in the handshake never asks, so the documents would be inert."""
    async with live_session(mcp_settings) as client:
        capabilities = client.server_capabilities
        assert capabilities.resources is not None


async def test_each_document_is_served_whole_under_its_build_bound(mcp_settings) -> None:
    async with live_session(mcp_settings) as client:
        for uri in ALL_URIS:
            text = await _read(client, uri)
            assert text == resources.document_for(uri)
            # Served whole, or not at all: a truncated document would read as the platform's contract.
            assert len(text.encode("utf-8")) <= resources.MAX_DOC_BYTES
            assert text.startswith("# ")


def test_an_oversized_document_is_a_build_fault_not_a_truncated_one(monkeypatch: pytest.MonkeyPatch) -> None:
    """§7.1 - the bound is enforced where the document is rendered, so a long one cannot ship half-read.

    The three documents are well under it today, which is exactly why the guard needs its own case: an
    assertion that each rendered text is short proves nothing about the code that checks the size.
    """
    oversized = resources.ResourceSpec(
        uri=resources.DSL_URI,
        name="aita-dsl",
        title="Case authoring DSL",
        description="Deliberately too long.",
        render=lambda: "x" * (resources.MAX_DOC_BYTES + 1),
    )
    monkeypatch.setattr(resources, "resource_specs", lambda: (oversized,))

    with pytest.raises(AssertionError, match="over the"):
        resources.document_for(resources.DSL_URI)


async def test_the_dsl_document_agrees_with_the_compiler_it_describes(mcp_settings) -> None:
    dsl = await _text(mcp_settings, resources.DSL_URI)
    for name in STEP_CLASSES:
        assert f"`{name}`" in dsl
    # An action the IR would reject is the mistake this document is not allowed to make.
    for invented in ("hover", "scroll", "drag", "press"):
        assert f"`{invented}`" not in dsl
    for kind in _literals(Condition, "kind"):
        assert f"`{kind}`" in dsl
    for strategy in _literals(LocatorCandidate, "strategy"):
        assert f"`{strategy}`" in dsl


async def test_the_error_document_covers_every_code_the_adapter_can_answer_with(mcp_settings) -> None:
    errors = await _text(mcp_settings, resources.ERRORS_URI)
    for code in AdapterCode:
        assert f"`{code.value}`" in errors, code
    for action in NextAction:
        assert f"`{action.value}`" in errors, action
    for code in resources.RUNTIME_CODES:
        assert f"`{code.value}`" in errors, code


async def test_the_workflow_document_advertises_only_what_this_build_has(mcp_settings) -> None:
    """The one document that names tools has to agree with `tools/list`, in both directions (§6.2)."""
    workflow = await _text(mcp_settings, resources.WORKFLOW_URI)
    lines = {name: _line_for(workflow, name) for name, _purpose in resources.WORKFLOW}
    registered = set(available_tool_names())
    assert registered, "an empty registry would mark every step unavailable and prove nothing"
    for name in registered:
        # A tool that can be called but is documented nowhere is the direction this catches first.
        assert lines[name], name
        assert not lines[name].endswith(NOT_ADVERTISED), name
    for name, _purpose in resources.WORKFLOW:
        if name in registered:
            continue
        assert lines[name].endswith(NOT_ADVERTISED), name


def _line_for(document: str, name: str) -> str:
    """The one workflow step that names an operation, or nothing if the document never lists it.

    Only the step list is searched: the rules below it mention tools by name as prose, and a step is what
    carries the "not advertised" marker.
    """
    matches = [line for line in document.splitlines() if line.startswith("- `") and f"`{name}`" in line]
    assert len(matches) <= 1, matches
    return matches[0] if matches else ""


async def test_a_document_holds_no_tenant_project_or_case_content(database, tmp_path) -> None:
    """§7.1 - the resources are help. Nothing here may read a row, so no row can appear in them."""
    settings = live_settings(database, tmp_path)
    workspace = seed_workspace(database, settings)
    project_id = new_project(database, workspace, "acme-payments", policy=OPEN, at=moment(1))
    ids = new_case(database, workspace, project_id, "结清尾款回归", at=moment(2))

    async with live_session(settings) as client:
        for uri in ALL_URIS:
            text = await _read(client, uri)
            for value in (workspace["tenant_id"], project_id, ids["case_id"], ids["revision_id"]):
                assert value not in text
            assert "acme-payments" not in text
            assert "结清尾款回归" not in text
            assert DEFAULT_MARKDOWN not in text
            assert "dev-engineer" not in text


async def test_a_resource_read_leaves_no_audit_row(mcp_settings, database) -> None:
    """§11 - the audit trail records content a caller was allowed to see; a help page decided nothing."""
    with database.session() as session:
        before = session.scalar(select(func.count()).select_from(AuditLog))
    async with live_session(mcp_settings) as client:
        for uri in ALL_URIS:
            await _read(client, uri)
    with database.session() as session:
        after = session.scalar(select(func.count()).select_from(AuditLog))
    assert after == before


async def test_an_unknown_uri_is_a_protocol_refusal_not_an_empty_document(mcp_settings) -> None:
    async with live_session(mcp_settings) as client:
        with pytest.raises(MCPError) as refused:
            await client.read_resource("aita://docs/dsl/2.0")
    assert refused.value.code != 0
    # A version this build does not publish is absent, not served as the 1.0 text under a new name.
    assert "1.0" not in str(refused.value.message)


# --------------------------------------------------------------------------------------
# the gate: the scope floor a document earns
# --------------------------------------------------------------------------------------


async def test_a_document_without_the_read_scope_is_refused_before_it_is_rendered(mcp_settings) -> None:
    services = McpServices(mcp_settings)
    middleware, call_next, reached = gate(services)
    try:
        ctx = GateContext(
            VerifiedPrincipal("local-dev", "dev-engineer", (SCOPE_CONNECT,)),
            {"uri": resources.DSL_URI},
            method="resources/read",
        )
        with pytest.raises(MCPError) as refused:
            await middleware(ctx, call_next)
        assert reached == []
        assert refused.value.code == -32600
        assert refused.value.data["required_scopes"] == ["aita:read"]
    finally:
        await services.close()


async def test_listing_resources_needs_no_more_than_the_connect_floor(mcp_settings) -> None:
    """§5.3 - only `tools/call` and `resources/read` carry an extra scope; a list discloses no content."""
    services = McpServices(mcp_settings)
    middleware, call_next, reached = gate(services)
    try:
        ctx = GateContext(VerifiedPrincipal("local-dev", "dev-engineer", ()), {}, method="resources/list")
        assert await middleware(ctx, call_next) == "reached"
        assert reached == ["handler"]
    finally:
        await services.close()


async def test_an_unverifiable_credential_is_refused_on_a_resource_read_too(mcp_settings) -> None:
    """The gate answers for the caller before it answers for the document, or a name would be checked."""
    services = McpServices(mcp_settings)
    middleware, call_next, reached = gate(services)
    try:
        ctx = GateContext(None, {"uri": resources.DSL_URI}, method="resources/read")  # type: ignore[arg-type]
        with pytest.raises(MCPError) as refused:
            await middleware(ctx, call_next)
        assert reached == []
        assert refused.value.data["code"] == "UNAUTHENTICATED"
    finally:
        await services.close()


# --------------------------------------------------------------------------------------
# the document text, checked against the contracts rather than against a copy of itself
# --------------------------------------------------------------------------------------


async def _text(settings: Settings, uri: str) -> str:
    async with live_session(settings) as client:
        return await _read(client, uri)


def _literals(model, field: str) -> tuple[str, ...]:
    return resources.literal_values(model, field)
