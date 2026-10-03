"""M2: the discovery reads over the real protocol - bounded SQL, keyset pages and policy projection.

The harness these cases run on lives in `mcp_live.py`; what is here is the seeding the three discovery
tools need and the assertions themselves. They are also the only reads that must answer while a project's
MCP gate is shut (§6.2), so the gate's two faces are both covered: a closed project still shows its id,
and refuses everything else.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

import pytest
from backend.app.config import Settings
from backend.app.db.models import Environment, EnvironmentRevision
from backend.app.domain.enums import Role
from backend.app.mcp.server import McpServices, available_tool_names, build_mcp_server

from .mcp_live import (
    CLOSED,
    OPEN,
    error_body,
    live_session,
    live_settings,
    moment,
    new_project,
    ok_data,
    other_tenant,
    page_items,
    seed_workspace,
)


@pytest.fixture
def mcp_settings(database: Any, tmp_path: Any) -> Settings:
    return live_settings(database, tmp_path)


@pytest.fixture
def mcp_workspace(database: Any, mcp_settings: Settings) -> dict[str, str]:
    return seed_workspace(database, mcp_settings)


def new_environment(
    database: Any,
    workspace: dict[str, str],
    project_id: str,
    name: str,
    *,
    at: datetime,
    revision: bool = True,
    archived: bool = False,
) -> str:
    """One environment, published or draft, with the configuration a read must never repeat."""
    from backend.app.db.base import new_id

    environment_id = new_id()
    with database.session() as session:
        environment = Environment(
            id=environment_id,
            tenant_id=workspace["tenant_id"],
            project_id=project_id,
            environment_name=name,
            row_version=3,
            created_at=at,
            updated_at=at,
            archived_at=at if archived else None,
        )
        session.add(environment)
        session.flush()
        if revision:
            record = EnvironmentRevision(
                id=new_id(),
                tenant_id=workspace["tenant_id"],
                project_id=project_id,
                environment_id=environment_id,
                version=7,
                config={
                    "base_url": "https://super-secret-host.example",
                    "allowed_domains": ["super-secret-host.example"],
                    "variables": {"password": "hunter2"},
                },
                secret_bindings={"login_password": "secret-1"},
                digest="sha256:" + "a" * 64,
            )
            session.add(record)
            session.flush()
            environment.current_revision_id = record.id
        session.commit()
    return environment_id



# --------------------------------------------------------------------------------------
# aita_get_context
# --------------------------------------------------------------------------------------


async def test_get_context_lists_the_callers_tenants_before_one_is_selected(mcp_settings, mcp_workspace):
    async with live_session(mcp_settings) as client:
        data = await ok_data(client, "aita_get_context", {})

    assert data["actor_id"]
    # No tenant was selected, so the answer says so rather than inventing a default (§5.1).
    assert data["tenant_id"] is None
    assert [item["name"] for item in data["items"]] == ["default"]
    item = data["items"][0]
    assert item["tenant_id"] == mcp_workspace["tenant_id"]
    # The role is the platform's own value, not a name this adapter invented for the wire.
    assert item["role"] == Role.ENGINEER.value
    assert data["capabilities"]["tenant_selected"] is False
    assert data["capabilities"]["entrypoint"] == "mcp"
    assert data["capabilities"]["credential_scopes"] == ["aita:connect", "aita:read", "aita:write", "aita:run"]
    # Nothing has been selected yet, so no tenant-wide authority is claimed (§5.1).
    assert data["capabilities"]["tenant_role"] is None
    assert data["capabilities"]["abilities"] == []
    assert data["limits"]["page_size_max"] == 100
    assert data["limits"]["evidence_download"] is False
    assert data["limits"]["prompts"] is False


async def test_get_context_with_a_tenant_shows_the_projects_and_their_permissions(mcp_settings, mcp_workspace):
    async with live_session(mcp_settings) as client:
        data = await ok_data(client, "aita_get_context", {"tenant_id": mcp_workspace["tenant_id"]})

    assert data["tenant_id"] == mcp_workspace["tenant_id"]
    assert data["capabilities"]["tenant_selected"] is True
    assert data["capabilities"]["tenant_role"] == Role.ENGINEER.value
    item = data["items"][0]
    assert item["project_id"] == mcp_workspace["project_id"]
    assert item["mcp_enabled"] is True
    assert item["name"] == "demo"
    assert item["role"] == Role.ENGINEER.value
    assert "case_read" in item["permissions"]
    # A specialist grant rides along with the role's own permissions, and only for that project (§14.1).
    assert "human_control" in item["permissions"]
    assert data["capabilities"]["grants"] == {
        mcp_workspace["project_id"]: ["human_control", "sensitive_artifact_read"]
    }


async def test_a_closed_project_is_listed_by_id_alone(mcp_settings, database, mcp_workspace):
    """`enabled=false` still counts as discovery: the client has to be able to tell why it was refused."""
    closed = new_project(database, mcp_workspace, "closed-project", policy=CLOSED, at=moment(10))

    async with live_session(mcp_settings) as client:
        context = await ok_data(client, "aita_get_context", {"tenant_id": mcp_workspace["tenant_id"]})
        projects = await ok_data(client, "aita_list_projects", {"tenant_id": mcp_workspace["tenant_id"]})

    by_id = {item["project_id"]: item for item in context["items"]}
    assert by_id[closed]["mcp_enabled"] is False
    # Not empty strings: an absent key is the difference between "hidden" and "there is no name" (§6.6).
    assert "name" not in by_id[closed]
    assert "display_name" not in by_id[closed]
    assert "description" not in by_id[closed]
    # The caller's own authority is still its own, even for a project MCP may not serve (§5.4).
    assert by_id[closed]["permissions"]
    listed = {item["project_id"]: item for item in projects["items"]}
    assert listed[closed] == {"project_id": closed, "mcp_enabled": False}
    assert listed[mcp_workspace["project_id"]]["name"] == "demo"


async def test_get_context_refuses_a_tenant_the_caller_is_not_in(mcp_settings, mcp_workspace):
    async with live_session(mcp_settings) as client:
        error = await error_body(client, "aita_get_context", {"tenant_id": "00000000-0000-4000-8000-000000000000"})

    assert error["code"] == "FORBIDDEN"


# --------------------------------------------------------------------------------------
# aita_list_projects: ordering, paging and the cursor contract
# --------------------------------------------------------------------------------------


async def test_projects_page_newest_first_and_stop_without_an_empty_follow_up(mcp_settings, database, mcp_workspace):
    newest = new_project(database, mcp_workspace, "newest", policy=OPEN, at=moment(30))
    middle = new_project(database, mcp_workspace, "middle", policy=OPEN, at=moment(20))
    oldest = new_project(database, mcp_workspace, "oldest", policy=CLOSED, at=moment(10))
    seed = mcp_workspace["project_id"]

    async with live_session(mcp_settings) as client:
        items, cursor = await page_items(
            client, "aita_list_projects", {"tenant_id": mcp_workspace["tenant_id"], "limit": 2}
        )
        assert [item["project_id"] for item in items] == [newest, middle]
        assert cursor

        items, cursor = await page_items(
            client, "aita_list_projects", {"tenant_id": mcp_workspace["tenant_id"], "limit": 2, "cursor": cursor}
        )
        assert [item["project_id"] for item in items] == [oldest, seed]
        # The last page says there is no last page; a cursor that yields nothing is not a bookmark (§11).
        assert cursor is None

    async with live_session(mcp_settings) as client:
        # Re-ask the first page with a wider `limit`: a changed page size is the same question, so the
        # cursor the narrow page minted has to continue the wide one rather than be refused.
        _, narrow = await page_items(
            client, "aita_list_projects", {"tenant_id": mcp_workspace["tenant_id"], "limit": 2}
        )
        wide, wide_cursor = await page_items(
            client, "aita_list_projects", {"tenant_id": mcp_workspace["tenant_id"], "limit": 100, "cursor": narrow}
        )
        assert [item["project_id"] for item in wide] == [oldest, seed]
        assert wide_cursor is None


async def test_rows_with_the_same_timestamp_page_in_id_order(mcp_settings, database, mcp_workspace):
    """`created_at DESC, id DESC` is the whole point of a keyset: two writes in one tick stay separable."""
    same_tick = moment(50)
    first = new_project(database, mcp_workspace, "twin-a", policy=OPEN, at=same_tick)
    second = new_project(database, mcp_workspace, "twin-b", policy=OPEN, at=same_tick)
    twins = sorted({first, second}, reverse=True)

    async with live_session(mcp_settings) as client:
        items, cursor = await page_items(
            client, "aita_list_projects", {"tenant_id": mcp_workspace["tenant_id"], "limit": 1}
        )
        assert [item["project_id"] for item in items] == [twins[0]]
        assert cursor

        items, _ = await page_items(
            client, "aita_list_projects", {"tenant_id": mcp_workspace["tenant_id"], "limit": 1, "cursor": cursor}
        )
        assert [item["project_id"] for item in items] == [twins[1]]


async def test_a_project_of_another_tenant_never_appears_in_the_page(mcp_settings, database, mcp_workspace):
    foreign_tenant, other_project = other_tenant(database, mcp_workspace, mcp_workspace["engineer_user_id"])

    async with live_session(mcp_settings) as client:
        mine = await ok_data(client, "aita_list_projects", {"tenant_id": mcp_workspace["tenant_id"], "limit": 100})
        theirs = await ok_data(client, "aita_list_projects", {"tenant_id": foreign_tenant, "limit": 100})

    assert other_project not in {item["project_id"] for item in mine["items"]}
    assert {item["project_id"] for item in theirs["items"]} == {other_project}


async def test_a_cursor_is_only_valid_for_the_query_that_minted_it(mcp_settings, database, mcp_workspace):
    new_project(database, mcp_workspace, "extra", policy=OPEN, at=moment(40))

    async with live_session(mcp_settings) as client:
        _items, cursor = await page_items(
            client, "aita_list_projects", {"tenant_id": mcp_workspace["tenant_id"], "limit": 1}
        )

        foreign_tenant, _ = other_tenant(database, mcp_workspace, mcp_workspace["engineer_user_id"])
        # Same kind, different tenant: the token is well formed and describes a page this call never asked
        # for, so continuing it would answer a question nobody asked (§11).
        cross_tenant = await error_body(
            client, "aita_list_projects", {"tenant_id": foreign_tenant, "limit": 1, "cursor": cursor}
        )
        assert cross_tenant["code"] == "VALIDATION_ERROR"
        assert cross_tenant["details"]["reason"] == "cursor_does_not_match"
        assert cross_tenant["next_action"] == "narrow_request"

        # A project cursor replayed against the environment page is a different keyset entirely.
        cross_kind = await error_body(
            client,
            "aita_list_environments",
            {"tenant_id": mcp_workspace["tenant_id"], "project_id": mcp_workspace["project_id"], "cursor": cursor},
        )
        assert cross_kind["details"]["reason"] == "cursor_does_not_match"

        # Tampering with the bytes is the same refusal, not a parse error escaping as INTERNAL.
        broken = await error_body(
            client,
            "aita_list_projects",
            {"tenant_id": mcp_workspace["tenant_id"], "limit": 1, "cursor": "not-a-cursor-at-all"},
        )
        assert broken["details"]["reason"] == "cursor_does_not_match"


async def test_arguments_outside_the_published_bounds_are_refused_before_any_query(mcp_settings, mcp_workspace):
    async with live_session(mcp_settings) as client:
        for limit in (0, 101):
            error = await error_body(
                client, "aita_list_projects", {"tenant_id": mcp_workspace["tenant_id"], "limit": limit}
            )
            assert error["code"] == "VALIDATION_ERROR"
            assert error["details"]["invalid_fields"] == ["limit"]

        empty = await error_body(client, "aita_list_projects", {"tenant_id": ""})
        assert empty["details"]["invalid_fields"] == ["tenant_id"]

        # A misspelled argument must not arrive as a missing one: the gate closes the schema the SDK
        # published open (§6.1).
        extra = await error_body(client, "aita_list_projects", {"tenant_id": mcp_workspace["tenant_id"], "limitt": 5})
        assert extra["details"]["invalid_fields"] == ["limitt"]


# --------------------------------------------------------------------------------------
# aita_list_environments
# --------------------------------------------------------------------------------------


async def test_list_environments_refuses_a_closed_project_even_when_no_content_was_asked_for(
    mcp_settings, database, mcp_workspace
):
    """§6.2: the gate is checked first, so there is no narrower read that slips past it."""
    closed = new_project(database, mcp_workspace, "closed-envs", policy=CLOSED, at=moment(60))

    async with live_session(mcp_settings) as client:
        error = await error_body(
            client, "aita_list_environments", {"tenant_id": mcp_workspace["tenant_id"], "project_id": closed}
        )
    assert error["code"] == "MCP_PROJECT_DISABLED"
    assert error["details"] == {"project_id": closed, "tool": "aita_list_environments", "mcp_enabled": False}
    # Nothing the caller can do next: the flag is an administrator's, and the message says whose.
    assert error["next_action"] == "none"


async def test_list_environments_answers_with_the_revision_to_run_and_never_the_config(
    mcp_settings, database, mcp_workspace
):
    # A project of its own, so the demo workspace's published `local` environment cannot join the page.
    project = new_project(database, mcp_workspace, "env-reads", policy=OPEN, at=moment(4))
    published = new_environment(database, mcp_workspace, project, "staging", at=moment(5))
    draft = new_environment(database, mcp_workspace, project, "unpublished", at=moment(6), revision=False)
    new_environment(database, mcp_workspace, project, "gone", at=moment(7), archived=True)

    async with live_session(mcp_settings) as client:
        data = await ok_data(
            client, "aita_list_environments", {"tenant_id": mcp_workspace["tenant_id"], "project_id": project}
        )

    # `created_at DESC, id DESC`: the newest row leads, and the archived one is not listed at all (§6.6).
    assert [item["environment_id"] for item in data["items"]] == [draft, published]
    by_name = {item["name"]: item for item in data["items"]}
    assert by_name["staging"]["current_revision_id"]
    assert by_name["staging"]["revision_version"] == 7
    assert by_name["staging"]["row_version"] == 3
    # Nullable is not the same as hidden: an unpublished environment really has no revision (§6.6).
    assert by_name["unpublished"]["current_revision_id"] is None
    assert by_name["unpublished"]["revision_version"] is None
    assert "gone" not in by_name

    serialized = json.dumps(data)
    for leak in ("base_url", "super-secret-host", "hunter2", "login_password", "secret_bindings", "allowed_domains"):
        assert leak not in serialized


async def test_environment_pages_are_bounded_by_the_project_in_the_cursor(mcp_settings, database, mcp_workspace):
    first_project = new_project(database, mcp_workspace, "first-project", policy=OPEN, at=moment(1))
    other = new_project(database, mcp_workspace, "second-project", policy=OPEN, at=moment(80))
    new_environment(database, mcp_workspace, first_project, "first-a", at=moment(11))
    new_environment(database, mcp_workspace, first_project, "first-b", at=moment(12))
    new_environment(database, mcp_workspace, other, "second-a", at=moment(13))

    async with live_session(mcp_settings) as client:
        listing = {"tenant_id": mcp_workspace["tenant_id"], "project_id": first_project, "limit": 1}
        items, after_first = await page_items(client, "aita_list_environments", listing)
        assert [item["name"] for item in items] == ["first-b"]
        assert after_first

        items, cursor = await page_items(client, "aita_list_environments", {**listing, "cursor": after_first})
        assert [item["name"] for item in items] == ["first-a"]
        assert cursor is None

        # The same cursor against the other project is refused: it names a position in a page of a
        # different project's rows.
        crossed = await error_body(
            client,
            "aita_list_environments",
            {"tenant_id": mcp_workspace["tenant_id"], "project_id": other, "limit": 1, "cursor": after_first},
        )
        assert crossed["details"]["reason"] == "cursor_does_not_match"


async def test_an_unknown_or_foreign_project_is_not_found_rather_than_empty(mcp_settings, database, mcp_workspace):
    foreign_tenant, other_project = other_tenant(database, mcp_workspace, mcp_workspace["engineer_user_id"])

    async with live_session(mcp_settings) as client:
        # Selecting the caller's own tenant but a project inside a different one: the row is not in this
        # tenant, so the answer is 404-shaped and reveals nothing (§5.1).
        error = await error_body(
            client,
            "aita_list_environments",
            {"tenant_id": mcp_workspace["tenant_id"], "project_id": other_project},
        )
        assert error["code"] == "NOT_FOUND"

        foreign = await error_body(
            client, "aita_list_environments", {"tenant_id": foreign_tenant, "project_id": mcp_workspace["project_id"]}
        )
        assert foreign["code"] == "NOT_FOUND"


# --------------------------------------------------------------------------------------
# one transaction per call
# --------------------------------------------------------------------------------------


async def test_a_read_holds_one_database_connection_at_a_time(tmp_path, database: Any, mcp_workspace) -> None:
    """The MCP pool is exactly the execution-slot count, so a call needing two connections would deadlock
    on its own pool rather than on the database (§4.4, §11)."""
    tight = live_settings(database, tmp_path, mcp_db_pool_size=1, mcp_max_inflight_total=1)
    async with live_session(tight) as client:
        data = await ok_data(client, "aita_list_projects", {"tenant_id": mcp_workspace["tenant_id"]})
    assert data["items"]


async def test_every_registered_tool_declares_its_scope_and_read_only_hints(mcp_settings) -> None:
    """Discovery is static: one scope, a fixed description, and the read-only hints (§5.4, §6.5).

    Building the server is part of the assertion. The SDK derives each tool's published argument schema
    from the handler signature at build time, so a signature it cannot turn into a schema would surface
    here as a broken tool list rather than as a client that sends arguments nobody declared.
    """
    services = McpServices(mcp_settings)
    try:
        _, specs = build_mcp_server(services.settings, services=services)
        assert set(specs) == set(available_tool_names())
        for name, spec in specs.items():
            assert spec.scope in {"aita:connect", "aita:read", "aita:write", "aita:run"}, name
            assert spec.description.strip(), name
        for name in ("aita_get_context", "aita_list_projects", "aita_list_environments"):
            assert specs[name].annotations.read_only_hint is True, name
            assert specs[name].annotations.open_world_hint is False, name
    finally:
        await services.close()
