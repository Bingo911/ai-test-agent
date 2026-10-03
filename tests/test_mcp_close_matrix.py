"""M2 and M3: the closed-project matrix for every tool, and the audit a content read owes (§6.2, §11, AC-39).

The design states this rule as a table, so it is tested as one: the rows are a dict keyed by tool name, and
the first assertion is that the dict still covers the registry. That is what makes the table worth having -
when a tool is added and nobody classifies it, the suite goes red instead of the new tool quietly doing
whatever it likes.

Two projects carry the load. One is closed with every flag off; the other is closed with the content flags
left switched on, the shape a legacy row really has. `enabled` outranks the content flags (§6.2), so both
must give the same answer, and a test that only tried the first would let a tool trust a stale flag.

The run under test belongs to the *closed* project on purpose: the matrix allows an existing execution to
be read there while a case read in the same project is refused, and only a seed that puts both in one
project can tell those two rows apart.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from backend.app.config import Settings
from backend.app.db.models import CompileArtifact, IdempotencyRecord, TestCase, TestExecution
from backend.app.domain.enums import CompileStatus
from backend.app.mcp.server import available_tool_names
from backend.app.repositories.platform import AuditRepository
from mcp.client.client import Client
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError

from .mcp_live import (
    CLOSED,
    DEFAULT_MARKDOWN,
    OPEN,
    SENTINELS,
    ask,
    call_tool,
    content_reads,
    error_body,
    failing,
    live_session,
    live_settings,
    moment,
    new_case,
    new_compile,
    new_execution,
    new_project,
    passing,
    revoke_subject,
    seed_workspace,
)

MINIMAL = "minimal"
REFUSED = "refused"
METADATA_ONLY = "metadata_only"
#: §6.2's last row: a legal cancel keeps working on a closed project, still behind its own permission check.
ALLOWED = "allowed"
OUTCOMES = (MINIMAL, REFUSED, METADATA_ONLY, ALLOWED)

#: §6.2's table, for the tools this build has. An unmapped tool is a build fault, not a gap in a test.
#: The command rows are the answer to a *new* intent; §6.2 gives them a second half - a successful
#: earlier intent with the same key still replays its receipt - which is `test_mcp_writes.py`'s business,
#: because only a call that already wrote a row can show the difference.
CLOSED_ROWS: dict[str, str] = {
    "aita_get_context": MINIMAL,
    "aita_list_projects": MINIMAL,
    "aita_list_environments": REFUSED,
    "aita_list_cases": REFUSED,
    "aita_get_case": REFUSED,
    "aita_create_case": REFUSED,
    "aita_add_case_revision": REFUSED,
    "aita_compile_case_revision": REFUSED,
    "aita_get_compilation": REFUSED,
    "aita_run_test": REFUSED,
    "aita_get_execution": MINIMAL,
    "aita_get_report": MINIMAL,
    "aita_get_execution_steps": METADATA_ONLY,
    "aita_cancel_execution": ALLOWED,
}

#: Closed, but carrying the content flags a project picked up before `enabled` meant anything (§5.5).
STALE_OPEN_FLAGS = {
    "enabled": False,
    "allow_case_content": True,
    "allow_report_details": True,
    "allow_server_ai": True,
}
POLICIES = {"all flags off": CLOSED, "closed with stale content flags": STALE_OPEN_FLAGS}

REPORT_OFF = dict(OPEN, allow_report_details=False)
#: The seed's artifact carries no IR document, so a run named against it cannot get as far as a digest
#: comparison - which is the point: `MCP_PROJECT_DISABLED` has to be the answer that arrives first.
RUN_DIGEST = "sha256:" + "b" * 64
STEP_METADATA = {"step_id", "step_no", "action", "status", "duration_ms", "error_code", "artifact_refs"}
GATED_STEP_FIELDS = frozenset({"description", "locator_attempts", "expected", "actual", "resume_phase"})

PROJECT_NAME = "closed-then-read"
CASE_NAME = "结清尾款回归"
#: Every free-text document the seed put inside this project and run, as the words the closed answers must
#: not contain: the case name, description and revision title, the Markdown body, a failed step's assertion
#: prose, an analysis note, and the four stored documents §11 keeps on the platform.
PROSE = (
    PROJECT_NAME,
    CASE_NAME,
    "Description of",
    "登录冒烟测试",
    "fill #username",
    "断言失败",
    "expected-",
    "actual-",
    "not a stable code",
    *SENTINELS,
)


@pytest.fixture
def mcp_settings(database: Any, tmp_path: Any) -> Settings:
    return live_settings(database, tmp_path)


@pytest.fixture
def mcp_workspace(database: Any, mcp_settings: Settings) -> dict[str, str]:
    return seed_workspace(database, mcp_settings)


def seeded(
    database: Any, workspace: dict[str, str], policy: dict[str, bool], *, name: str = PROJECT_NAME
) -> dict[str, Any]:
    """One project under `policy`, holding a case, a compile and a finished three-step run.

    Returns the arguments every tool needs (`tenant_id` included) rather than only the ids, because the
    matrix calls each tool with the least the schema allows and one seed has to serve all fourteen. The
    environment revision is the demo workspace's own: a run refused at the gate never reaches it.
    """
    project_id = new_project(database, workspace, name, policy=policy, at=moment(1))
    ids = new_case(database, workspace, project_id, CASE_NAME, at=moment(2))
    artifact = new_compile(
        database,
        workspace,
        project_id,
        ids["revision_id"],
        status=CompileStatus.SUCCEEDED.value,
        at=moment(3),
    )
    execution_id = new_execution(
        database,
        workspace,
        project_id,
        case_id=ids["case_id"],
        revision_id=ids["revision_id"],
        compile_artifact_id=artifact,
        at=moment(4),
        # The caller in these tests is the engineer the dev token resolves to, and an engineer may stop
        # their own runs - so the cancel row tests the gate's absence, not a permission it never had (§6.2).
        requested_by=workspace["engineer_user_id"],
        outcome="FAILED",
        error_code="assertion_failed",
        steps=(*passing(1), *failing(2, first=2)),
        analyses=({"revision": 2, "failure_type": "assertion_mismatch"},),
    )
    return {
        **ask(workspace),
        "project_id": project_id,
        "execution_id": execution_id,
        "environment_revision_id": workspace["environment_revision_id"],
        **ids,
        "artifact_id": artifact,
    }


def calls(rows: dict[str, Any], *, content: bool) -> dict[str, dict[str, Any]]:
    """One call per tool, each asking for the least the schema allows (§6.2).

    `content` decides only whether the two reads that *have* a content flag are asked to use it: the matrix
    refuses the case reads either way, and a test that tried only `include_markdown=true` would pass while
    the handler quietly answered the plain read.
    """
    return {
        "aita_get_context": ask(rows),
        "aita_list_projects": ask(rows),
        "aita_list_environments": ask(rows, project_id=rows["project_id"]),
        "aita_list_cases": ask(rows, project_id=rows["project_id"]),
        "aita_get_case": ask(rows, case_id=rows["case_id"], include_markdown=content),
        "aita_create_case": ask(
            rows,
            project_id=rows["project_id"],
            name=CASE_NAME,
            markdown=DEFAULT_MARKDOWN,
            idempotency_key="matrix-create",
        ),
        "aita_add_case_revision": ask(
            rows,
            case_id=rows["case_id"],
            markdown=DEFAULT_MARKDOWN,
            expected_row_version=4,
            idempotency_key="matrix-revise",
        ),
        "aita_compile_case_revision": ask(
            rows, revision_id=rows["revision_id"], idempotency_key="matrix-compile"
        ),
        "aita_get_compilation": ask(rows, revision_id=rows["revision_id"], include_ir=content),
        "aita_run_test": ask(
            rows,
            compile_artifact_id=rows["artifact_id"],
            expected_ir_digest=RUN_DIGEST,
            environment_revision_id=rows["environment_revision_id"],
            idempotency_key="matrix-run",
        ),
        "aita_get_execution": ask(rows, execution_id=rows["execution_id"]),
        "aita_get_report": ask(rows, execution_id=rows["execution_id"]),
        "aita_get_execution_steps": ask(rows, execution_id=rows["execution_id"], limit=20),
        "aita_cancel_execution": ask(rows, execution_id=rows["execution_id"], idempotency_key="matrix-cancel"),
    }


def items_for(data: dict[str, Any], project_id: str) -> dict[str, Any]:
    """The one row a discovery page carries for the closed project, out of the tenant's whole page."""
    rows = [item for item in data["items"] if item.get("project_id") == project_id]
    assert len(rows) == 1, data["items"]
    return rows[0]


