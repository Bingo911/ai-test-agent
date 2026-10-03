"""M2: the three execution reads over the live protocol (§6.2, §8, §8.2, §11, §14.1).

These are the reads where a run's stored state - and, when the project agrees, its prose - reaches a model,
so the assertions are about cost as much as about content: counts come from an aggregate rather than a
loaded run, a step page carries twenty steps of a two-hundred-step run plus a cursor that reaches the rest,
evidence is referenced and never fetched, and the run's IR, its environment snapshot with the run variables
and both stack traces stay in the database. The last part is checked against markers seeded into those
columns deliberately: an assertion that a field is absent proves nothing about a row that has nothing in it.

The harness lives in `mcp_live.py`. The rows are written directly because §8.2 is a promise about reads, and
the only way to test it is a run with far more rows than any answer has fields.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from backend.app.application import report_queries
from backend.app.config import Settings
from backend.app.mcp import tools
from backend.app.reporting import report as report_module
from sqlalchemy import text

from .mcp_live import (
    CLOSED,
    OPEN,
    SENTINEL_DETAIL,
    SENTINEL_IR,
    SENTINEL_SNAPSHOT,
    SENTINEL_STEP_DETAIL,
    ask,
    content_reads,
    error_body,
    failing,
    live_session,
    live_settings,
    moment,
    new_project,
    ok_data,
    other_tenant,
    page_items,
    passing,
    seed_workspace,
    seeded_run,
    walk,
)

#: A project with MCP switched on that keeps its report prose to itself (§5.5).
REPORT_OFF = dict(OPEN, allow_report_details=False)
#: The seven fields a closed project may still see for a step (§14.1).
STEP_METADATA = ("step_id", "step_no", "action", "status", "duration_ms", "error_code", "artifact_refs")
GATED_STEP_FIELDS = ("description", "locator_attempts", "expected", "actual", "resume_phase")
#: One long enough to reach the §11 per-field clip, in characters rather than in words.
LONG_DESCRIPTION = "断言页面包含订单号与合计金额，并且金额与购物车一致。" * 90


@pytest.fixture
def mcp_settings(database: Any, tmp_path: Any) -> Settings:
    return live_settings(database, tmp_path)


@pytest.fixture
def mcp_workspace(database: Any, mcp_settings: Settings) -> dict[str, str]:
    return seed_workspace(database, mcp_settings)


@pytest.fixture
def project(database: Any, mcp_workspace: dict[str, str]) -> str:
    """A project of its own, so the demo workspace can never join an answer under test."""
    return new_project(database, mcp_workspace, "execution-reads", policy=OPEN, at=moment(1))


# --------------------------------------------------------------------------------------
# aita_get_execution
# --------------------------------------------------------------------------------------


async def test_execution_state_is_the_run_and_none_of_its_documents(mcp_settings, database, mcp_workspace, project):
    execution_id = seeded_run(
        database,
        mcp_workspace,
        project,
        error_code="task_hard_limit",
        steps=passing(2),
    )

    async with live_session(mcp_settings) as client:
        data = await ok_data(client, "aita_get_execution", ask(mcp_workspace, execution_id=execution_id))

    assert data["execution_id"] == execution_id
    assert data["tenant_id"] == mcp_workspace["tenant_id"]
    assert data["project_id"] == project
    assert data["status"] == "FINISHED"
    assert data["outcome"] == "PASSED"
    assert data["terminal"] is True
    assert data["state_version"] == 7
    assert data["base_report_ready"] is True
    # Counted by status, not listed: the answer says how far the run got without carrying a step (§6.6).
    assert data["step_counts"] == {"PASSED": 2}
    assert data["analysis_status"] == "NOT_REQUIRED"
    assert data["artifact_status"] == "COMPLETE"
    assert data["cleanup_status"] == "CLEAN"
    assert data["error_code"] == "task_hard_limit"
    assert data["human_required"] is False
    assert data["human_task"] is None
    assert data["evidence_mode"] == "NORMAL"
    assert data["browser"] == "chromium"
    assert data["active_ms"] == 4321
    # A digest is an identifier, and the document it digests is not sent (§11).
    assert data["ir_digest"] == "sha256:" + "c" * 64
    assert data["links"]["report"] == f"{mcp_settings.mcp_console_url.rstrip('/')}/#/report/{execution_id}"
    assert data["links"]["requires_project_selection"] is True
    # §8.1: a terminal run offers no interval, so a client can tell "done" from "not yet" off the payload.
    assert data["recommended_poll_after_ms"] is None
    assert data["created_at"].endswith("+00:00")
    assert data["started_at"].endswith("+00:00")
    assert data["ended_at"].endswith("+00:00")
    # The three documents on the row, plus the run's own stack trace, never reach the answer (§11).
    for sentinel in (SENTINEL_IR, SENTINEL_SNAPSHOT, SENTINEL_DETAIL):
        assert sentinel not in json.dumps(data, ensure_ascii=False)


async def test_finalizing_is_not_terminal_even_though_the_outcome_is_known(
    mcp_settings, database, mcp_workspace, project
):
    """§8: `FINALIZING` already has an outcome and is still not archived, and the answer must say so."""
    execution_id = seeded_run(
        database, mcp_workspace, project, status="FINALIZING", outcome="FAILED", steps=passing(3)
    )

    async with live_session(mcp_settings) as client:
        data = await ok_data(client, "aita_get_execution", ask(mcp_workspace, execution_id=execution_id))

    assert data["outcome"] == "FAILED"
    assert data["terminal"] is False
    assert data["base_report_ready"] is False
    assert 2000 <= data["recommended_poll_after_ms"] <= 5000


async def test_a_run_parked_on_a_person_stays_parked(mcp_settings, database, mcp_workspace, project):
    execution_id = seeded_run(
        database,
        mcp_workspace,
        project,
        status="WAIT_HUMAN",
        outcome=None,
        steps=({"step_no": 1, "status": "WAIT_HUMAN", "resume_phase": "before_action"},),
        human_task={"step_id": "s1", "reason": "captcha", "detail": "请输入屏幕上显示的六位验证码"},
    )

    async with live_session(mcp_settings) as client:
        data = await ok_data(client, "aita_get_execution", ask(mcp_workspace, execution_id=execution_id))

    assert data["status"] == "WAIT_HUMAN"
    assert data["terminal"] is False
    assert data["outcome"] is None
    assert data["human_required"] is True
    assert data["step_counts"] == {"WAIT_HUMAN": 1}
    task = data["human_task"]
    # Identifier, stable reason code, deadline and the human entry point - and nothing else (§6.6, §7.3).
    assert set(task) == {"human_task_id", "step_id", "reason", "deadline", "console_url"}
    assert task["reason"] == "captcha"
    assert task["step_id"] == "s1"
    assert task["deadline"].endswith("+00:00")
    assert task["console_url"] == f"{mcp_settings.mcp_console_url.rstrip('/')}/#/runs/{execution_id}"
    # What the operator typed about the page, and the control ticket that would hand the run over, stay (§7.3).
    assert "请输入屏幕上显示的六位验证码" not in json.dumps(data, ensure_ascii=False)
    assert "pause-" not in json.dumps(data, ensure_ascii=False)
    # A wait measured in minutes is not worth checking every two seconds (§8.1).
    assert 5000 <= data["recommended_poll_after_ms"] <= 10000


async def test_get_execution_leaves_no_content_audit(mcp_settings, database, mcp_workspace, project):
    """§11 - a minimal state read is the one read that logs rather than audits, even from an open project."""
    execution_id = seeded_run(database, mcp_workspace, project, steps=passing(1))

    async with live_session(mcp_settings) as client:
        data = await ok_data(client, "aita_get_execution", ask(mcp_workspace, execution_id=execution_id))

    assert data["details_available"] is True
    assert content_reads(database, mcp_workspace["tenant_id"]) == []


async def test_an_unknown_or_foreign_execution_is_not_found(mcp_settings, database, mcp_workspace, project):
    foreign_project = new_project(database, mcp_workspace, "foreign-run", policy=CLOSED, at=moment(1))
    other_id = seeded_run(database, mcp_workspace, foreign_project, steps=passing(1))
    # A run in another tenant is the same answer as a run that does not exist (§5.1).
    foreign_tenant, _ = other_tenant(database, mcp_workspace, mcp_workspace["engineer_user_id"])

    async with live_session(mcp_settings) as client:
        missing = await error_body(client, "aita_get_execution", ask(mcp_workspace, execution_id="exec-none"))
        cross = await error_body(
            client, "aita_get_execution", ask({"tenant_id": foreign_tenant}, execution_id=other_id)
        )

    assert missing["code"] == "NOT_FOUND"
    assert cross["code"] == "NOT_FOUND"


async def test_a_closed_project_still_answers_for_a_run_in_flight(mcp_settings, database, mcp_workspace):
    """§14.1's matrix: `aita_get_execution` is not refused by a closed project - it is projected (§6.2)."""
    closed = new_project(database, mcp_workspace, "closed-run", policy=CLOSED, at=moment(1))
    execution_id = seeded_run(database, mcp_workspace, closed, steps=passing(2))

    async with live_session(mcp_settings) as client:
        data = await ok_data(client, "aita_get_execution", ask(mcp_workspace, execution_id=execution_id))

    assert data["terminal"] is True
    assert data["step_counts"] == {"PASSED": 2}
    assert data["details_available"] is False
    assert data["links"]["run"].endswith(f"#/runs/{execution_id}")


