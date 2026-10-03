"""M3: the write adapters over the real protocol (§6.2, §6.6, §9.2, §9.3, §11, §15).

These are the calls that change what the platform holds, so the assertions are about the two things a
reader cannot see in a receipt: whether this call *executed* or replayed an earlier answer, and whether a
refusal left the key unspent. Both are properties of the transaction, so both are driven through the wire
rather than by calling the commands, and the database is read back afterwards to check what really exists.

The harness lives in `mcp_live.py` with the read tests; the rows here are created by the tools themselves,
because a write adapter that could not create its own case would prove nothing about creating one.
"""

from __future__ import annotations

import json
import time
from datetime import timedelta
from typing import Any

import pytest
from backend.app.config import Settings
from backend.app.db.base import new_id, utcnow
from backend.app.db.models import (
    AuditLog,
    CaseRevision,
    CompileArtifact,
    Environment,
    EnvironmentRevision,
    IdempotencyRecord,
    Outbox,
    Project,
    TestCase,
    TestExecution,
    WorkerLease,
)
from backend.app.domain.enums import CompileStatus, ExecutionStatus, Outcome
from backend.app.domain.mcp_policy import POLICY_KEY
from backend.app.mcp import tools
from backend.app.mcp.auth import SCOPE_CONNECT, SCOPE_READ, SCOPE_RUN, SCOPE_WRITE, VerifiedPrincipal
from backend.app.mcp.callcontext import McpCall
from backend.app.mcp.errors import ToolFailure
from backend.app.mcp.projections import EXECUTION_POLL_MS, FIRST_COMPILE_POLL_MS
from backend.app.mcp.server import McpServices
from backend.app.orchestrator.events import EXECUTION_QUEUED
from backend.app.workers.compile import CompileWorker
from mcp.client.client import Client
from sqlalchemy import delete, func, select
from sqlalchemy.exc import SQLAlchemyError

from .mcp_live import (
    CLOSED,
    OPEN,
    GateContext,
    ask,
    call_tool,
    error_body,
    gate,
    live_session,
    live_settings,
    moment,
    new_environment,
    new_project,
    ok_data,
    other_tenant,
    revoke_subject,
    seed_workspace,
    seeded_run,
)

#: Body text no answer may repeat: the receipt names what was created, it does not echo what was sent (§6.6).
AUTHORED = "SENTINEL-AUTHORED-CASE-BODY"
CASE_BODY = f"""---
dsl_version: "1.0"
tags: [smoke]
---
# 结算回归 {AUTHORED}

1. goto ${{base_url}}/orders
2. expect text #total contains {AUTHORED}
"""
SECOND_BODY = CASE_BODY.replace(AUTHORED, "SENTINEL-SECOND-CASE-BODY")
NAME = "结清尾款回归"
RECEIPT_FIELDS = {
    "case_id",
    "revision_id",
    "revision_no",
    "row_version",
    "source_digest",
    "compilation_lookup",
}


@pytest.fixture
def mcp_settings(database: Any, tmp_path: Any) -> Settings:
    return live_settings(database, tmp_path)


@pytest.fixture
def mcp_workspace(database: Any, mcp_settings: Settings) -> dict[str, str]:
    return seed_workspace(database, mcp_settings)


@pytest.fixture
def project(database: Any, mcp_workspace: dict[str, str]) -> str:
    return new_project(database, mcp_workspace, "writes", policy=OPEN, at=moment(1))


def write_args(workspace: dict[str, str], project_id: str, key: str, **extra: Any) -> dict[str, Any]:
    """The least a `aita_create_case` call can legally be, plus whatever this test is about."""
    return {
        **ask(workspace, project_id=project_id),
        "name": NAME,
        "markdown": CASE_BODY,
        "idempotency_key": key,
        **extra,
    }


def set_policy(database: Any, project_id: str, policy: dict[str, bool]) -> None:
    """Switch a project's MCP access the way the console does, between two calls of one test."""
    with database.session() as session:
        row = session.get(Project, project_id)
        row.settings = dict(row.settings or {}) | {POLICY_KEY: dict(policy)}
        session.commit()


def counts(database: Any, tenant_id: str, project_id: str | None = None) -> dict[str, int]:
    """How much this test has actually written, because a receipt alone cannot tell execution from replay."""
    with database.session() as session:
        cases = session.scalar(
            select(func.count(TestCase.id)).where(TestCase.tenant_id == tenant_id)
            if project_id is None
            else select(func.count(TestCase.id)).where(TestCase.project_id == project_id)
        )
        keys = session.scalar(
            select(func.count(IdempotencyRecord.id)).where(IdempotencyRecord.tenant_id == tenant_id)
        )
        creates = session.scalar(
            select(func.count(AuditLog.id)).where(
                AuditLog.tenant_id == tenant_id, AuditLog.operation == "case.create"
            )
        )
    return {"cases": int(cases or 0), "keys": int(keys or 0), "create_audits": int(creates or 0)}


class LogSpy:
    """The observation line as it was built, before any formatter: §15 is a claim about these fields."""

    def __init__(self) -> None:
        self.lines: list[tuple[str, dict[str, Any]]] = []

    def _record(self, message: str, extra: dict[str, Any] | None) -> None:
        fields = (extra or {}).get("fields") or {}
        self.lines.append((message, fields))

    def info(self, message: str, extra: dict[str, Any] | None = None, *_args: Any, **_kwargs: Any) -> None:
        self._record(message, extra)

    warning = info

    def exception(self, message: str, extra: dict[str, Any] | None = None, *_args: Any, **_kwargs: Any) -> None:
        self._record(message, extra)

    error = exception

    def debug(self, *_args: Any, **_kwargs: Any) -> None:
        return

    def writes(self) -> list[dict[str, Any]]:
        return [fields for message, fields in self.lines if message == "mcp_write"]


@pytest.fixture
def spy(monkeypatch: pytest.MonkeyPatch) -> LogSpy:
    logger = LogSpy()
    monkeypatch.setattr(tools, "log", logger)
    return logger


# --------------------------------------------------------------------------------------
# the receipt
# --------------------------------------------------------------------------------------


async def test_creating_a_case_answers_with_ids_and_nothing_else(
    database: Any, mcp_settings: Settings, mcp_workspace: dict[str, str], project: str
) -> None:
    """§6.6: a CaseWriteReceipt is six fields, and the text it was built from stays on the platform."""
    async with live_session(mcp_settings) as client:
        receipt = await ok_data(client, "aita_create_case", write_args(mcp_workspace, project, "k-shape"))
        assert set(receipt) == RECEIPT_FIELDS, receipt
        assert receipt["revision_no"] == 1
        # The case row is created at 1 and the same command then attaches revision 1, so the version a
        # caller must write against is 2 the moment the case exists. Naming 1 here would hand every client
        # a guaranteed VERSION_CONFLICT on its first revision.
        assert receipt["row_version"] == 2
        assert receipt["source_digest"].startswith("sha256:")
        assert receipt["compilation_lookup"] == {"revision_id": receipt["revision_id"]}

        # The write really committed: the read plane finds the same case, with the text that was sent.
        case = await ok_data(
            client, "aita_get_case", ask(mcp_workspace, case_id=receipt["case_id"], include_markdown=True)
        )
        assert case["current_revision_id"] == receipt["revision_id"]
        assert case["source_digest"] == receipt["source_digest"]
        assert case["markdown"] == CASE_BODY

        # ... and the receipt that named it carried none of that text.
        wire = json.dumps(await call_tool(client, "aita_create_case", write_args(mcp_workspace, project, "k-new")))
        assert AUTHORED not in wire
        assert NAME not in wire


async def test_the_receipt_names_the_version_to_revise_against(
    database: Any, mcp_settings: Settings, mcp_workspace: dict[str, str], project: str
) -> None:
    """A replay must hand back the row version the caller should use *now*, not the one from the write."""
    arguments = write_args(mcp_workspace, project, "k-version")
    async with live_session(mcp_settings) as client:
        first = await ok_data(client, "aita_create_case", arguments)
        assert first["row_version"] == 2

        # A second call with the same key replays the old answer - and the case has moved on underneath it.
        await ok_data(
            client,
            "aita_add_case_revision",
            ask(
                mcp_workspace,
                case_id=first["case_id"],
                markdown=SECOND_BODY,
                expected_row_version=first["row_version"],
                idempotency_key="k-revise",
            ),
        )
        replayed = await ok_data(client, "aita_create_case", arguments)

    assert replayed["case_id"] == first["case_id"]
    assert replayed["revision_id"] == first["revision_id"]
    assert replayed["revision_no"] == first["revision_no"]
    # The answer is the earlier one, but the version the caller is told to use is the current one.
    assert replayed["row_version"] == 3


# --------------------------------------------------------------------------------------
# idempotency: replay, conflict, and the key a refusal must not spend (§9.2, §15)
# --------------------------------------------------------------------------------------