async def answered(client: Client, tool: str, arguments: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """A successful answer, and the whole envelope as the wire saw it.

    The second half is what makes the leak assertions honest: `data` is only part of an envelope, and a
    field that escaped into `error` or into the text channel would go unnoticed otherwise.
    """
    envelope = await call_tool(client, tool, arguments)
    assert envelope["ok"] is True, envelope
    return envelope["data"], json.dumps(envelope, ensure_ascii=False)


def assert_no_prose(tool: str, wire: str) -> None:
    for marker in PROSE:
        assert marker not in wire, f"{tool} leaked {marker!r}"


# --------------------------------------------------------------------------------------
# the table itself
# --------------------------------------------------------------------------------------


def test_the_matrix_covers_every_registered_tool() -> None:
    """A tool nobody classified is the failure this table exists to catch (§6.2, AC-39)."""
    from backend.app.mcp import tools  # noqa: F401  - registering the tools is what fills the catalog

    assert set(CLOSED_ROWS) == set(available_tool_names())
    assert all(outcome in OUTCOMES for outcome in CLOSED_ROWS.values())


@pytest.mark.parametrize("label", sorted(POLICIES))
async def test_the_content_reads_refuse_however_they_are_asked_for(
    database: Any, mcp_settings: Settings, mcp_workspace: dict[str, str], label: str
) -> None:
    """§6.2: on a closed project the four content reads answer MCP_PROJECT_DISABLED, flag or no flag."""
    rows = seeded(database, mcp_workspace, POLICIES[label])
    refused = [tool for tool, outcome in CLOSED_ROWS.items() if outcome == REFUSED]

    async with live_session(mcp_settings) as client:
        for content in (True, False):
            for tool in refused:
                error = await error_body(client, tool, calls(rows, content=content)[tool])
                assert error["code"] == "MCP_PROJECT_DISABLED", (tool, content)
                assert error["retryable"] is False, tool
                assert error["next_action"] == "none", tool
                assert error["message"], tool
                assert_no_prose(tool, json.dumps(error, ensure_ascii=False))

    assert content_reads(database, rows["tenant_id"]) == []


@pytest.mark.parametrize("label", sorted(POLICIES))
async def test_the_discovery_reads_answer_minimally_and_say_the_project_is_closed(
    database: Any, mcp_settings: Settings, mcp_workspace: dict[str, str], label: str
) -> None:
    """§6.2: `get_context` and `list_projects` keep working - the closed project is an id and a gate state."""
    rows = seeded(database, mcp_workspace, POLICIES[label])

    async with live_session(mcp_settings) as client:
        context, wire = await answered(client, "aita_get_context", calls(rows, content=False)["aita_get_context"])
        authority = items_for(context, rows["project_id"])
        assert authority["mcp_enabled"] is False
        assert set(authority) == {"project_id", "mcp_enabled", "permissions", "role"}
        assert authority["permissions"]
        assert_no_prose("aita_get_context", wire)

        page, wire = await answered(client, "aita_list_projects", calls(rows, content=False)["aita_list_projects"])
        listed = items_for(page, rows["project_id"])
        assert listed == {"project_id": rows["project_id"], "mcp_enabled": False}
        assert_no_prose("aita_list_projects", wire)

        # The same page carries the demo project with its names, so what is missing above is the gate and
        # not the tenant, the tool, or this caller's rights.
        open_row = items_for(page, mcp_workspace["project_id"])
        assert open_row["mcp_enabled"] is True
        assert open_row["name"] == "demo"

        # A closed project changes nothing about the deployment's own bounds, which is what a client plans a page from.
        assert context["capabilities"]["tenant_selected"] is True
        assert context["capabilities"]["credential_scopes"]
        assert context["limits"]["page_size_max"] == 100
        assert context["limits"]["mcp_policy_flags"] == [
            "enabled",
            "allow_case_content",
            "allow_report_details",
            "allow_server_ai",
        ]


@pytest.mark.parametrize("label", sorted(POLICIES))
async def test_existing_runs_still_answer_with_state_codes_and_links(
    database: Any, mcp_settings: Settings, mcp_workspace: dict[str, str], label: str
) -> None:
    """§6.2: a closed project's finished run is readable - statistics, ids, digests, links, no prose."""
    rows = seeded(database, mcp_workspace, POLICIES[label])
    arguments = calls(rows, content=False)

    async with live_session(mcp_settings) as client:
        execution, wire = await answered(client, "aita_get_execution", arguments["aita_get_execution"])
        assert execution["details_available"] is False
        assert execution["status"] == "FINISHED"
        assert execution["terminal"] is True
        assert execution["recommended_poll_after_ms"] is None
        assert execution["error_code"] == "assertion_failed"
        assert execution["step_counts"] == {"PASSED": 1, "FAILED": 2}
        assert execution["compile_artifact_id"] == rows["artifact_id"]
        assert execution["ir_digest"].startswith("sha256:")
        assert execution["links"]["report"].endswith(f"/report/{rows['execution_id']}")
        assert_no_prose("aita_get_execution", wire)

        report, wire = await answered(client, "aita_get_report", arguments["aita_get_report"])
        assert report["details_available"] is False
        assert "failure_summaries" not in report
        assert report["failure_step_ids"] == ["s2", "s3"]
        assert report["error_code"] == "assertion_failed"
        assert report["base_report_ready"] is True
        assert_no_prose("aita_get_report", wire)


@pytest.mark.parametrize("label", sorted(POLICIES))
async def test_the_step_page_is_exactly_the_seven_allowed_fields(
    database: Any, mcp_settings: Settings, mcp_workspace: dict[str, str], label: str
) -> None:
    """§6.2: step metadata in full, the four gated fields absent rather than null."""
    rows = seeded(database, mcp_workspace, POLICIES[label])

    async with live_session(mcp_settings) as client:
        arguments = calls(rows, content=False)
        page, wire = await answered(client, "aita_get_execution_steps", arguments["aita_get_execution_steps"])

    assert page["details_available"] is False
    assert [item["step_no"] for item in page["items"]] == [1, 2, 3]
    for item in page["items"]:
        assert set(item) == STEP_METADATA, item
        assert not GATED_STEP_FIELDS & set(item)
    assert [item["status"] for item in page["items"]] == ["PASSED", "FAILED", "FAILED"]
    assert [item["error_code"] for item in page["items"]] == [None, "assertion_failed", "assertion_failed"]
    assert_no_prose("aita_get_execution_steps", wire)


@pytest.mark.parametrize("label", sorted(POLICIES))
async def test_closing_a_project_produces_no_content_audit_row(
    database: Any, mcp_settings: Settings, mcp_workspace: dict[str, str], label: str
) -> None:
    """§11 - an audit records content that left; a refused or projected read has nothing to record."""
    rows = seeded(database, mcp_workspace, POLICIES[label])
    arguments = calls(rows, content=True)

    async with live_session(mcp_settings) as client:
        for tool, outcome in CLOSED_ROWS.items():
            envelope = await call_tool(client, tool, arguments[tool])
            assert envelope["ok"] is (outcome != REFUSED), tool

    assert content_reads(database, rows["tenant_id"]) == []


# --------------------------------------------------------------------------------------
# the other half of §6.2: an open project whose content flags are off
# --------------------------------------------------------------------------------------


async def test_an_open_project_with_report_details_off_projects_instead_of_refusing(
    database: Any, mcp_settings: Settings, mcp_workspace: dict[str, str]
) -> None:
    """§6.2: `enabled` is the gate; a lone content flag projects the answer rather than refusing it."""
    rows = seeded(database, mcp_workspace, REPORT_OFF, name="report-off")
    arguments = calls(rows, content=True)

    async with live_session(mcp_settings) as client:
        # Case content is still allowed here, so only the report plane is narrowed.
        case, _ = await answered(client, "aita_get_case", arguments["aita_get_case"])
        assert case["markdown"]

        page, wire = await answered(client, "aita_get_execution_steps", arguments["aita_get_execution_steps"])
        assert page["details_available"] is False
        for item in page["items"]:
            assert set(item) == STEP_METADATA, item
        assert_no_prose("aita_get_execution_steps", wire)

        report, wire = await answered(client, "aita_get_report", arguments["aita_get_report"])
        assert report["details_available"] is False
        assert "failure_summaries" not in report
        assert report["failure_step_ids"] == ["s2", "s3"]
        assert_no_prose("aita_get_report", wire)

    # The audit follows the content, not the request: the markdown did leave so it is recorded, and the two
    # report reads withheld theirs so they have nothing to record (§11).
    rows_after = content_reads(database, rows["tenant_id"])
    assert [row.resource_type for row in rows_after] == ["case"]
    assert rows_after[0].resource_id == rows["case_id"]


# --------------------------------------------------------------------------------------
# 审计失败不外发 (§11)
# --------------------------------------------------------------------------------------

@pytest.mark.parametrize("tool", ["aita_get_execution_steps", "aita_get_report"])
async def test_an_audit_failure_sends_no_details(
    database: Any, mcp_settings: Settings, mcp_workspace: dict[str, str], tool: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§11 - the audited reads are the two that send step prose: no audit row, no prose on the wire."""

    def refused(*_args: Any, **_kwargs: Any) -> None:
        raise SQLAlchemyError("the audit store is down")

    monkeypatch.setattr(AuditRepository, "append", refused)
    rows = seeded(database, mcp_workspace, OPEN, name="audit-under-test")
    # The run's steps failed, which is what puts prose on the wire: with details allowed, both of these
    # reads audit, and the audit is written in the same transaction as the answer.

    async with live_session(mcp_settings) as client:
        error = await error_body(client, tool, calls(rows, content=True)[tool])

    assert error["code"] == "DEPENDENCY_UNAVAILABLE"
    assert error["retryable"] is True
    assert_no_prose(tool, json.dumps(error, ensure_ascii=False))
    assert content_reads(database, rows["tenant_id"]) == []


# --------------------------------------------------------------------------------------
# the other subject failure: the caller who once had the grant no longer has it (§9.3.1)
# --------------------------------------------------------------------------------------


def holdings(database: Any, tenant_id: str) -> dict[str, int]:
    """What this tenant holds, because a refused call has to change none of it."""
    with database.session() as session:
        return {
            "cases": int(session.scalar(select(func.count(TestCase.id)).where(TestCase.tenant_id == tenant_id)) or 0),
            "artifacts": int(
                session.scalar(
                    select(func.count(CompileArtifact.id)).where(CompileArtifact.tenant_id == tenant_id)
                )
                or 0
            ),
            "executions": int(
                session.scalar(select(func.count(TestExecution.id)).where(TestExecution.tenant_id == tenant_id)) or 0
            ),
            "keys": int(
                session.scalar(select(func.count(IdempotencyRecord.id)).where(IdempotencyRecord.tenant_id == tenant_id))
                or 0
            ),
        }


@pytest.mark.parametrize("tool", sorted(CLOSED_ROWS))
async def test_a_revoked_subject_is_refused_by_every_tool(
    database: Any, mcp_settings: Settings, mcp_workspace: dict[str, str], tool: str
) -> None:
    """§9.3.1: revocation is not a rule that only guards new work - it answers every tool, reads included."""
    rows = seeded(database, mcp_workspace, OPEN, name="revoked-subject")
    arguments = calls(rows, content=False)[tool]
    before = holdings(database, rows["tenant_id"])
    revoke_subject(database, mcp_workspace)

    async with live_session(mcp_settings) as client:
        error = await error_body(client, tool, arguments)

    assert error["code"] == "FORBIDDEN", tool
    assert error["retryable"] is False, tool
    assert error["next_action"] == "none", tool
    assert_no_prose(tool, json.dumps(error, ensure_ascii=False))
    assert holdings(database, rows["tenant_id"]) == before


async def test_a_revoked_subject_cannot_collect_the_answer_an_earlier_key_earned(
    database: Any, mcp_settings: Settings, mcp_workspace: dict[str, str]
) -> None:
    """§9.3.1, §9.4: a replay is a second use of the grant, so a revoked caller is handed none of the first answer.

    Three keys are earned while the subject still holds rights, one per write shape this build has - create,
    compile, cancel. Each of them has a stored receipt to hand back, and every hand-back is the leak here.
    """
    rows = seeded(database, mcp_workspace, OPEN, name="revoked-replay")
    create_args = ask(
        rows,
        project_id=rows["project_id"],
        name=CASE_NAME,
        markdown=DEFAULT_MARKDOWN,
        idempotency_key="revoked-create",
    )
    compile_args = ask(rows, revision_id=rows["revision_id"], idempotency_key="revoked-compile")
    cancel_args = ask(rows, execution_id=rows["execution_id"], idempotency_key="revoked-cancel")

    async with live_session(mcp_settings) as client:
        for tool, arguments in (
            ("aita_create_case", create_args),
            ("aita_compile_case_revision", compile_args),
            ("aita_cancel_execution", cancel_args),
        ):
            assert (await call_tool(client, tool, arguments))["ok"] is True, tool
        earned = holdings(database, rows["tenant_id"])
        revoke_subject(database, mcp_workspace)

        for tool, arguments in (
            ("aita_create_case", create_args),
            ("aita_compile_case_revision", compile_args),
            ("aita_cancel_execution", cancel_args),
        ):
            error = await error_body(client, tool, arguments)
            assert error["code"] == "FORBIDDEN", tool
            assert_no_prose(tool, json.dumps(error, ensure_ascii=False))

    # The three answers stayed in the database, unspent and unwritten-by-again: the refusal came before the
    # command, so the case is still the one case and the run is still the one run.
    assert earned["cases"] == 2
    assert holdings(database, rows["tenant_id"]) == earned