# --------------------------------------------------------------------------------------
# aita_get_execution_steps
# --------------------------------------------------------------------------------------


async def test_step_pages_walk_the_run_in_the_order_it_ran(mcp_settings, database, mcp_workspace, project):
    execution_id = seeded_run(database, mcp_workspace, project, steps=passing(45))

    async with live_session(mcp_settings) as client:
        arguments = ask(mcp_workspace, execution_id=execution_id)
        first, cursor = await page_items(client, "aita_get_execution_steps", {**arguments, "limit": 20})
        seen = await walk(client, "aita_get_execution_steps", {**arguments, "limit": 20})

    # `step_no ASC, id ASC`, and the page stops where the caller's limit says (§6.2, §11).
    assert [item["step_no"] for item in first] == list(range(1, 21))
    assert cursor is not None
    assert [item["step_no"] for item in seen] == list(range(1, 46))
    assert len({item["step_id"] for item in seen}) == 45
    assert seen[-1]["step_id"] == "s45"


async def test_a_step_page_carries_evidence_references_and_never_a_storage_key(
    mcp_settings, database, mcp_workspace, project
):
    execution_id = seeded_run(
        database,
        mcp_workspace,
        project,
        steps=(
            {"step_no": 1, "artifacts": 8},
            {"step_no": 2, "artifacts": 0, "status": "FAILED", "error_code": "timeout"},
        ),
    )

    async with live_session(mcp_settings) as client:
        data = await ok_data(client, "aita_get_execution_steps", ask(mcp_workspace, execution_id=execution_id))

    head = data["items"][0]
    # References, at most five per step, in the step's own order - not the eight rows the run produced (§8.2).
    assert len(head["artifact_refs"]) == 5
    ref = head["artifact_refs"][0]
    assert set(ref) == {"ref", "kind", "size", "upload_status", "publishable"}
    assert ref["ref"].startswith("artifact:")
    assert ref["upload_status"] == "READY"
    # A step with no evidence says so, and a closed-looking page never reads as a withheld file (§6.6).
    assert data["items"][1]["artifact_refs"] == []
    body = json.dumps(data, ensure_ascii=False)
    # §11: no object key, local path, digest or media type goes to a model, and there is no download tool.
    assert "mcp-seed/" not in body
    assert "sha256" not in body
    assert ".png" not in body
    assert "image/png" not in body