async def test_the_same_key_creates_one_case_and_replays_the_same_answer(
    database: Any, mcp_settings: Settings, mcp_workspace: dict[str, str], project: str, spy: LogSpy
) -> None:
    """§9.2: one intent, one case, one resource-creation audit - and a log line that says which it was."""
    arguments = write_args(mcp_workspace, project, "k-twice")
    async with live_session(mcp_settings) as client:
        first = await ok_data(client, "aita_create_case", arguments)
        second = await ok_data(client, "aita_create_case", arguments)

    assert first == second
    assert counts(database, mcp_workspace["tenant_id"], project) == {"cases": 1, "keys": 1, "create_audits": 1}

    lines = spy.writes()
    assert [line["replayed"] for line in lines] == [False, True], lines
    assert [line["tool"] for line in lines] == ["aita_create_case"] * 2
    assert all(line["case_id"] == first["case_id"] for line in lines)
    assert all(line["request_id"] for line in lines)
    # A replay mints a new correlation id rather than repeating the stored one (§9.4).
    assert lines[0]["request_id"] != lines[1]["request_id"]


async def test_the_logged_write_line_carries_identifiers_and_no_content(
    database: Any, mcp_settings: Settings, mcp_workspace: dict[str, str], project: str, spy: LogSpy
) -> None:
    """§15 - a log line has no policy gate and no byte budget, so the only fields it may carry are ids."""
    async with live_session(mcp_settings) as client:
        receipt = await ok_data(client, "aita_create_case", write_args(mcp_workspace, project, "k-log", tags=["smoke"]))

    fields = spy.writes()[0]
    assert AUTHORED not in json.dumps(fields)
    assert NAME not in json.dumps(fields)
    assert "markdown" not in fields
    assert "name" not in fields
    assert "tags" not in fields
    # The key is a caller-held token; the ids say what it did, which is all the log needs (§15).
    assert "k-log" not in json.dumps(fields)
    assert fields["project_id"] == project
    assert fields["case_id"] == receipt["case_id"]
    assert fields["tenant_id"] == mcp_workspace["tenant_id"]
    # `actor_id` is the platform's own resolved user, not the token subject: §15 wants the identity that the
    # tenant tables agreed to, which is only knowable inside the transaction that committed the write.
    assert fields["actor_id"] == mcp_workspace["engineer_user_id"]
    assert fields["result_code"] == "OK"


async def test_a_refused_write_is_logged_by_code_and_by_nothing_else(
    database: Any, mcp_settings: Settings, mcp_workspace: dict[str, str], project: str, spy: LogSpy
) -> None:
    """§15: `result_code` is the field that makes a refusal findable, and a refusal is the call to find.

    Two refusals, because they arrive as two different exception types: the gate raises the adapter's own
    `ToolFailure`, whose code is already a plain string, while the version guard raises the platform's
    `ApiError`, whose code is an enum - and a log field that reads `ErrorCode.VERSION_CONFLICT` is a field
    nobody can query.
    """
    set_policy(database, project, CLOSED)
    async with live_session(mcp_settings) as client:
        error = await error_body(client, "aita_create_case", write_args(mcp_workspace, project, "k-refused"))

    fields = spy.writes()[0]
    assert fields["result_code"] == error["code"] == "MCP_PROJECT_DISABLED"
    assert fields["replayed"] is False
    assert fields["tool"] == "aita_create_case"
    assert fields["project_id"] == project
    assert AUTHORED not in json.dumps(fields)
    assert NAME not in json.dumps(fields)
    assert "actor_id" not in fields, "the transaction never resolved an identity to name"

    set_policy(database, project, OPEN)
    async with live_session(mcp_settings) as client:
        created = await ok_data(client, "aita_create_case", write_args(mcp_workspace, project, "k-logged"))
        await error_body(
            client,
            "aita_add_case_revision",
            ask(
                mcp_workspace,
                case_id=created["case_id"],
                markdown=SECOND_BODY,
                expected_row_version=99,
                idempotency_key="k-stale-version",
            ),
        )

    refused = spy.writes()[-1]
    assert refused["result_code"] == "VERSION_CONFLICT", refused
    assert refused["case_id"] == created["case_id"]
    assert AUTHORED not in json.dumps(refused)


async def test_the_same_key_with_a_different_body_is_a_conflict(
    database: Any, mcp_settings: Settings, mcp_workspace: dict[str, str], project: str
) -> None:
    """§9.2: a key is one intent, so a second body under it is refused, not executed and not replayed."""
    async with live_session(mcp_settings) as client:
        created = await ok_data(client, "aita_create_case", write_args(mcp_workspace, project, "k-clash"))
        error = await error_body(
            client, "aita_create_case", write_args(mcp_workspace, project, "k-clash", markdown=SECOND_BODY)
        )

    assert error["code"] == "IDEMPOTENCY_CONFLICT"
    assert error["retryable"] is False
    assert error["next_action"] == "fix_input"
    assert AUTHORED not in json.dumps(error)

    after = counts(database, mcp_workspace["tenant_id"], project)
    assert after["cases"] == 1
    assert after["keys"] == 1
    with database.session() as session:
        # The refused call appended nothing: the case still holds only the revision its first call wrote.
        revisions = list(
            session.scalars(select(CaseRevision.version).where(CaseRevision.case_id == created["case_id"]))
        )
        assert revisions == [1]


async def test_a_refused_intent_leaves_the_key_unspent(
    database: Any, mcp_settings: Settings, mcp_workspace: dict[str, str], project: str
) -> None:
    """§6.2, §9.3: a closed project refuses a new write, and the same key works once the project is open."""
    set_policy(database, project, CLOSED)
    arguments = write_args(mcp_workspace, project, "k-closed")
    async with live_session(mcp_settings) as client:
        error = await error_body(client, "aita_create_case", arguments)
        assert error["code"] == "MCP_PROJECT_DISABLED"
        assert counts(database, mcp_workspace["tenant_id"], project) == {"cases": 0, "keys": 0, "create_audits": 0}

        # The reservation rolled back with the refusal, so reopening the project does not need a new key.
        set_policy(database, project, OPEN)
        receipt = await ok_data(client, "aita_create_case", arguments)

    assert receipt["revision_no"] == 1
    assert counts(database, mcp_workspace["tenant_id"], project) == {"cases": 1, "keys": 1, "create_audits": 1}


async def test_a_closed_project_still_answers_a_successful_earlier_intent(
    database: Any, mcp_settings: Settings, mcp_workspace: dict[str, str], project: str, spy: LogSpy
) -> None:
    """§6.2: closing a project stops new writes; the answer to one that already succeeded stays readable."""
    arguments = write_args(mcp_workspace, project, "k-after-close")
    async with live_session(mcp_settings) as client:
        created = await ok_data(client, "aita_create_case", arguments)
        set_policy(database, project, CLOSED)
        replay = await ok_data(client, "aita_create_case", arguments)

        # A *new* intent in the same closed project is still refused, replay or no replay.
        error = await error_body(client, "aita_create_case", write_args(mcp_workspace, project, "k-new-intent"))
        assert error["code"] == "MCP_PROJECT_DISABLED"

    assert replay["case_id"] == created["case_id"]
    assert replay["revision_id"] == created["revision_id"]
    # One case, and one spent key: the refused new intent never reserved one, so `keys` stays at 1 (§9.3).
    assert counts(database, mcp_workspace["tenant_id"], project) == {"cases": 1, "keys": 1, "create_audits": 1}
    # create / replay / the refused new intent - the replay is the only line that says it executed nothing,
    # and the refusal is a line the caller would otherwise never appear in this process's log at all (§15).
    assert [line["replayed"] for line in spy.writes()] == [False, True, False]
    assert [line["result_code"] for line in spy.writes()] == ["OK", "REPLAYED", "MCP_PROJECT_DISABLED"]


# --------------------------------------------------------------------------------------
# the second command, and the refusals that belong to both
# --------------------------------------------------------------------------------------


async def test_add_case_revision_needs_the_version_the_caller_read(
    database: Any, mcp_settings: Settings, mcp_workspace: dict[str, str], project: str
) -> None:
    """§6.6: `expected_row_version` is the lost-update guard, and its refusal is a stable code."""
    async with live_session(mcp_settings) as client:
        created = await ok_data(client, "aita_create_case", write_args(mcp_workspace, project, "k-rev-create"))
        basis = created["row_version"]
        second = await ok_data(
            client,
            "aita_add_case_revision",
            ask(
                mcp_workspace,
                case_id=created["case_id"],
                markdown=SECOND_BODY,
                expected_row_version=basis,
                idempotency_key="k-rev-two",
            ),
        )
        assert second["revision_no"] == 2
        assert second["row_version"] == basis + 1
        assert set(second) == RECEIPT_FIELDS

        # The same key again replays that revision rather than adding a third (§9.2). The version it names is
        # now stale, and that does not matter: §9.3.1 puts a variable precondition after the replay decision,
        # so a retry of a write that already succeeded is never refused for a version it already advanced.
        replayed = await ok_data(
            client,
            "aita_add_case_revision",
            ask(
                mcp_workspace,
                case_id=created["case_id"],
                markdown=SECOND_BODY,
                expected_row_version=basis,
                idempotency_key="k-rev-two",
            ),
        )
        assert replayed["revision_id"] == second["revision_id"]

        # A *new* intent on that stale version is the case the guard exists for.
        error = await error_body(
            client,
            "aita_add_case_revision",
            ask(
                mcp_workspace,
                case_id=created["case_id"],
                markdown=CASE_BODY,
                expected_row_version=basis,
                idempotency_key="k-rev-stale",
            ),
        )

    assert replayed["revision_no"] == 2
    assert error["code"] == "VERSION_CONFLICT"
    assert error["retryable"] is False
    assert error["next_action"] == "query_current_policy_and_etag"
    assert counts(database, mcp_workspace["tenant_id"], project)["cases"] == 1


async def test_an_unknown_case_is_a_not_found_and_writes_nothing(
    database: Any, mcp_settings: Settings, mcp_workspace: dict[str, str]
) -> None:
    """§14.1: an id the caller cannot see answers 404-shaped, before the key is spent."""
    error = None
    async with live_session(mcp_settings) as client:
        error = await error_body(
            client,
            "aita_add_case_revision",
            ask(
                mcp_workspace,
                case_id="case-does-not-exist",
                markdown=CASE_BODY,
                expected_row_version=1,
                idempotency_key="k-ghost",
            ),
        )
    assert error["code"] == "NOT_FOUND"
    assert error["next_action"] == "fix_input"
    assert counts(database, mcp_workspace["tenant_id"])["keys"] == 0


async def test_a_project_in_another_tenant_is_not_visible_to_a_write(
    database: Any, mcp_settings: Settings, mcp_workspace: dict[str, str]
) -> None:
    """§14.2: a foreign project id answers as not found, and the caller's own tenant gains nothing."""
    other_tenant_id, foreign_project = other_tenant(database, mcp_workspace, mcp_workspace["engineer_user_id"])
    async with live_session(mcp_settings) as client:
        error = await error_body(client, "aita_create_case", write_args(mcp_workspace, foreign_project, "k-cross"))
        # The same id does belong to the other tenant the same subject is a member of, so asking for it
        # there is a legitimate write - which is what proves the refusal above was about tenant, not id.
        created = await ok_data(
            client,
            "aita_create_case",
            write_args({"tenant_id": other_tenant_id}, foreign_project, "k-cross-ok"),
        )
    assert error["code"] == "NOT_FOUND"
    assert created["revision_no"] == 1


async def test_a_write_without_the_write_scope_never_reaches_the_command(
    database: Any, mcp_settings: Settings, mcp_workspace: dict[str, str], project: str
) -> None:
    """§5.3: the scope floor is the gate's, and a credential that lacks it cannot create a case."""
    services = McpServices(mcp_settings, database=database)
    middleware, call_next, reached = gate(services)
    arguments = write_args(mcp_workspace, project, "k-scope")
    try:
        for scopes, required in ((SCOPE_CONNECT, SCOPE_WRITE), (SCOPE_READ, SCOPE_WRITE)):
            ctx = GateContext(VerifiedPrincipal("local-dev", "dev-engineer", (scopes,)), {"name": "aita_create_case"})
            ctx.params = {"name": "aita_create_case", "arguments": arguments}
            result = await middleware(ctx, call_next)
            assert reached == []
            error = result.structured_content["error"]
            assert error["code"] == "FORBIDDEN"
            assert error["details"] == {"required_scopes": [required]}
            assert error["next_action"] == "reauthorize_scope"
    finally:
        await services.close()
    assert counts(database, mcp_workspace["tenant_id"], project) == {"cases": 0, "keys": 0, "create_audits": 0}


# --------------------------------------------------------------------------------------
# the third command: a compile, and the two policies that decide whether a model sees the case
# --------------------------------------------------------------------------------------


COMPILE_FIELDS = {
    "compile_artifact_id",
    "revision_id",
    "compile_status",
    "compiler_mode",
    "compiler_version",
    "recommended_poll_after_ms",
}
SERVER_AI = dict(OPEN, allow_server_ai=True)
#: A configured model that is nowhere near reachable: the compile is queued, and the worker that would
#: call it is not running in this process (§6.4 checks the *policy*, not the network).
AI_ON = "http://127.0.0.1:9/v1"


def compile_args(workspace: dict[str, str], revision_id: str, key: str, **extra: Any) -> dict[str, Any]:
    return {**ask(workspace, revision_id=revision_id), "idempotency_key": key, **extra}


def with_model(settings: Settings) -> Settings:
    return settings.model_copy(update={"ai_enabled": True, "ai_base_url": AI_ON})


def artifacts_of(database: Any, revision_id: str) -> list[str]:
    with database.session() as session:
        return list(session.scalars(select(CompileArtifact.id).where(CompileArtifact.revision_id == revision_id)))


def queued_compile(database: Any, revision_id: str, *, force: bool = False, use_ai: bool = False) -> dict[str, Any]:
    """The one queue entry this revision holds for exactly this intent.

    Saving a case already queues a deterministic compile, so "the compile job" is not one row: the intent
    is what tells the two apart, and it is the same pair the discriminator is built from (§13.2).
    """
    with database.session() as session:
        queued = session.scalars(
            select(Outbox).where(Outbox.aggregate_id == revision_id, Outbox.event_type == "compile.request")
        )
        rows = list(queued)
    assert rows, "the compile was never queued"
    intents = {(bool(row.payload["force"]), bool(row.payload["use_ai"])): row.payload for row in rows}
    assert (force, use_ai) in intents, [dict(payload) for payload in intents.values()]
    return dict(intents[(force, use_ai)])


async def test_compiling_a_revision_answers_with_the_artifact_and_when_to_ask_again(
    database: Any, mcp_settings: Settings, mcp_workspace: dict[str, str], project: str
) -> None:
    """§6.6, §8.1: the receipt names the attempt and suggests the first poll interval, and forks nothing."""
    async with live_session(mcp_settings) as client:
        created = await ok_data(client, "aita_create_case", write_args(mcp_workspace, project, "k-compile"))
        arguments = compile_args(mcp_workspace, created["revision_id"], "k-queue")
        receipt = await ok_data(client, "aita_compile_case_revision", arguments)

        assert set(receipt) == COMPILE_FIELDS, receipt
        assert receipt["revision_id"] == created["revision_id"]
        assert receipt["compile_status"] == "PENDING"
        assert receipt["compiler_mode"] == "deterministic"
        assert receipt["compiler_version"]
        assert receipt["recommended_poll_after_ms"] == FIRST_COMPILE_POLL_MS
        assert AUTHORED not in json.dumps(receipt)

        # Asking again with the same key answers with the same attempt, and a deterministic compile of a
        # revision that was already queued joins that queue entry rather than forking a second one (§13.2).
        replayed = await ok_data(client, "aita_compile_case_revision", arguments)
        assert replayed["compile_artifact_id"] == receipt["compile_artifact_id"]

    assert artifacts_of(database, created["revision_id"]) == [receipt["compile_artifact_id"]]


async def test_a_replay_of_a_finished_attempt_reports_the_artifact_as_it_is_now(
    database: Any, mcp_settings: Settings, mcp_workspace: dict[str, str], project: str
) -> None:
    """The stored answer is the intent; the status and the poll are read live, so a replay cannot lie (§6.6)."""
    async with live_session(mcp_settings) as client:
        created = await ok_data(client, "aita_create_case", write_args(mcp_workspace, project, "k-settled"))
        arguments = compile_args(mcp_workspace, created["revision_id"], "k-settle")
        receipt = await ok_data(client, "aita_compile_case_revision", arguments)
        with database.session() as session:
            artifact = session.get(CompileArtifact, receipt["compile_artifact_id"])
            artifact.status = CompileStatus.SUCCEEDED.value
            session.commit()
        replayed = await ok_data(client, "aita_compile_case_revision", arguments)

    assert replayed["compile_status"] == "SUCCEEDED"
    assert replayed["recommended_poll_after_ms"] is None


async def test_the_queued_compile_records_which_entrypoint_asked(
    database: Any, mcp_settings: Settings, mcp_workspace: dict[str, str], project: str
) -> None:
    """§6.4: the worker may not read only the global AI switch, so the job has to say who queued it."""
    async with live_session(mcp_settings) as client:
        created = await ok_data(client, "aita_create_case", write_args(mcp_workspace, project, "k-origin"))
        await ok_data(
            client,
            "aita_compile_case_revision",
            compile_args(mcp_workspace, created["revision_id"], "k-origin-2", force=True),
        )

    assert queued_compile(database, created["revision_id"], force=True)["origin"] == "mcp"