async def test_step_details_are_absent_rather_than_empty_when_the_project_keeps_them(
    mcp_settings, database, mcp_workspace
):
    closed = new_project(database, mcp_workspace, "details-off", policy=REPORT_OFF, at=moment(1))
    execution_id = seeded_run(
        database,
        mcp_workspace,
        closed,
        steps=(*failing(1), {"step_no": 2, "status": "FAILED", "error_code": "assertion_failed"}),
    )

    async with live_session(mcp_settings) as client:
        data = await ok_data(client, "aita_get_execution_steps", ask(mcp_workspace, execution_id=execution_id))

    assert data["details_available"] is False
    item = data["items"][0]
    # The stable state is still sent in full: a page that hid the error code would hide the reason to retry.
    assert set(item) == set(STEP_METADATA)
    for field in GATED_STEP_FIELDS:
        assert field not in item
    assert item["error_code"] == "assertion_failed"
    assert content_reads(database, mcp_workspace["tenant_id"]) == []


async def test_step_details_arrive_with_the_flag_and_owe_an_audit(mcp_settings, database, mcp_workspace, project):
    execution_id = seeded_run(
        database,
        mcp_workspace,
        project,
        steps=(
            {
                "step_no": 1,
                "status": "FAILED",
                "error_code": "assertion_failed",
                "description": "断言欢迎文案",
                "expected": "Welcome back",
                "actual": "Welcome",
                "resume_phase": "after_action",
                "locator_attempts": [
                    {"strategy": "role", "selector": "button", "outcome": "miss", "reason": "x" * 400, "elapsed_ms": 12}
                    for _ in range(7)
                ],
            },
        ),
    )

    async with live_session(mcp_settings) as client:
        data = await ok_data(client, "aita_get_execution_steps", ask(mcp_workspace, execution_id=execution_id))

    item = data["items"][0]
    assert data["details_available"] is True
    assert item["description"] == "断言欢迎文案"
    assert item["expected"] == "Welcome back"
    assert item["actual"] == "Welcome"
    assert item["resume_phase"] == "after_action"
    # The same bounded locator shape the console report uses, reused rather than copied (§8.2).
    assert len(item["locator_attempts"]) == 5
    assert len(item["locator_attempts"][0]["reason"]) == 200
    # Only the two assertion keys come out of the step's error document; its trace does not (§11).
    assert SENTINEL_STEP_DETAIL not in json.dumps(data, ensure_ascii=False)

    rows = content_reads(database, mcp_workspace["tenant_id"])
    assert len(rows) == 1
    assert rows[0].resource_type == "test_execution"
    assert rows[0].resource_id == execution_id
    assert rows[0].project_id == project
    assert rows[0].detail["flag"] == "allow_report_details"
    assert rows[0].detail["steps"] == 1
    assert rows[0].detail["tool"] == "aita_get_execution_steps"