async def test_use_ai_is_refused_when_the_deployment_has_no_model(
    database: Any, mcp_settings: Settings, mcp_workspace: dict[str, str], project: str
) -> None:
    """MCP-AC-06: no internal model is an operator's situation, and it is named as one."""
    async with live_session(mcp_settings) as client:
        created = await ok_data(client, "aita_create_case", write_args(mcp_workspace, project, "k-nomodel"))
        error = await error_body(
            client,
            "aita_compile_case_revision",
            compile_args(mcp_workspace, created["revision_id"], "k-ai", use_ai=True),
        )

    assert error["code"] == "AI_DISABLED"
    assert error["retryable"] is False
    assert error["next_action"] == "narrow_request"
    assert error["details"]["ai_configured"] is False
    assert AUTHORED not in json.dumps(error)
    # Nothing was queued and no key row was left behind, so the same intent after the operator configures a
    # model is a first attempt, not a conflict with this refusal. Only `k-nomodel` is spent here.
    assert counts(database, mcp_workspace["tenant_id"], project)["keys"] == 1
    assert artifacts_of(database, created["revision_id"]) == []


async def test_use_ai_is_refused_when_the_project_keeps_its_own_model_out(
    database: Any, mcp_settings: Settings, mcp_workspace: dict[str, str], project: str
) -> None:
    """§5.5, §6.4: a configured model is not an agreement; `allow_server_ai` is, and it says no by name."""
    settings = with_model(mcp_settings)
    async with live_session(settings) as client:
        created = await ok_data(client, "aita_create_case", write_args(mcp_workspace, project, "k-policy"))
        error = await error_body(
            client,
            "aita_compile_case_revision",
            compile_args(mcp_workspace, created["revision_id"], "k-ai2", use_ai=True),
        )

    assert error["code"] == "DATA_POLICY_DENIED"
    assert error["details"]["flag"] == "allow_server_ai"
    assert error["details"]["project_id"] == project
    assert error["next_action"] == "narrow_request"
    assert artifacts_of(database, created["revision_id"]) == []


async def test_use_ai_is_accepted_only_when_both_halves_agree(
    database: Any, mcp_settings: Settings, mcp_workspace: dict[str, str]
) -> None:
    """§6.4: refused by name or answered with the mode that was asked for - never quietly compiled otherwise."""
    project_id = new_project(database, mcp_workspace, "ai-allowed", policy=SERVER_AI, at=moment(1))
    settings = with_model(mcp_settings)
    async with live_session(settings) as client:
        created = await ok_data(client, "aita_create_case", write_args(mcp_workspace, project_id, "k-ai-ok"))
        receipt = await ok_data(
            client,
            "aita_compile_case_revision",
            compile_args(mcp_workspace, created["revision_id"], "k-ai3", use_ai=True),
        )

    assert receipt["compiler_mode"] == "ai_assisted"
    assert receipt["compile_status"] == "PENDING"
    # The AI intent is part of the job, not only of the answer: the worker must not have to guess it from a
    # deployment-wide flag.
    assert queued_compile(database, created["revision_id"], use_ai=True)["use_ai"] is True


async def test_a_revision_the_caller_cannot_see_is_not_found_before_the_key_is_spent(
    database: Any, mcp_settings: Settings, mcp_workspace: dict[str, str]
) -> None:
    """§14.1: an unknown revision id answers 404-shaped, and writes no reservation."""
    async with live_session(mcp_settings) as client:
        error = await error_body(
            client,
            "aita_compile_case_revision",
            compile_args(mcp_workspace, "revision-does-not-exist", "k-ghost-compile"),
        )

    assert error["code"] == "NOT_FOUND"
    assert error["next_action"] == "fix_input"
    assert counts(database, mcp_workspace["tenant_id"])["keys"] == 0


async def test_the_logged_compile_line_names_the_attempt_it_queued(
    database: Any, mcp_settings: Settings, mcp_workspace: dict[str, str], project: str, spy: LogSpy
) -> None:
    """§15: for a compile the identifying field an operator goes looking for is the artifact, not the case."""
    async with live_session(mcp_settings) as client:
        created = await ok_data(client, "aita_create_case", write_args(mcp_workspace, project, "k-log-compile"))
        receipt = await ok_data(
            client,
            "aita_compile_case_revision",
            compile_args(mcp_workspace, created["revision_id"], "k-log-queue"),
        )

    fields = spy.writes()[-1]
    assert fields["compile_artifact_id"] == receipt["compile_artifact_id"]
    assert fields["revision_id"] == created["revision_id"]
    assert fields["result_code"] == "OK"
    assert "k-log-queue" not in json.dumps(fields)
    assert AUTHORED not in json.dumps(fields)


# --------------------------------------------------------------------------------------
# the fourth and fifth commands: a run, and the stop that ends it
# --------------------------------------------------------------------------------------


#: Case text the deterministic compiler accepts, carrying the one marker no answer about the run may repeat:
#: it appears in the heading and in a `wait` condition, so it is inside the IR and the step rows the run freezes.
RUN_MARKER = "SENTINEL-RUN-CASE-BODY"
RUN_BODY = f"""---
dsl_version: "1.0"
---
# 结算回归 {RUN_MARKER}

## Step 1
```yaml
action: open
url: "${{env.base_url}}/index.html"
```

## Step 2
```yaml
action: click
target:
  css: "#submit"
  description: 提交按钮
```

## Step 3
```yaml
action: wait
condition:
  kind: page_contains
  expected: "{RUN_MARKER}"
```
"""
#: The same case with a declared variable, because §6.4's promise about run variables is about types: an
#: integer must not arrive at the worker as the string `"1"`, and must not replay as one.
VAR_BODY = """---
dsl_version: "1.0"
variables:
  count:
    type: integer
---
# 结算回归

## Step 1
```yaml
action: open
url: "${env.base_url}/index.html?count=${vars.count}"
```
"""

RUN_FIELDS = {
    "execution_id",
    "tenant_id",
    "project_id",
    "status",
    "outcome",
    "compile_artifact_id",
    "ir_digest",
    "environment_revision_id",
    "evidence_mode",
    "effective_server_ai",
    "worker_available",
    "recommended_poll_after_ms",
    "links",
}
CANCEL_FIELDS = {
    "execution_id",
    "cancel_requested",
    "status",
    "terminal",
    "outcome",
    "recommended_poll_after_ms",
}


def run_target(database: Any, workspace: dict[str, str], name: str, policy: dict[str, bool]) -> dict[str, str]:
    """A project a run can be started in: MCP open, with an environment of its own (§6.4).

    The revision has to belong to this project - the platform refuses a run whose environment lives elsewhere -
    so the seeded workspace's environment cannot serve a test that writes its own project.
    """
    project_id = new_project(database, workspace, name, policy=policy, at=moment(1))
    return {
        "project_id": project_id,
        "environment_revision_id": new_environment(database, workspace, project_id, at=moment(1)),
    }


@pytest.fixture
def target(database: Any, mcp_workspace: dict[str, str]) -> dict[str, str]:
    return run_target(database, mcp_workspace, "runs", OPEN)


def run_args(
    workspace: dict[str, str], target: dict[str, str], artifact: dict[str, str], key: str, **extra: Any
) -> dict[str, Any]:
    """The least a `aita_run_test` call can legally be: the artifact, the digest, the environment, the key."""
    return {
        **ask(workspace),
        "compile_artifact_id": artifact["artifact_id"],
        "expected_ir_digest": artifact["ir_digest"],
        "environment_revision_id": target["environment_revision_id"],
        "idempotency_key": key,
        **extra,
    }


async def runnable(
    client: Client,
    database: Any,
    settings: Settings,
    workspace: dict[str, str],
    project_id: str,
    *,
    key: str,
    markdown: str = RUN_BODY,
) -> dict[str, str]:
    """A case with an executable artifact, made by the tools and compiled by the platform's own worker."""
    created = await ok_data(
        client, "aita_create_case", write_args(workspace, project_id, f"{key}-case", markdown=markdown)
    )
    queued = await ok_data(
        client, "aita_compile_case_revision", compile_args(workspace, created["revision_id"], f"{key}-compile")
    )
    result = CompileWorker(settings=settings).run(queued_compile(database, created["revision_id"]))
    assert result["status"] == CompileStatus.SUCCEEDED.value, result
    # The digest is read back through the read tool rather than from the worker's return value: this is the
    # number an assistant would actually have in hand when it asks for the run.
    read = await ok_data(
        client, "aita_get_compilation", ask(workspace, compile_artifact_id=queued["compile_artifact_id"])
    )
    assert read["executable"] is True, read
    return {
        "case_id": created["case_id"],
        "revision_id": created["revision_id"],
        "artifact_id": queued["compile_artifact_id"],
        "ir_digest": read["ir_digest"],
    }


def run_tally(database: Any, tenant_id: str) -> dict[str, int]:
    """The rows a run command writes, because a receipt cannot tell an executed call from a replayed one."""
    with database.session() as session:
        return {
            "executions": int(
                session.scalar(select(func.count(TestExecution.id)).where(TestExecution.tenant_id == tenant_id)) or 0
            ),
            "queued": int(
                session.scalar(
                    select(func.count())
                    .select_from(Outbox)
                    .where(Outbox.tenant_id == tenant_id, Outbox.event_type == EXECUTION_QUEUED)
                )
                or 0
            ),
            "run_audits": int(
                session.scalar(
                    select(func.count(AuditLog.id)).where(
                        AuditLog.tenant_id == tenant_id, AuditLog.operation == "execution.create"
                    )
                )
                or 0
            ),
            "cancel_audits": int(
                session.scalar(
                    select(func.count(AuditLog.id)).where(
                        AuditLog.tenant_id == tenant_id, AuditLog.operation == "execution.cancel"
                    )
                )
                or 0
            ),
            "keys": int(
                session.scalar(
                    select(func.count(IdempotencyRecord.id)).where(IdempotencyRecord.tenant_id == tenant_id)
                )
                or 0
            ),
        }


def execution_row(database: Any, execution_id: str) -> TestExecution:
    with database.session() as session:
        return session.get(TestExecution, execution_id)


def stored_answer(database: Any, *, route: str, key: str) -> dict[str, Any]:
    """What the record keeps for a key, which is what the next caller with that key is handed back."""
    with database.session() as session:
        row = session.scalar(
            select(IdempotencyRecord).where(IdempotencyRecord.route == route, IdempotencyRecord.key == key)
        )
    return dict(row.response["result"])


def finish(database: Any, execution_id: str, *, outcome: str = "PASSED") -> None:
    """Close a run the way the worker does, so a later call reads a terminal row (§8)."""
    with database.session() as session:
        row = session.get(TestExecution, execution_id)
        row.status = ExecutionStatus.FINISHED.value
        row.outcome = outcome
        row.ended_at = utcnow()
        session.commit()


def worker_heartbeat(database: Any, *, seconds_ago: int = 0, draining: bool = False) -> None:
    """The fleet as one Worker, because `worker_available` is exactly a heartbeat and nothing more (§13.5).

    It replaces rather than adds: three calls in one test describe three different fleets, and a leftover
    live lease from the previous one would make the draining step read as available.
    """
    with database.session() as session:
        session.execute(delete(WorkerLease))
        session.add(
            WorkerLease(
                id=new_id(),
                worker_id="worker-under-test",
                capabilities={},
                capacity=1,
                draining=draining,
                heartbeat_at=utcnow() - timedelta(seconds=seconds_ago),
            )
        )
        session.commit()


def claim(database: Any, execution_id: str, *, seconds_left: int = 300) -> None:
    """Put a Worker's lease on the run, which is the one state where a stop really is only a request."""
    with database.session() as session:
        row = session.get(TestExecution, execution_id)
        row.status = ExecutionStatus.RUNNING.value
        row.owner_worker_id = "worker-under-test"
        row.lease_until = utcnow() + timedelta(seconds=seconds_left)
        session.commit()


async def test_running_a_compiled_artifact_answers_with_the_run_and_where_to_watch(
    database: Any, mcp_settings: Settings, mcp_workspace: dict[str, str], target: dict[str, str], spy: LogSpy
) -> None:
    """§6.6: the receipt names the run and the inputs it froze, and carries none of the text behind them."""
    project_id = target["project_id"]
    async with live_session(mcp_settings) as client:
        artifact = await runnable(client, database, mcp_settings, mcp_workspace, project_id, key="r-shape")
        envelope = await call_tool(client, "aita_run_test", run_args(mcp_workspace, target, artifact, "k-run-shape"))

    data = envelope["data"]
    assert set(data) == RUN_FIELDS, data
    assert data["status"] == ExecutionStatus.QUEUED.value
    assert data["outcome"] is None
    assert data["compile_artifact_id"] == artifact["artifact_id"]
    assert data["ir_digest"] == artifact["ir_digest"]
    assert data["environment_revision_id"] == target["environment_revision_id"]
    assert data["tenant_id"] == mcp_workspace["tenant_id"]
    assert data["project_id"] == project_id
    assert data["evidence_mode"] == "NORMAL"
    assert data["effective_server_ai"] is False
    # No Worker has been seen in this deployment, and the run is queued anyway: the queue is the promise,
    # the browser is not (§13.5).
    assert data["worker_available"] is False
    assert data["recommended_poll_after_ms"] in range(EXECUTION_POLL_MS[0], EXECUTION_POLL_MS[1] + 1)
    # The link origin is the configured console, never the Host the call arrived on: those two differ in this
    # fixture, so an exact match is what proves the receipt was not built from the request (§7.4).
    assert data["links"]["run"] == f"{mcp_settings.mcp_console_url.rstrip('/')}/#/runs/{data['execution_id']}"
    assert RUN_MARKER not in json.dumps(envelope)

    row = execution_row(database, data["execution_id"])
    assert str(row.revision_id) == artifact["revision_id"]
    assert str(row.compile_artifact_id) == artifact["artifact_id"]
    # The run and the answer for it are one transaction: the queue entry exists exactly when the receipt does
    # (§9.3.5), and the audit of the creation is in the same commit as both.
    assert run_tally(database, mcp_workspace["tenant_id"])["queued"] == 1
    assert run_tally(database, mcp_workspace["tenant_id"])["run_audits"] == 1
    assert spy.writes()[-1]["execution_id"] == data["execution_id"]


async def test_the_run_freezes_the_artifact_the_call_named(
    database: Any, mcp_settings: Settings, mcp_workspace: dict[str, str], target: dict[str, str]
) -> None:
    """§9.2: an artifact is a frozen product, so a revision written afterwards cannot move this run."""
    project_id = target["project_id"]
    async with live_session(mcp_settings) as client:
        artifact = await runnable(client, database, mcp_settings, mcp_workspace, project_id, key="r-frozen")
        started = await ok_data(
            client, "aita_run_test", run_args(mcp_workspace, target, artifact, "k-run-frozen")
        )

        # The case moves on underneath the run that was just queued.
        await ok_data(
            client,
            "aita_add_case_revision",
            ask(
                mcp_workspace,
                case_id=artifact["case_id"],
                markdown=SECOND_BODY,
                expected_row_version=2,
                idempotency_key="k-run-newrev",
            ),
        )
        again = await ok_data(
            client, "aita_run_test", run_args(mcp_workspace, target, artifact, "k-run-frozen-2")
        )

    row = execution_row(database, started["execution_id"])
    later = execution_row(database, again["execution_id"])
    assert str(row.revision_id) == artifact["revision_id"]
    # Both runs are of the artifact's revision, not of the case's current one - which is the difference
    # between naming an artifact and naming a case (§6.4).
    assert str(later.revision_id) == artifact["revision_id"]
    assert later.compile_artifact_id == row.compile_artifact_id
    assert later.ir_digest == artifact["ir_digest"]


async def test_a_digest_that_is_not_the_artifacts_stops_the_run(
    database: Any, mcp_settings: Settings, mcp_workspace: dict[str, str], target: dict[str, str]
) -> None:
    """§6.4: the digest is the review, so an artifact that no longer matches it is refused, not run."""
    async with live_session(mcp_settings) as client:
        artifact = await runnable(client, database, mcp_settings, mcp_workspace, target["project_id"], key="r-digest")
        error = await error_body(
            client,
            "aita_run_test",
            run_args(mcp_workspace, target, artifact, "k-stale", expected_ir_digest="sha256:" + "f" * 64),
        )

    assert error["code"] == "COMPILE_STALE_DIGEST"
    assert error["details"]["compile_artifact_id"] == artifact["artifact_id"]
    assert error["next_action"] == "none"
    assert run_tally(database, mcp_workspace["tenant_id"])["executions"] == 0


async def test_an_artifact_the_caller_cannot_see_is_not_found_before_the_key_is_spent(
    database: Any, mcp_settings: Settings, mcp_workspace: dict[str, str], target: dict[str, str]
) -> None:
    """§14.1: an unknown artifact id answers 404-shaped, and reserves nothing."""
    async with live_session(mcp_settings) as client:
        error = await error_body(
            client,
            "aita_run_test",
            run_args(
                mcp_workspace,
                target,
                {"artifact_id": "artifact-does-not-exist", "ir_digest": "sha256:" + "a" * 64},
                "k-ghost-run",
            ),
        )

    assert error["code"] == "NOT_FOUND"
    assert error["next_action"] == "fix_input"
    assert run_tally(database, mcp_workspace["tenant_id"])["keys"] == 0