async def test_a_step_page_that_cannot_fit_is_shortened_and_re_mints_its_cursor(
    database, mcp_workspace, project, tmp_path
):
    """§11 - the metadata budget trims whole rows, and the cursor has to point at the row it kept.

    A page that simply stopped early would read as a shorter run, so the promise is that the rows this page
    moved off are still reachable - which only holds if the cursor is minted from the last kept row rather
    than from the row the query was going to answer with.
    """
    tight = live_settings(database, tmp_path, mcp_max_metadata_response_bytes=20_000)
    execution_id = seeded_run(
        database,
        mcp_workspace,
        project,
        steps=tuple(
            {"step_no": index, "status": "FAILED", "error_code": "assertion_failed", "description": LONG_DESCRIPTION}
            for index in range(1, 13)
        ),
    )

    async with live_session(tight) as client:
        arguments = ask(mcp_workspace, execution_id=execution_id)
        first, cursor = await page_items(client, "aita_get_execution_steps", {**arguments, "limit": 20})
        assert 0 < len(first) < 12, first
        assert len(first[0]["description"]) == 2000
        assert cursor is not None
        seen = await walk(client, "aita_get_execution_steps", {**arguments, "limit": 20})

    # Nothing was skipped and nothing was repeated: the trimmed tail is the next page's head (§11).
    assert [item["step_no"] for item in seen] == list(range(1, 13))