async def test_the_same_key_replays_one_run_and_reports_it_as_it_is_now(
    database: Any, mcp_settings: Settings, mcp_workspace: dict[str, str], target: dict[str, str], spy: LogSpy
) -> None:
    """§9.4: a retry of a run answers with the same execution, read live - never with a second browser."""
    async with live_session(mcp_settings) as client:
        artifact = await runnable(client, database, mcp_settings, mcp_workspace, target["project_id"], key="r-twice")
        arguments = run_args(mcp_workspace, target, artifact, "k-run-twice")
        first = await ok_data(client, "aita_run_test", arguments)
        finish(database, first["execution_id"], outcome="PASSED")
        replayed = await ok_data(client, "aita_run_test", arguments)

        assert [line["replayed"] for line in spy.writes()][-2:] == [False, True]

    assert replayed["execution_id"] == first["execution_id"]
    # The stored answer is the intent; the state and the poll come from the row, so a replay an hour later
    # cannot tell the caller the run is still queued (§6.6).
    assert replayed["status"] == ExecutionStatus.FINISHED.value
    assert replayed["outcome"] == "PASSED"
    assert replayed["recommended_poll_after_ms"] is None
    stored = stored_answer(database, route="POST /executions", key="k-run-twice")
    # The record keeps the command's answer, not this caller's receipt: a console link minted for one
    # deployment must never be handed to whoever replays the key (§9.4).
    assert set(stored) == {"id", "status", "outcome", "project_id", "case_id", "trigger"}
    assert stored["status"] == ExecutionStatus.QUEUED.value
    assert "links" not in json.dumps(stored)
    tally = run_tally(database, mcp_workspace["tenant_id"])
    # One run, one queue entry, one creation audit - and only this test's own keys spent (§9.3.5).
    assert tally["executions"] == 1
    assert tally["queued"] == 1
    assert tally["run_audits"] == 1
    # case + compile + run, and the retry spent no fourth key of its own.
    assert tally["keys"] == 3


async def test_variables_keep_their_types_and_a_different_value_is_a_different_intent(
    database: Any, mcp_settings: Settings, mcp_workspace: dict[str, str], target: dict[str, str]
) -> None:
    """§6.4: the digest is over the JSON values, and the snapshot keeps them, so `1` never becomes `"1"`."""
    async with live_session(mcp_settings) as client:
        artifact = await runnable(
            client, database, mcp_settings, mcp_workspace, target["project_id"], key="r-vars", markdown=VAR_BODY
        )
        started = await ok_data(
            client, "aita_run_test", run_args(mcp_workspace, target, artifact, "k-vars", variables={"count": 1})
        )
        clash = await error_body(
            client, "aita_run_test", run_args(mcp_workspace, target, artifact, "k-vars", variables={"count": "1"})
        )

    assert clash["code"] == "IDEMPOTENCY_CONFLICT"
    snapshot = dict(execution_row(database, started["execution_id"]).snapshot or {})
    assert snapshot["run_variables"] == {"count": 1}
    assert isinstance(snapshot["run_variables"]["count"], int)
    assert RUN_MARKER not in json.dumps(snapshot["run_variables"])


async def test_a_variable_the_case_never_declared_is_refused(
    database: Any, mcp_settings: Settings, mcp_workspace: dict[str, str], target: dict[str, str]
) -> None:
    """§6.4: the shared layer's declared/required checks are reused as they are, not widened."""
    async with live_session(mcp_settings) as client:
        artifact = await runnable(
            client, database, mcp_settings, mcp_workspace, target["project_id"], key="r-varmissing"
        )
        error = await error_body(
            client, "aita_run_test", run_args(mcp_workspace, target, artifact, "k-varmiss", variables={"count": 1})
        )

    assert error["code"] == "VARIABLE_UNDECLARED"
    assert error["details"]["declared"] == []
    assert run_tally(database, mcp_workspace["tenant_id"])["executions"] == 0


async def test_worker_available_is_a_heartbeat_that_changes_nothing(
    database: Any, mcp_settings: Settings, mcp_workspace: dict[str, str], target: dict[str, str]
) -> None:
    """§13.5: the field says a Worker was seen recently; a draining or stale one is not, and both queue."""
    worker_heartbeat(database, seconds_ago=mcp_settings.lease_ttl_seconds + 10)
    async with live_session(mcp_settings) as client:
        artifact = await runnable(client, database, mcp_settings, mcp_workspace, target["project_id"], key="r-lease")
        stale = await ok_data(client, "aita_run_test", run_args(mcp_workspace, target, artifact, "k-lease-stale"))

        worker_heartbeat(database)
        live = await ok_data(
            client, "aita_run_test", run_args(mcp_workspace, target, artifact, "k-lease-live")
        )

        worker_heartbeat(database, draining=True)
        draining = await ok_data(
            client, "aita_run_test", run_args(mcp_workspace, target, artifact, "k-lease-drain")
        )

    assert stale["worker_available"] is False
    assert live["worker_available"] is True
    assert draining["worker_available"] is False
    # Every one of them was queued, which is the whole point: the flag informs the poll, it does not gate the
    # run, and no answer here claims the site was reached.
    assert run_tally(database, mcp_workspace["tenant_id"])["executions"] == 3


async def test_an_archived_environment_cannot_start_a_new_run(
    database: Any, mcp_settings: Settings, mcp_workspace: dict[str, str], target: dict[str, str]
) -> None:
    """§6.4: archiving is a precondition of a *new* intent, so it refuses without spending the key (§9.3.1)."""
    async with live_session(mcp_settings) as client:
        artifact = await runnable(
            client, database, mcp_settings, mcp_workspace, target["project_id"], key="r-archive"
        )
        arguments = run_args(mcp_workspace, target, artifact, "k-archive")
        # The revision id names a revision, not the environment, so the archive is taken through the row.
        with database.session() as session:
            environment_id = session.get(
                EnvironmentRevision, target["environment_revision_id"]
            ).environment_id
            session.get(Environment, environment_id).archived_at = utcnow()
            session.commit()

        error = await error_body(client, "aita_run_test", arguments)
        assert error["code"] == "SEMANTIC_ERROR"
        assert run_tally(database, mcp_workspace["tenant_id"])["executions"] == 0
        assert error["details"]["environment_revision_id"] == target["environment_revision_id"]

        # The refusal spent nothing: the same intent after the archive is lifted is a first attempt, not a
        # conflict with this answer.
        with database.session() as session:
            session.get(Environment, environment_id).archived_at = None
            session.commit()
        started = await ok_data(client, "aita_run_test", arguments)

    assert started["status"] == ExecutionStatus.QUEUED.value


async def test_a_run_already_started_keeps_its_answer_when_the_environment_later_archives(
    database: Any, mcp_settings: Settings, mcp_workspace: dict[str, str], target: dict[str, str]
) -> None:
    """§9.3.1: archiving is a precondition of a new intent, so it must not reach back into a answered one."""
    async with live_session(mcp_settings) as client:
        artifact = await runnable(
            client, database, mcp_settings, mcp_workspace, target["project_id"], key="r-archive2"
        )
        arguments = run_args(mcp_workspace, target, artifact, "k-archive2")
        started = await ok_data(client, "aita_run_test", arguments)
        with database.session() as session:
            environment_id = session.get(
                EnvironmentRevision, target["environment_revision_id"]
            ).environment_id
            session.get(Environment, environment_id).archived_at = utcnow()
            session.commit()
        before = run_tally(database, mcp_workspace["tenant_id"])

        replay = await ok_data(client, "aita_run_test", arguments)
        refused = await error_body(
            client, "aita_run_test", run_args(mcp_workspace, target, artifact, "k-archive2-new")
        )

    assert replay["execution_id"] == started["execution_id"]
    assert refused["code"] == "SEMANTIC_ERROR"
    assert run_tally(database, mcp_workspace["tenant_id"]) == before


async def test_a_closed_project_refuses_a_new_run_and_replays_the_one_it_started(
    database: Any, mcp_settings: Settings, mcp_workspace: dict[str, str], target: dict[str, str]
) -> None:
    """§6.2: the run row of the matrix - a new intent is refused once MCP closes, an old answer is not."""
    async with live_session(mcp_settings) as client:
        artifact = await runnable(client, database, mcp_settings, mcp_workspace, target["project_id"], key="r-close")
        arguments = run_args(mcp_workspace, target, artifact, "k-close")
        started = await ok_data(client, "aita_run_test", arguments)
        set_policy(database, target["project_id"], CLOSED)
        before = run_tally(database, mcp_workspace["tenant_id"])

        replay = await ok_data(client, "aita_run_test", arguments)
        refused = await error_body(
            client, "aita_run_test", run_args(mcp_workspace, target, artifact, "k-close-new")
        )

    assert replay["execution_id"] == started["execution_id"]
    assert refused["code"] == "MCP_PROJECT_DISABLED"
    assert run_tally(database, mcp_workspace["tenant_id"]) == before