async def test_a_step_cursor_from_another_run_is_refused(mcp_settings, database, mcp_workspace, project):
    """The execution belongs in the cursor's filter digest, so a bookmark cannot walk a different run (§11)."""
    first_run = seeded_run(database, mcp_workspace, project, steps=passing(25))
    second_run = seeded_run(database, mcp_workspace, project, steps=passing(25))

    async with live_session(mcp_settings) as client:
        _, cursor = await page_items(client, "aita_get_execution_steps", ask(mcp_workspace, execution_id=first_run))
        error = await error_body(
            client, "aita_get_execution_steps", ask(mcp_workspace, execution_id=second_run, cursor=cursor)
        )

    assert error["code"] == "VALIDATION_ERROR"


# --------------------------------------------------------------------------------------
# aita_get_report
# --------------------------------------------------------------------------------------


async def test_report_counts_and_evidence_completeness_come_from_aggregates(
    mcp_settings, database, mcp_workspace, project
):
    execution_id = seeded_run(
        database,
        mcp_workspace,
        project,
        outcome="FAILED",
        steps=(*passing(2), *failing(3, first=3)),
    )

    async with live_session(mcp_settings) as client:
        data = await ok_data(client, "aita_get_report", ask(mcp_workspace, execution_id=execution_id))

    assert data["execution_id"] == execution_id
    assert data["status"] == "FINISHED"
    assert data["outcome"] == "FAILED"
    assert data["terminal"] is True
    assert data["base_report_ready"] is True
    assert data["step_counts"] == {"PASSED": 2, "FAILED": 3}
    assert data["details_available"] is True
    assert len(data["failure_step_ids"]) == 3
    # The evidence aggregate says whether it is whole, and that there is no way to fetch it here (§11).
    assert data["artifact_summary"]["total"] == 0
    assert data["artifact_summary"]["download"] is False
    assert data["links"]["report"].endswith(f"#/report/{execution_id}")
    assert data["recommended_poll_after_ms"] is None
    body = json.dumps(data, ensure_ascii=False)
    for sentinel in (SENTINEL_IR, SENTINEL_SNAPSHOT, SENTINEL_DETAIL):
        assert sentinel not in body


async def test_a_report_for_a_run_that_has_not_finished_is_progress_not_an_error(
    mcp_settings, database, mcp_workspace, project
):
    """§8 - "not yet" is a state, and a tool error would make an assistant give up on a run that is fine."""
    execution_id = seeded_run(
        database,
        mcp_workspace,
        project,
        status="RUNNING",
        outcome=None,
        analysis_status="PENDING",
        artifact_status="PENDING",
        steps=(
            *passing(3),
            {"step_no": 4, "status": "RUNNING"},
            *({"step_no": index, "status": "PENDING"} for index in range(5, 9)),
        ),
    )

    async with live_session(mcp_settings) as client:
        data = await ok_data(client, "aita_get_report", ask(mcp_workspace, execution_id=execution_id))

    assert data["terminal"] is False
    assert data["base_report_ready"] is False
    assert data["outcome"] is None
    assert data["analysis_status"] == "PENDING"
    assert data["step_counts"] == {"PASSED": 3, "RUNNING": 1, "PENDING": 4}
    assert data["failure_step_ids"] == []
    assert 2000 <= data["recommended_poll_after_ms"] <= 5000


async def test_the_report_references_five_failures_and_says_there_were_more(
    mcp_settings, database, mcp_workspace, project
):
    """§11 - five summaries is a cap, and `step_counts` is what stops it reading as the whole truth."""
    execution_id = seeded_run(
        database, mcp_workspace, project, outcome="FAILED", steps=failing(9) + passing(1)
    )

    async with live_session(mcp_settings) as client:
        data = await ok_data(client, "aita_get_report", ask(mcp_workspace, execution_id=execution_id))

    assert data["step_counts"]["FAILED"] == 9
    assert data["failure_step_ids"] == ["s1", "s2", "s3", "s4", "s5"]
    assert len(data["failure_summaries"]) == 5
    assert data["failure_summaries"][0]["expected"] == "expected-1"
    # The reference is the first five in step order, not the five the database happened to return first.
    assert [item["step_no"] for item in data["failure_summaries"]] == [1, 2, 3, 4, 5]


async def test_failure_prose_and_the_audit_travel_together(mcp_settings, database, mcp_workspace):
    closed = new_project(database, mcp_workspace, "report-off", policy=REPORT_OFF, at=moment(1))
    execution_id = seeded_run(database, mcp_workspace, closed, outcome="FAILED", steps=failing(2))

    async with live_session(mcp_settings) as client:
        data = await ok_data(client, "aita_get_report", ask(mcp_workspace, execution_id=execution_id))

    assert data["details_available"] is False
    # The references and the stable codes are still sent; only the prose is withheld (§14.1).
    assert data["failure_step_ids"] == ["s1", "s2"]
    assert data["error_code"] is None
    assert "failure_summaries" not in data
    assert content_reads(database, mcp_workspace["tenant_id"]) == []


async def test_the_failure_category_is_the_newest_analysis_row(mcp_settings, database, mcp_workspace, project):
    """§8.2 - the newest analysis is a `LIMIT 1` by revision, and a failed model pass keeps its rules (§12.3)."""
    execution_id = seeded_run(
        database,
        mcp_workspace,
        project,
        status="FINISHED",
        outcome="FAILED",
        analysis_status="SUCCEEDED",
        steps=failing(1),
        analyses=(
            {"revision": 1, "failure_type": "locator_drift", "source": "rules"},
            {"revision": 2, "failure_type": "timing_instability", "status": "FAILED", "source": "ai"},
        ),
    )

    async with live_session(mcp_settings) as client:
        data = await ok_data(client, "aita_get_report", ask(mcp_workspace, execution_id=execution_id))

    assert data["failure_type"] == "timing_instability"
    assert data["analysis_status"] == "SUCCEEDED"


async def test_a_closed_project_still_answers_with_report_statistics(mcp_settings, database, mcp_workspace):
    """§14.1: a closed project gets statistics, IDs, stable codes and links for a run it already started."""
    closed = new_project(database, mcp_workspace, "closed-report", policy=CLOSED, at=moment(1))
    execution_id = seeded_run(
        database,
        mcp_workspace,
        closed,
        status="FINALIZING",
        outcome="ERROR",
        error_code="browser_start_failed",
        steps=failing(2),
    )

    async with live_session(mcp_settings) as client:
        data = await ok_data(client, "aita_get_report", ask(mcp_workspace, execution_id=execution_id))

    assert data["status"] == "FINALIZING"
    assert data["terminal"] is False
    assert data["error_code"] == "browser_start_failed"
    assert data["failure_step_ids"] == ["s1", "s2"]
    assert data["details_available"] is False
    assert data["recommended_poll_after_ms"] is not None


# --------------------------------------------------------------------------------------
# the promises all three share
# --------------------------------------------------------------------------------------