async def test_a_revoked_caller_cannot_collect_the_run_an_earlier_key_started(
    database: Any, mcp_settings: Settings, mcp_workspace: dict[str, str], target: dict[str, str]
) -> None:
    """§9.3.1: a replay is a fresh use of the grant, so losing membership loses the stored answer too."""
    async with live_session(mcp_settings) as client:
        artifact = await runnable(client, database, mcp_settings, mcp_workspace, target["project_id"], key="r-revoke")
        arguments = run_args(mcp_workspace, target, artifact, "k-revoke")
        started = await ok_data(client, "aita_run_test", arguments)
        earned = run_tally(database, mcp_workspace["tenant_id"])
        revoke_subject(database, mcp_workspace)

        replay = await error_body(client, "aita_run_test", arguments)
        again = await error_body(
            client, "aita_run_test", run_args(mcp_workspace, target, artifact, "k-revoke-new")
        )

    assert replay["code"] == "FORBIDDEN"
    assert again["code"] == "FORBIDDEN"
    assert replay["message"]
    # Nothing was started on the strength of a grant that no longer exists, and the run it did start stays.
    assert run_tally(database, mcp_workspace["tenant_id"]) == earned
    assert execution_row(database, started["execution_id"]).status == ExecutionStatus.QUEUED.value


async def test_a_run_needs_the_credential_to_ask_for_one(
    database: Any, mcp_settings: Settings, mcp_workspace: dict[str, str], target: dict[str, str]
) -> None:
    """§5.3: `run` is its own scope floor, above read and write on the case side."""
    services = McpServices(mcp_settings, database=database)
    middleware, call_next, reached = gate(services)
    async with live_session(mcp_settings) as client:
        artifact = await runnable(client, database, mcp_settings, mcp_workspace, target["project_id"], key="r-scope")
    arguments = run_args(mcp_workspace, target, artifact, "k-scope-run")
    try:
        for scopes in ((SCOPE_CONNECT, SCOPE_READ), (SCOPE_CONNECT, SCOPE_READ, SCOPE_WRITE)):
            ctx = GateContext(VerifiedPrincipal("local-dev", "dev-engineer", scopes), {"name": "aita_run_test"})
            ctx.params = {"name": "aita_run_test", "arguments": arguments}
            result = await middleware(ctx, call_next)
            assert reached == []
            error = result.structured_content["error"]
            assert error["code"] == "FORBIDDEN"
            assert error["details"] == {"required_scopes": [SCOPE_RUN]}
            assert error["next_action"] == "reauthorize_scope"
    finally:
        await services.close()
    assert run_tally(database, mcp_workspace["tenant_id"])["executions"] == 0


# ------------------------------------------------------------------ the AI half of a run


async def test_a_run_does_not_borrow_the_deployment_s_model_on_its_own(
    database: Any, mcp_settings: Settings, mcp_workspace: dict[str, str]
) -> None:
    """§6.4: an MCP run says no to the model unless the call asked, even where both halves would allow one."""
    allowed = run_target(database, mcp_workspace, "run-ai-no", dict(OPEN, allow_server_ai=True))
    settings = with_model(mcp_settings)
    async with live_session(settings) as client:
        artifact = await runnable(client, database, settings, mcp_workspace, allowed["project_id"], key="r-aino")
        receipt = await ok_data(client, "aita_run_test", run_args(mcp_workspace, allowed, artifact, "k-aino"))
        snapshot = execution_row(database, receipt["execution_id"]).snapshot

    assert receipt["effective_server_ai"] is False
    # Recorded as a negative, not left out: §6.4 also promises this run does not borrow the model for failure
    # analysis, and a worker can only keep that promise if the row says who asked for the model.
    assert snapshot["run_ai"] == {
        "origin": "mcp",
        "use_server_ai": False,
        "policy": {"allow_server_ai": True},
    }


async def test_a_run_with_the_model_asked_for_is_refused_by_name(
    database: Any, mcp_settings: Settings, mcp_workspace: dict[str, str], target: dict[str, str]
) -> None:
    """§6.4: two conditions, two codes, so the caller learns which half has to move."""
    async with live_session(mcp_settings) as client:
        artifact = await runnable(
            client, database, mcp_settings, mcp_workspace, target["project_id"], key="r-aidisabled"
        )
        error = await error_body(
            client, "aita_run_test", run_args(mcp_workspace, target, artifact, "k-aidisabled", use_server_ai=True)
        )

    assert error["code"] == "AI_DISABLED"
    assert error["next_action"] == "narrow_request"
    assert run_tally(database, mcp_workspace["tenant_id"])["executions"] == 0

    async with live_session(with_model(mcp_settings)) as client:
        error = await error_body(
            client, "aita_run_test", run_args(mcp_workspace, target, artifact, "k-aipolicy", use_server_ai=True)
        )
    assert error["code"] == "DATA_POLICY_DENIED"
    assert error["details"]["flag"] == "allow_server_ai"
    assert run_tally(database, mcp_workspace["tenant_id"])["executions"] == 0


async def test_a_run_the_project_agreed_to_records_what_the_gate_passed(
    database: Any, mcp_settings: Settings, mcp_workspace: dict[str, str]
) -> None:
    """§6.4: the snapshot carries the origin, the intent and the policy that actually let it through."""
    allowed = run_target(database, mcp_workspace, "run-ai", dict(OPEN, allow_server_ai=True))
    settings = with_model(mcp_settings)
    async with live_session(settings) as client:
        artifact = await runnable(client, database, settings, mcp_workspace, allowed["project_id"], key="r-aiyes")
        receipt = await ok_data(
            client, "aita_run_test", run_args(mcp_workspace, allowed, artifact, "k-aiyes", use_server_ai=True)
        )
        snapshot = execution_row(database, receipt["execution_id"]).snapshot

    assert receipt["effective_server_ai"] is True
    assert snapshot["run_ai"] == {
        "origin": "mcp",
        "use_server_ai": True,
        "policy": {"allow_server_ai": True},
    }


async def test_an_ai_run_already_started_keeps_its_answer_when_the_project_tightens(
    database: Any, mcp_settings: Settings, mcp_workspace: dict[str, str]
) -> None:
    """§9.3.1, §6.4: the policy is re-read for a new intent only; a answered run keeps its original acceptance."""
    allowed = run_target(database, mcp_workspace, "run-ai-replay", dict(OPEN, allow_server_ai=True))
    settings = with_model(mcp_settings)
    async with live_session(settings) as client:
        artifact = await runnable(client, database, settings, mcp_workspace, allowed["project_id"], key="r-aitight")
        arguments = run_args(mcp_workspace, allowed, artifact, "k-aitight", use_server_ai=True)
        started = await ok_data(client, "aita_run_test", arguments)
        set_policy(database, allowed["project_id"], dict(OPEN, allow_server_ai=False))

        replay = await ok_data(client, "aita_run_test", arguments)
        refused = await error_body(
            client,
            "aita_run_test",
            run_args(mcp_workspace, allowed, artifact, "k-aitight-new", use_server_ai=True),
        )

    assert replay["execution_id"] == started["execution_id"]
    assert refused["code"] == "DATA_POLICY_DENIED"
    assert refused["details"]["flag"] == "allow_server_ai"


# ------------------------------------------------------------------ cancelling


async def test_cancelling_answers_with_the_acceptance_and_the_live_state(
    database: Any, mcp_settings: Settings, mcp_workspace: dict[str, str], target: dict[str, str]
) -> None:
    """§6.6: a stop is requested here and the run is still the worker's to end."""
    async with live_session(mcp_settings) as client:
        artifact = await runnable(client, database, mcp_settings, mcp_workspace, target["project_id"], key="r-cancel")
        started = await ok_data(client, "aita_run_test", run_args(mcp_workspace, target, artifact, "k-cancel-run"))
        claim(database, started["execution_id"])
        envelope = await call_tool(
            client,
            "aita_cancel_execution",
            ask(mcp_workspace, execution_id=started["execution_id"], idempotency_key="k-cancel"),
        )

    data = envelope["data"]
    assert set(data) == CANCEL_FIELDS, data
    assert data["execution_id"] == started["execution_id"]
    assert data["cancel_requested"] is True
    assert data["status"] == ExecutionStatus.RUNNING.value
    assert data["terminal"] is False
    assert data["outcome"] is None
    assert data["recommended_poll_after_ms"] in range(EXECUTION_POLL_MS[0], EXECUTION_POLL_MS[1] + 1)
    assert run_tally(database, mcp_workspace["tenant_id"])["cancel_audits"] == 1


async def test_a_cancel_of_a_run_no_worker_had_claimed_ends_it_at_once(
    database: Any, mcp_settings: Settings, mcp_workspace: dict[str, str], target: dict[str, str]
) -> None:
    """§9.5: the run with no lease holder has nobody to hand the conclusion to, so this call closes it."""
    async with live_session(mcp_settings) as client:
        artifact = await runnable(client, database, mcp_settings, mcp_workspace, target["project_id"], key="r-cancel0")
        started = await ok_data(client, "aita_run_test", run_args(mcp_workspace, target, artifact, "k-cancel0-run"))
        receipt = await ok_data(
            client,
            "aita_cancel_execution",
            ask(mcp_workspace, execution_id=started["execution_id"], idempotency_key="k-cancel0"),
        )

    assert receipt["cancel_requested"] is True
    assert receipt["status"] == ExecutionStatus.FINISHED.value
    assert receipt["outcome"] == Outcome.CANCELLED.value
    assert receipt["terminal"] is True
    assert receipt["recommended_poll_after_ms"] is None