async def test_nothing_on_this_path_builds_a_report(mcp_settings, database, mcp_workspace, project, monkeypatch):
    """§8.2 forbids reusing `build_report`, and the only proof that holds is that it was never called."""

    def refused(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("MCP must not build the whole report")

    monkeypatch.setattr(report_module, "build_report", refused)
    # ... and neither of the two modules on this path may even hold a reference to it.
    assert not hasattr(report_queries, "build_report")
    assert not hasattr(tools, "build_report")

    execution_id = seeded_run(database, mcp_workspace, project, steps=passing(30))
    async with live_session(mcp_settings) as client:
        arguments = ask(mcp_workspace, execution_id=execution_id)
        assert (await ok_data(client, "aita_get_execution", arguments))["step_counts"] == {"PASSED": 30}
        assert len((await ok_data(client, "aita_get_execution_steps", arguments))["items"]) == 20
        assert (await ok_data(client, "aita_get_report", arguments))["base_report_ready"] is True


async def test_each_execution_read_holds_one_database_connection(tmp_path, database: Any, mcp_workspace) -> None:
    """The MCP pool is exactly the execution-slot count, so a second connection would deadlock the call (§4.4).

    All three of these reads run several statements - the run's row, its counts, its open task, five failure
    references, one page of steps and this page's evidence - and every one of them has to be served by the
    session the transaction already holds. A handler that reached for the process database would show up
    here as a timeout rather than as a slow query.
    """
    project_id = new_project(database, mcp_workspace, "one-connection", policy=OPEN, at=moment(1))
    execution_id = seeded_run(
        database, mcp_workspace, project_id, steps=(*passing(2), *failing(2, first=3)), human_task={"step_id": "s1"}
    )
    tight = live_settings(database, tmp_path, mcp_db_pool_size=1, mcp_max_inflight_total=1)
    async with live_session(tight) as client:
        arguments = ask(mcp_workspace, execution_id=execution_id)
        assert (await ok_data(client, "aita_get_execution", arguments))["human_required"] is True
        assert len((await ok_data(client, "aita_get_execution_steps", arguments))["items"]) == 4
        assert (await ok_data(client, "aita_get_report", arguments))["failure_step_ids"] == ["s3", "s4"]


def test_the_step_and_failure_reads_use_the_step_index(database: Any, mcp_workspace) -> None:
    """§11 - a bounded page is a plan, not an intention: both reads must reach the step index.

    SQLite calls a scan of the table `SCAN step_execution` and a sort it could not serve from an index
    `USE TEMP B-TREE`; either would mean the page's bound was a wish, so both are refused here.
    """
    with database.session() as session:
        page = _plan(
            session,
            report_queries.step_page_statement(tenant_id=mcp_workspace["tenant_id"], execution_id="e1", limit=20),
        )
        references = _plan(
            session,
            report_queries.failure_reference_statement(
                tenant_id=mcp_workspace["tenant_id"], execution_id="e1", details=False
            ),
        )
        evidence = _plan(
            session,
            report_queries.artifact_reference_statement(
                tenant_id=mcp_workspace["tenant_id"], execution_id="e1", step_ids=["s1", "s2"]
            ),
        )

    assert "ix_step_execution_page" in page
    assert "SCAN step_execution" not in page
    assert "TEMP B-TREE" not in page.upper()
    assert "ix_step_execution_page" in references
    assert "SCAN step_execution" not in references
    # The page's evidence is read through the step-scoped index, so a run's other evidence is not touched.
    assert "ix_artifact_step_evidence" in evidence
    assert "SCAN artifact" not in evidence


def test_a_closed_policy_does_not_read_the_columns_it_may_not_send() -> None:
    """§8.2 - a projection that discards a sensitive column is still a query that fetched it.

    Asserted on the statement rather than on the answer, because that is the only place the difference
    between "never read" and "read and dropped" is visible.
    """
    tenant, execution = "t1", "e1"
    off = str(
        report_queries.step_page_statement(tenant_id=tenant, execution_id=execution, limit=20, details=False).compile(
            compile_kwargs={"literal_binds": True}
        )
    )
    on = str(
        report_queries.step_page_statement(tenant_id=tenant, execution_id=execution, limit=20, details=True).compile(
            compile_kwargs={"literal_binds": True}
        )
    )
    failures_off = str(
        report_queries.failure_reference_statement(tenant_id=tenant, execution_id=execution, details=False).compile(
            compile_kwargs={"literal_binds": True}
        )
    )

    for column in ("description", "locator_attempts", "error_detail", "resume_phase"):
        assert f"step_execution.{column}" not in off
        assert f"step_execution.{column}" in on
    assert "step_execution.description" not in failures_off
    assert "step_execution.error_detail" not in failures_off
    # The reference columns a closed project is allowed are still read, so the page is not merely empty.
    for column in ("step_id", "action", "status", "duration_ms", "error_code"):
        assert f"step_execution.{column}" in off


def _plan(session: Any, statement: Any) -> str:
    sql = str(statement.compile(compile_kwargs={"literal_binds": True}))
    rows = session.execute(text(f"EXPLAIN QUERY PLAN {sql}")).all()
    return "\n".join(" ".join(str(value) for value in tuple(row)) for row in rows)