async def test_a_cancel_replay_keeps_its_original_answer_and_reads_the_new_state(
    database: Any, mcp_settings: Settings, mcp_workspace: dict[str, str], target: dict[str, str]
) -> None:
    """§9.4: the same key never asks twice, and never rewrites what the run has since become."""
    async with live_session(mcp_settings) as client:
        artifact = await runnable(client, database, mcp_settings, mcp_workspace, target["project_id"], key="r-cancel2")
        started = await ok_data(client, "aita_run_test", run_args(mcp_workspace, target, artifact, "k-cancel2-run"))
        claim(database, started["execution_id"])
        arguments = ask(mcp_workspace, execution_id=started["execution_id"], idempotency_key="k-cancel2")
        first = await ok_data(client, "aita_cancel_execution", arguments)
        finish(database, started["execution_id"], outcome="CANCELLED")
        replay = await ok_data(client, "aita_cancel_execution", arguments)
        clash = await error_body(client, "aita_cancel_execution", {**arguments, "reason": "changed mind"})

    assert replay["execution_id"] == first["execution_id"]
    assert first["status"] == ExecutionStatus.RUNNING.value
    assert replay["cancel_requested"] is True
    assert replay["status"] == ExecutionStatus.FINISHED.value
    assert replay["outcome"] == "CANCELLED"
    assert replay["terminal"] is True
    assert replay["recommended_poll_after_ms"] is None
    assert clash["code"] == "IDEMPOTENCY_CONFLICT"
    tally = run_tally(database, mcp_workspace["tenant_id"])
    # One cancellation command for one key, whatever the caller did next with it.
    assert tally["cancel_audits"] == 1


async def test_a_cancel_of_a_run_that_had_already_finished_was_not_accepted(
    database: Any, mcp_settings: Settings, mcp_workspace: dict[str, str], target: dict[str, str]
) -> None:
    """§6.6: the first call to a finished run says so, and the answer is not that it cancelled something."""
    async with live_session(mcp_settings) as client:
        artifact = await runnable(client, database, mcp_settings, mcp_workspace, target["project_id"], key="r-cancel3")
        started = await ok_data(client, "aita_run_test", run_args(mcp_workspace, target, artifact, "k-cancel3-run"))
        finish(database, started["execution_id"], outcome="PASSED")
        receipt = await ok_data(
            client,
            "aita_cancel_execution",
            ask(mcp_workspace, execution_id=started["execution_id"], idempotency_key="k-cancel3"),
        )
        replay = await ok_data(
            client,
            "aita_cancel_execution",
            ask(mcp_workspace, execution_id=started["execution_id"], idempotency_key="k-cancel3"),
        )

    assert receipt["cancel_requested"] is False
    assert receipt["outcome"] == "PASSED"
    assert receipt["terminal"] is True
    # The run is not rewritten as cancelled by a stop that arrived too late, then or on any replay (§6.6).
    assert replay == receipt


async def test_a_cancel_still_works_after_the_project_closed_mcp(
    database: Any, mcp_settings: Settings, mcp_workspace: dict[str, str], target: dict[str, str]
) -> None:
    """§6.2: closing MCP does not strand a run - the last row of the matrix has no gate in it."""
    async with live_session(mcp_settings) as client:
        artifact = await runnable(client, database, mcp_settings, mcp_workspace, target["project_id"], key="r-cancel4")
        started = await ok_data(client, "aita_run_test", run_args(mcp_workspace, target, artifact, "k-cancel4-run"))
        claim(database, started["execution_id"])
        set_policy(database, target["project_id"], CLOSED)

        refused = await error_body(
            client, "aita_run_test", run_args(mcp_workspace, target, artifact, "k-cancel4-new")
        )
        stopped = await ok_data(
            client,
            "aita_cancel_execution",
            ask(mcp_workspace, execution_id=started["execution_id"], idempotency_key="k-cancel4"),
        )

    assert refused["code"] == "MCP_PROJECT_DISABLED"
    assert stopped["cancel_requested"] is True
    assert stopped["status"] == ExecutionStatus.RUNNING.value


async def test_cancelling_runs_the_permission_check_the_read_never_makes(
    database: Any, mcp_settings: Settings, mcp_workspace: dict[str, str], target: dict[str, str]
) -> None:
    """§14.2: an engineer sees every run in the tenant and may stop only their own."""
    foreign = seeded_run(
        database,
        mcp_workspace,
        target["project_id"],
        requested_by=mcp_workspace["admin_user_id"],
        status=ExecutionStatus.QUEUED.value,
        outcome=None,
    )
    async with live_session(mcp_settings) as client:
        # The same id is readable - §6.2 lets a closed project's run be polled - but stopping it is narrower.
        read = await ok_data(client, "aita_get_execution", ask(mcp_workspace, execution_id=foreign))
        assert read["execution_id"] == foreign

        error = await error_body(
            client,
            "aita_cancel_execution",
            ask(mcp_workspace, execution_id=foreign, idempotency_key="k-cancel-foreign"),
        )
    assert error["code"] == "FORBIDDEN"
    assert run_tally(database, mcp_workspace["tenant_id"])["keys"] == 0


# --------------------------------------------------------------------------------------
# the published schema, and the pipeline's own two exits
# --------------------------------------------------------------------------------------


async def test_the_published_schema_requires_the_key_and_bounds_the_body(
    mcp_settings: Settings,
) -> None:
    """§6.6: what a client plans a call from has to say the bounds out loud."""

    async with live_session(mcp_settings) as client:
        listed = {tool.name: tool for tool in (await client.list_tools()).tools}

    create = listed["aita_create_case"]
    assert set(create.input_schema["required"]) == {
        "tenant_id",
        "project_id",
        "name",
        "markdown",
        "idempotency_key",
    }
    properties = create.input_schema["properties"]
    assert properties["markdown"]["maxLength"] == 262_144
    assert properties["name"]["maxLength"] == 200
    assert "120" in json.dumps(properties["idempotency_key"])
    assert "include_markdown" not in properties
    annotations = create.annotations.model_dump(by_alias=True, exclude_none=True)
    assert annotations["readOnlyHint"] is False
    assert annotations["idempotentHint"] is True
    assert annotations["openWorldHint"] is False

    revise = listed["aita_add_case_revision"]
    assert "expected_row_version" in revise.input_schema["required"]
    assert revise.input_schema["properties"]["expected_row_version"]["minimum"] == 1

    compile = listed["aita_compile_case_revision"]
    assert set(compile.input_schema["required"]) == {"tenant_id", "revision_id", "idempotency_key"}
    assert compile.input_schema["properties"]["use_ai"]["default"] is False
    assert compile.input_schema["properties"]["force"]["default"] is False
    hints = compile.annotations.model_dump(by_alias=True, exclude_none=True)
    assert hints["readOnlyHint"] is False
    assert hints["destructiveHint"] is False
    assert hints["idempotentHint"] is True
    # §6.5: the compile is the only case-side write whose answer is settled by a worker outside this process,
    # so it is the only one that may tell a client the world can still move under it.
    assert hints["openWorldHint"] is True


def _call(tenant_hint: str | None = None) -> McpCall:
    return McpCall(
        request_id="req_write_probe",
        subject="dev-engineer",
        credential_scopes=(SCOPE_CONNECT, SCOPE_READ, SCOPE_WRITE),
        tenant_hint=tenant_hint,
        deadline=time.monotonic() + 10.0,
    )


async def test_a_write_that_cannot_name_its_tenant_is_a_wiring_fault(
    database: Any, mcp_settings: Settings
) -> None:
    """§6.2: a command never defaults to "the caller's first tenant" - the adapter refuses instead."""
    services = McpServices(mcp_settings, database=database)
    try:
        with pytest.raises(ToolFailure) as refused, tools._command_transaction(services, _call(), tenant=None):
            pass
    finally:
        await services.close()
    assert refused.value.code == "VALIDATION_ERROR"
    assert refused.value.next_action.value == "fix_input"


async def test_a_database_failure_on_a_write_says_it_may_have_committed(
    database: Any, mcp_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§10: a lost connection at commit is not a rollback, so the advice is the same key, not a new one."""

    def broken(*_args: Any, **_kwargs: Any) -> Any:
        raise SQLAlchemyError("connection lost during commit")

    services = McpServices(mcp_settings, database=database)
    monkeypatch.setattr(tools, "write_transaction", broken)
    try:
        with (
            pytest.raises(ToolFailure) as refused,
            tools._command_transaction(services, _call("tenant-hint"), tenant=None),
        ):
            pass
    finally:
        await services.close()
    failure = refused.value
    assert failure.code == "DEPENDENCY_UNAVAILABLE"
    assert failure.retryable is True
    assert failure.next_action.value == "retry_same_key_or_query"
