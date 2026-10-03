"""M2: the case and compilation reads over the real protocol (§6.2, §6.3, §6.6, §11).

These are the reads where the platform's own words leave the building - a case name, a title, a tag, the
full Markdown, an IR document - so the assertions are about *shape* as much as about content: a field the
project keeps to itself is absent rather than empty, an explicit request for it is a refusal rather than a
silently shorter answer, and an access audit that could not be written means the bytes were never sent.

The harness lives in `mcp_live.py`; the rows are written directly because a page is ordered by
`created_at`, which no REST call lets a test pin.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from backend.app.application import case_queries
from backend.app.config import Settings
from backend.app.db.models import AuditLog, CompileArtifact
from backend.app.domain.enums import CompileStatus
from backend.app.repositories.cases import latest_attempt, source_digest
from backend.app.repositories.platform import AuditRepository
from mcp.client.client import Client
from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError

from .mcp_live import (
    CLOSED,
    CONTENT_OFF,
    DEFAULT_MARKDOWN,
    OPEN,
    call_tool,
    error_body,
    live_session,
    live_settings,
    moment,
    new_case,
    new_compile,
    new_project,
    new_revision,
    ok_data,
    other_tenant,
    page_items,
    seed_workspace,
    title_of,
)

SECOND_REVISION = "# 第二版\n"

LONG_BODY = "# 结算回归\n" + ("断言页面包含订单号与合计金额，并且金额与购物车一致。" * 90)


@pytest.fixture
def mcp_settings(database: Any, tmp_path: Any) -> Settings:
    return live_settings(database, tmp_path)


@pytest.fixture
def mcp_workspace(database: Any, mcp_settings: Settings) -> dict[str, str]:
    return seed_workspace(database, mcp_settings)


@pytest.fixture
def project(database: Any, mcp_workspace: dict[str, str]) -> str:
    """A project of its own, so the demo workspace can never join a page under test."""
    return new_project(database, mcp_workspace, "case-reads", policy=OPEN, at=moment(1))


def listing(workspace: dict[str, str], project_id: str, **extra: Any) -> dict[str, Any]:
    return {"tenant_id": workspace["tenant_id"], "project_id": project_id, **extra}


def ask(workspace: dict[str, str], **extra: Any) -> dict[str, Any]:
    return {"tenant_id": workspace["tenant_id"], **extra}


def content_reads(database: Any, tenant_id: str) -> list[AuditLog]:
    with database.session() as session:
        rows = list(session.scalars(select(AuditLog).where(AuditLog.tenant_id == tenant_id)))
    return [row for row in rows if row.operation == "mcp.content.read"]


async def drain(client: Client, tool: str, arguments: dict[str, Any]) -> list[dict[str, Any]]:
    """Every row of a filtered query, following only the cursors the platform itself minted."""
    seen: list[dict[str, Any]] = []
    cursor: str | None = None
    while True:
        envelope = await call_tool(client, tool, {**arguments, "cursor": cursor})
        assert envelope["ok"] is True, envelope
        seen.extend(envelope["data"]["items"])
        cursor = envelope["next_cursor"]
        if cursor is None:
            return seen
        assert len(seen) < 200, "the page never ended"


# --------------------------------------------------------------------------------------
# aita_list_cases
# --------------------------------------------------------------------------------------


async def test_case_page_is_newest_first_and_reports_the_compile_summary(
    mcp_settings, database, mcp_workspace, project
):
    older = new_case(database, mcp_workspace, project, "旧用例", at=moment(2))
    newer = new_case(database, mcp_workspace, project, "新用例", at=moment(3), tags=("smoke",))
    new_compile(
        database,
        mcp_workspace,
        project,
        newer["revision_id"],
        status=CompileStatus.SUCCEEDED.value,
        at=moment(4),
    )

    async with live_session(mcp_settings) as client:
        data = await ok_data(client, "aita_list_cases", listing(mcp_workspace, project))

    # `created_at DESC, id DESC`, and every filter is applied before the page is taken (§6.2).
    assert [item["case_id"] for item in data["items"]] == [newer["case_id"], older["case_id"]]
    head = data["items"][0]
    assert head["revision_id"] == newer["revision_id"]
    assert head["source_digest"] == source_digest(DEFAULT_MARKDOWN)
    assert head["row_version"] == 4
    assert head["compile_status"] == CompileStatus.SUCCEEDED.value
    # The summary carries the case's own words only because this project agreed to send them (§5.5).
    assert head["name"] == "新用例"
    assert head["title"] == title_of(DEFAULT_MARKDOWN)
    assert head["tags"] == ["smoke"]
    # A case with no attempt says so rather than inventing a status (§6.3).
    assert data["items"][1]["compile_status"] is None


async def test_case_names_titles_and_tags_are_absent_when_content_is_closed(tmp_path, database, mcp_workspace):
    """§6.6 - a policy-hidden field is absent from the item; only a genuinely missing value is `null`."""
    settings = live_settings(database, tmp_path)
    workspace = seed_workspace(database, settings)
    quiet = new_project(database, workspace, "quiet-cases", policy=CONTENT_OFF, at=moment(1))
    ids = new_case(database, workspace, quiet, "保密用例", at=moment(2), tags=("smoke",))

    async with live_session(settings) as client:
        data = await ok_data(client, "aita_list_cases", listing(workspace, quiet))

    item = data["items"][0]
    assert "name" not in item
    assert "title" not in item
    assert "tags" not in item
    assert item["case_id"] == ids["case_id"]
    assert item["revision_id"] == ids["revision_id"]
    assert item["source_digest"] == source_digest(DEFAULT_MARKDOWN)
    assert item["compile_status"] is None
    assert item["archived"] is False
    assert "保密用例" not in json.dumps(data, ensure_ascii=False)
    # Nothing gated left, so this read is a log line rather than an access row (§11).
    assert content_reads(database, workspace["tenant_id"]) == []


async def test_an_archived_case_stays_in_the_page_flagged(mcp_settings, database, mcp_workspace, project):
    live = new_case(database, mcp_workspace, project, "在用的", at=moment(2))
    gone = new_case(database, mcp_workspace, project, "已归档", at=moment(3), archived=True)

    async with live_session(mcp_settings) as client:
        data = await ok_data(client, "aita_list_cases", listing(mcp_workspace, project))

    by_id = {item["case_id"]: item for item in data["items"]}
    assert set(by_id) == {live["case_id"], gone["case_id"]}
    assert by_id[gone["case_id"]]["archived"] is True
    assert by_id[live["case_id"]]["archived"] is False


async def test_search_matches_the_case_name_literally(mcp_settings, database, mcp_workspace, project):
    new_case(database, mcp_workspace, project, "50%_off 促销页", at=moment(2))
    new_case(database, mcp_workspace, project, "50x off 促销页", at=moment(3))
    new_case(database, mcp_workspace, project, "登录", at=moment(4))

    async with live_session(mcp_settings) as client:
        # `%` and `_` are LIKE's own wildcards; letting them through would match all three rows and tell
        # the assistant the project holds cases it does not have (§6.2).
        data = await ok_data(client, "aita_list_cases", listing(mcp_workspace, project, search="50%_off"))
        blank = await ok_data(client, "aita_list_cases", listing(mcp_workspace, project, search="   "))

    assert [item["name"] for item in data["items"]] == ["50%_off 促销页"]
    # A blank search is not "match the empty string", which would be every case in the project.
    assert len(blank["items"]) == 3


async def test_tags_filter_by_name_and_an_unknown_name_is_refused(mcp_settings, database, mcp_workspace, project):
    tagged = new_case(database, mcp_workspace, project, "带标签的", at=moment(2), tags=("smoke",))
    new_case(database, mcp_workspace, project, "没标签的", at=moment(3))

    async with live_session(mcp_settings) as client:
        data = await ok_data(client, "aita_list_cases", listing(mcp_workspace, project, tags=["Smoke"]))
        error = await error_body(client, "aita_list_cases", listing(mcp_workspace, project, tags=["nope"]))

    assert [item["case_id"] for item in data["items"]] == [tagged["case_id"]]
    # Filtering on nothing would answer "this project has no such case" to a question about a typo (§10).
    assert error["code"] == "VALIDATION_ERROR"
    assert error["details"]["unknown_tags"] == ["nope"]
    assert error["next_action"] == "fix_input"


async def test_a_case_cursor_continues_only_the_query_that_minted_it(
    mcp_settings, database, mcp_workspace, project
):
    oldest = new_case(database, mcp_workspace, project, "最早", at=moment(2))
    middle = new_case(database, mcp_workspace, project, "中间", at=moment(3))
    newest = new_case(database, mcp_workspace, project, "最新", at=moment(4))

    async with live_session(mcp_settings) as client:
        items, cursor = await page_items(client, "aita_list_cases", listing(mcp_workspace, project, limit=1))
        assert [item["case_id"] for item in items] == [newest["case_id"]]
        assert cursor

        # A cursor is a bookmark in *this* query, so a page taken after a different search has to start
        # over rather than continue a question nobody asked (§11).
        crossed = await error_body(
            client, "aita_list_cases", listing(mcp_workspace, project, limit=1, cursor=cursor, search="中间")
        )
        assert crossed["details"]["reason"] == "cursor_does_not_match"

        second, after_middle = await page_items(
            client, "aita_list_cases", listing(mcp_workspace, project, limit=1, cursor=cursor)
        )
        assert [item["case_id"] for item in second] == [middle["case_id"]]

        third, end = await page_items(
            client, "aita_list_cases", listing(mcp_workspace, project, limit=1, cursor=after_middle)
        )
        assert [item["case_id"] for item in third] == [oldest["case_id"]]
        # The last page answers no cursor: a marker that yields an empty page is not a bookmark (§11).
        assert end is None

        # Re-asking the first page with a wider `limit` is the same question, so the same cursor holds.
        _, narrow = await page_items(client, "aita_list_cases", listing(mcp_workspace, project, limit=1))
        wide, _ = await page_items(client, "aita_list_cases", listing(mcp_workspace, project, limit=100, cursor=narrow))
        assert len(wide) == 2


async def test_a_page_is_shortened_and_continued_while_whole_rows_remain(tmp_path, database, mcp_workspace):
    """§11 - a page that would outgrow the metadata budget drops rows and says to continue, not truncate."""
    tight = live_settings(database, tmp_path, mcp_max_metadata_response_bytes=2000)
    workspace = seed_workspace(database, tight)
    project = new_project(database, workspace, "tight-cases", policy=OPEN, at=moment(1))
    for index in range(6):
        new_case(database, workspace, project, f"用例 {index}", at=moment(10 + index), tags=("smoke",))

    async with live_session(tight) as client:
        first, cursor = await page_items(client, "aita_list_cases", listing(workspace, project, limit=6))
        assert 0 < len(first) < 6, "the budget did not shorten anything"
        assert cursor
        pages = await drain(client, "aita_list_cases", listing(workspace, project, limit=6))

    # Every row is still reachable: the dropped tail continues from the re-minted marker.
    assert len(pages) == 6
    assert {item["case_id"] for item in first} <= {item["case_id"] for item in pages}


async def test_a_page_that_cannot_fit_even_one_row_is_refused(tmp_path, database, mcp_workspace):
    tiny = live_settings(database, tmp_path, mcp_max_metadata_response_bytes=300)
    workspace = seed_workspace(database, tiny)
    project = new_project(database, workspace, "tiny-cases", policy=OPEN, at=moment(1))
    new_case(database, workspace, project, "最小的页", at=moment(2))

    async with live_session(tiny) as client:
        error = await error_body(client, "aita_list_cases", listing(workspace, project, limit=20))

    assert error["code"] == "RESULT_TOO_LARGE"
    assert error["details"]["limit_bytes"] == 300
    assert error["next_action"] == "narrow_request"


# --------------------------------------------------------------------------------------
# aita_get_case
# --------------------------------------------------------------------------------------


async def test_get_case_sends_the_complete_markdown_only_when_called_for_it(
    mcp_settings, database, mcp_workspace, project
):
    ids = new_case(database, mcp_workspace, project, "结算回归", at=moment(2), markdown=LONG_BODY)
    read = ask(mcp_workspace, case_id=ids["case_id"])

    async with live_session(mcp_settings) as client:
        metadata = await ok_data(client, "aita_get_case", read)
        with_body = await ok_data(client, "aita_get_case", {**read, "include_markdown": True})

    assert metadata["content_available"] is True
    # Not asked for, so not present - which is a different answer from "the case is empty" (§6.6).
    assert "markdown" not in metadata
    assert metadata["current_revision_id"] == ids["revision_id"]

    # The 2,000-character summary clip does not apply to the full text (§11), and the byte count is the
    # UTF-8 size of exactly what was sent.
    assert with_body["markdown"] == LONG_BODY
    assert len(with_body["markdown"]) > 2000
    assert with_body["markdown_bytes"] == len(LONG_BODY.encode("utf-8"))
    assert with_body["source_digest"] == source_digest(LONG_BODY)


async def test_explicit_markdown_on_a_project_that_keeps_its_content_is_refused(tmp_path, database, mcp_workspace):
    settings = live_settings(database, tmp_path)
    workspace = seed_workspace(database, settings)
    closed = new_project(database, workspace, "quiet", policy=CONTENT_OFF, at=moment(1))
    ids = new_case(database, workspace, closed, "保密的", at=moment(2))
    read = ask(workspace, case_id=ids["case_id"])

    async with live_session(settings) as client:
        error = await error_body(client, "aita_get_case", {**read, "include_markdown": True})
        data = await ok_data(client, "aita_get_case", read)

    assert error["code"] == "DATA_POLICY_DENIED"
    assert error["details"]["flag"] == "allow_case_content"
    assert error["details"]["tool"] == "aita_get_case"
    assert error["next_action"] == "narrow_request"

    assert data["content_available"] is False
    for hidden in ("name", "title", "description", "tags", "markdown", "markdown_bytes"):
        assert hidden not in data
    # Identifiers and state stay: the policy hides the project's prose, not the ability to point at a case.
    assert data["case_id"] == ids["case_id"]
    assert data["row_version"] == 4


async def test_get_case_names_where_to_ask_about_the_compilation(
    mcp_settings, database, mcp_workspace, project
):
    ids = new_case(database, mcp_workspace, project, "未编译", at=moment(2))
    read = ask(mcp_workspace, case_id=ids["case_id"])

    async with live_session(mcp_settings) as client:
        bare = await ok_data(client, "aita_get_case", read)
        artifact = new_compile(
            database,
            mcp_workspace,
            project,
            ids["revision_id"],
            status=CompileStatus.SUCCEEDED.value,
            at=moment(3),
        )
        compiled = await ok_data(client, "aita_get_case", read)

    assert bare["compilation_lookup"] == {"revision_id": ids["revision_id"]}
    # Once an attempt exists, the answer names the id to pin (§6.3: "后续应固定使用此 ID").
    assert compiled["compilation_lookup"] == {"revision_id": ids["revision_id"], "compile_artifact_id": artifact}


async def test_a_second_revision_is_what_the_page_and_the_read_both_report(
    mcp_settings, database, mcp_workspace, project
):
    ids = new_case(database, mcp_workspace, project, "演进中的", at=moment(2))
    newer = new_revision(
        database, mcp_workspace, project, ids["case_id"], version=2, markdown=SECOND_REVISION, at=moment(3)
    )

    async with live_session(mcp_settings) as client:
        data = await ok_data(client, "aita_get_case", ask(mcp_workspace, case_id=ids["case_id"]))
        items, _ = await page_items(client, "aita_list_cases", listing(mcp_workspace, project))

    assert data["current_revision_id"] == newer
    assert data["revision_version"] == 2
    assert data["row_version"] == 5
    assert items[0]["revision_id"] == newer
    # The digest moves with the text, which is what makes it usable as an optimistic precondition (§6.4).
    assert data["source_digest"] == source_digest(SECOND_REVISION)


async def test_an_unknown_or_foreign_case_is_not_found(mcp_settings, database, mcp_workspace, project):
    foreign_tenant, _ = other_tenant(database, mcp_workspace, mcp_workspace["engineer_user_id"])
    ids = new_case(database, mcp_workspace, project, "别人的", at=moment(2))

    async with live_session(mcp_settings) as client:
        missing = await error_body(client, "aita_get_case", ask(mcp_workspace, case_id="0" * 32))
        foreign = await error_body(client, "aita_get_case", {"tenant_id": foreign_tenant, "case_id": ids["case_id"]})

    assert missing["code"] == "NOT_FOUND"
    # Another tenant's case answers exactly like no case at all, so the id leaks nothing (§14.1).
    assert foreign["code"] == "NOT_FOUND"


# --------------------------------------------------------------------------------------
# aita_get_compilation
# --------------------------------------------------------------------------------------


async def test_the_newest_attempt_wins_and_the_id_breaks_a_tie(mcp_settings, database, mcp_workspace, project):
    ids = new_case(database, mcp_workspace, project, "两次编译", at=moment(2))
    revision = ids["revision_id"]
    oldest = new_compile(
        database, mcp_workspace, project, revision, status=CompileStatus.FAILED.value, at=moment(3)
    )
    same_tick = moment(4)
    twin_a = new_compile(
        database, mcp_workspace, project, revision, status=CompileStatus.SUCCEEDED.value, at=same_tick
    )
    twin_b = new_compile(
        database, mcp_workspace, project, revision, status=CompileStatus.RUNNING.value, at=same_tick
    )

    async with live_session(mcp_settings) as client:
        data = await ok_data(client, "aita_get_compilation", ask(mcp_workspace, revision_id=revision))
        by_id = await ok_data(client, "aita_get_compilation", ask(mcp_workspace, compile_artifact_id=oldest))

    # `created_at DESC, id DESC` (§6.3): two attempts in one tick are still one honest answer, and the
    # tie is broken by the id rather than by whichever row the engine happened to reach first.
    assert data["compile_artifact_id"] == max(twin_a, twin_b)
    assert data["lookup_status"] == "FOUND"
    # Asking by an id means that attempt, whatever has happened to the revision since (§6.4).
    assert by_id["compile_artifact_id"] == oldest
    assert by_id["compile_status"] == CompileStatus.FAILED.value


async def test_a_revision_with_no_attempt_answers_not_created(mcp_settings, database, mcp_workspace, project):
    ids = new_case(database, mcp_workspace, project, "排队中的", at=moment(2))

    async with live_session(mcp_settings) as client:
        data = await ok_data(client, "aita_get_compilation", ask(mcp_workspace, revision_id=ids["revision_id"]))

    assert data["lookup_status"] == "NOT_CREATED"
    assert data["compile_status"] is None
    assert data["compile_artifact_id"] is None
    assert data["executable"] is False
    assert data["diagnostics"] == []
    # A save queues the compile, so the answer is a poll suggestion rather than an error (§6.3, §8.1).
    assert data["recommended_poll_after_ms"] in range(1000, 5001)


@pytest.mark.parametrize(
    ("status", "confirmed", "executable"),
    [
        (CompileStatus.SUCCEEDED.value, False, True),
        (CompileStatus.NEEDS_REVIEW.value, False, False),
        (CompileStatus.NEEDS_REVIEW.value, True, True),
        (CompileStatus.FAILED.value, False, False),
        (CompileStatus.PENDING.value, False, False),
        (CompileStatus.RUNNING.value, False, False),
    ],
)
async def test_executable_is_a_statement_about_the_artifact_that_was_named(
    mcp_settings, database, mcp_workspace, project, status, confirmed, executable
):
    ids = new_case(database, mcp_workspace, project, "可执行性", at=moment(2))
    artifact = new_compile(
        database, mcp_workspace, project, ids["revision_id"], status=status, at=moment(3), confirmed=confirmed
    )

    async with live_session(mcp_settings) as client:
        data = await ok_data(client, "aita_get_compilation", ask(mcp_workspace, compile_artifact_id=artifact))

    assert data["executable"] is executable
    assert data["compile_status"] == status
    settled = status in {CompileStatus.SUCCEEDED.value, CompileStatus.NEEDS_REVIEW.value, CompileStatus.FAILED.value}
    assert (data["recommended_poll_after_ms"] is None) is settled


async def test_exactly_one_of_artifact_and_revision_is_required(mcp_settings, database, mcp_workspace, project):
    ids = new_case(database, mcp_workspace, project, "两种入口", at=moment(2))
    artifact = new_compile(
        database, mcp_workspace, project, ids["revision_id"], status=CompileStatus.SUCCEEDED.value, at=moment(3)
    )

    async with live_session(mcp_settings) as client:
        both = await error_body(
            client,
            "aita_get_compilation",
            ask(mcp_workspace, revision_id=ids["revision_id"], compile_artifact_id=artifact),
        )
        neither = await error_body(client, "aita_get_compilation", ask(mcp_workspace))

        # A blank id means "not given", so this is still the one-id question rather than a lookup of nothing.
        blank = await ok_data(
            client,
            "aita_get_compilation",
            ask(mcp_workspace, revision_id=ids["revision_id"], compile_artifact_id="  "),
        )

    assert both["code"] == "VALIDATION_ERROR"
    assert both["next_action"] == "fix_input"
    assert neither["code"] == "VALIDATION_ERROR"
    assert blank["compile_artifact_id"] == artifact


async def test_diagnostics_page_by_index_inside_the_fixed_artifact(mcp_settings, database, mcp_workspace, project):
    ids = new_case(database, mcp_workspace, project, "满是问题的", at=moment(2))
    diagnostics = tuple(
        {"code": f"E{index}", "severity": "ERROR", "message": f"第 {index} 步缺少断言", "step_id": f"s{index}"}
        for index in range(25)
    )
    artifact = new_compile(
        database,
        mcp_workspace,
        project,
        ids["revision_id"],
        status=CompileStatus.FAILED.value,
        at=moment(3),
        diagnostics=diagnostics,
    )

    async with live_session(mcp_settings) as client:
        first = await call_tool(
            client, "aita_get_compilation", ask(mcp_workspace, revision_id=ids["revision_id"], diagnostics_limit=20)
        )
        assert len(first["data"]["diagnostics"]) == 20
        assert first["next_cursor"]

        second = await call_tool(
            client,
            "aita_get_compilation",
            ask(
                mcp_workspace,
                compile_artifact_id=artifact,
                diagnostics_limit=20,
                cursor=first["next_cursor"],
            ),
        )

    # Ordered by the index inside the fixed artifact (§11), so a continuation is not a reshuffle.
    assert [item["code"] for item in second["data"]["diagnostics"]] == [f"E{index}" for index in range(20, 25)]
    assert second["next_cursor"] is None


async def test_a_diagnostic_cursor_refuses_a_revision_whose_newest_attempt_moved(
    mcp_settings, database, mcp_workspace, project
):
    ids = new_case(database, mcp_workspace, project, "换了目标", at=moment(2))
    first = new_compile(
        database,
        mcp_workspace,
        project,
        ids["revision_id"],
        status=CompileStatus.FAILED.value,
        at=moment(3),
        diagnostics=tuple({"code": f"E{index}", "message": "缺少断言"} for index in range(30)),
    )

    async with live_session(mcp_settings) as client:
        by_revision = ask(mcp_workspace, revision_id=ids["revision_id"], diagnostics_limit=20)
        page = await call_tool(client, "aita_get_compilation", by_revision)
        cursor = page["next_cursor"]
        assert cursor

        # A newer attempt has since landed, so the revision now points somewhere else; continuing the old
        # bookmark as if it were the same question would mix two artifacts' diagnostics (§6.6).
        new_compile(
            database,
            mcp_workspace,
            project,
            ids["revision_id"],
            status=CompileStatus.SUCCEEDED.value,
            at=moment(4),
        )
        moved = await error_body(client, "aita_get_compilation", {**by_revision, "cursor": cursor})
        assert moved["details"]["reason"] == "cursor_does_not_match"

        # Pinned to the artifact the page was actually taken from, the same cursor still works (§6.6).
        rest = await call_tool(
            client,
            "aita_get_compilation",
            ask(mcp_workspace, compile_artifact_id=first, diagnostics_limit=20, cursor=cursor),
        )

    assert len(rest["data"]["diagnostics"]) == 10


async def test_diagnostic_and_review_prose_never_leave_a_closed_project(tmp_path, database, mcp_workspace):
    settings = live_settings(database, tmp_path)
    workspace = seed_workspace(database, settings)
    quiet = new_project(database, workspace, "quiet-compile", policy=CONTENT_OFF, at=moment(1))
    ids = new_case(database, workspace, quiet, "保密诊断", at=moment(2))
    new_compile(
        database,
        workspace,
        quiet,
        ids["revision_id"],
        status=CompileStatus.FAILED.value,
        at=moment(3),
        diagnostics=(
            {
                "code": "TARGET_NEEDS_LOCATOR",
                "severity": "ERROR",
                "message": "步骤 3 的中文描述：点击提交按钮",
                "step_id": "s3",
                "suggestion": "改写为 yaml 块",
            },
        ),
        review_items=(
            {
                "step_id": "s3",
                "source_text": "点击提交按钮",
                "generated": {"css": "#submit"},
                "reason": "derived_locator_candidate",
            },
        ),
    )

    async with live_session(settings) as client:
        data = await ok_data(client, "aita_get_compilation", ask(workspace, revision_id=ids["revision_id"]))
        denied = await error_body(
            client, "aita_get_compilation", ask(workspace, revision_id=ids["revision_id"], include_ir=True)
        )

    assert data["content_available"] is False
    assert data["diagnostics"] == [{"code": "TARGET_NEEDS_LOCATOR", "severity": "ERROR", "step_id": "s3"}]
    assert data["review_items"] == [{"step_id": "s3", "reason": "derived_locator_candidate"}]
    serialized = json.dumps(data, ensure_ascii=False)
    for leak in ("点击提交按钮", "改写为", "中文描述", "#submit", "css"):
        assert leak not in serialized
    # An explicit request for IR is a refusal, not a metadata answer (§6.2).
    assert denied["code"] == "DATA_POLICY_DENIED"
    assert content_reads(database, workspace["tenant_id"]) == []


async def test_more_than_twenty_review_items_point_at_the_console(
    mcp_settings, database, mcp_workspace, project
):
    ids = new_case(database, mcp_workspace, project, "需要复核", at=moment(2))
    items = tuple(
        {"step_id": f"s{index}", "reason": "derived_locator_candidate", "source_text": "点击提交"}
        for index in range(25)
    )
    artifact = new_compile(
        database,
        mcp_workspace,
        project,
        ids["revision_id"],
        status=CompileStatus.NEEDS_REVIEW.value,
        at=moment(3),
        review_items=items,
    )

    async with live_session(mcp_settings) as client:
        data = await ok_data(client, "aita_get_compilation", ask(mcp_workspace, compile_artifact_id=artifact))

    assert len(data["review_items"]) == 20
    assert data["review_required"] is True
    # The console entry is built from the configured URL and a route the console actually has (§7.4).
    assert data["review_url"].endswith("#/cases")
    assert "localhost:5173" in data["review_url"]


async def test_a_settled_artifact_with_no_review_items_does_not_advertise_one(
    mcp_settings, database, mcp_workspace, project
):
    ids = new_case(database, mcp_workspace, project, "干净的", at=moment(2))
    artifact = new_compile(
        database, mcp_workspace, project, ids["revision_id"], status=CompileStatus.SUCCEEDED.value, at=moment(3)
    )

    async with live_session(mcp_settings) as client:
        data = await ok_data(client, "aita_get_compilation", ask(mcp_workspace, compile_artifact_id=artifact))

    assert data["review_required"] is False
    assert data["review_url"] is None
    assert data["ir_digest"] is None
    assert data["executable"] is True
    # IR is only ever present for a call that asked for it (§6.6).
    assert "ir" not in data


async def test_ir_is_an_explicit_request_and_an_overlong_diagnostic_is_clipped(
    mcp_settings, database, mcp_workspace, project
):
    ids = new_case(database, mcp_workspace, project, "带 IR 的", at=moment(2))
    long_text = "断" * 3000
    artifact = new_compile(
        database,
        mcp_workspace,
        project,
        ids["revision_id"],
        status=CompileStatus.SUCCEEDED.value,
        at=moment(3),
        ir={"steps": [{"id": "s1", "action": "open"}]},
        diagnostics=({"code": "WARNING", "severity": "WARNING", "message": long_text},),
    )
    ask_by_id = ask(mcp_workspace, compile_artifact_id=artifact)

    async with live_session(mcp_settings) as client:
        with_ir = await ok_data(client, "aita_get_compilation", {**ask_by_id, "include_ir": True})
        without = await ok_data(client, "aita_get_compilation", ask_by_id)

    assert with_ir["ir"] == {"steps": [{"id": "s1", "action": "open"}]}
    assert with_ir["ir_digest"] == "sha256:" + "b" * 64
    assert "ir" not in without
    # Free text in a diagnostic is a summary and is clipped; a case's full Markdown is not, and is not (§11).
    assert len(without["diagnostics"][0]["message"]) == 2000


# --------------------------------------------------------------------------------------
# the gate, the audit and the indexes
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("tool", "key"),
    [("aita_list_cases", "project_id"), ("aita_get_case", "case_id"), ("aita_get_compilation", "revision_id")],
)
async def test_a_disabled_project_refuses_every_case_read_even_without_content(
    tmp_path, database, tool, key
):
    """§6.2 - `enabled` outranks the content flags, so all three refuse the same way."""
    settings = live_settings(database, tmp_path)
    workspace = seed_workspace(database, settings)
    shut = new_project(database, workspace, "shut", policy=CLOSED, at=moment(1))
    ids = new_case(database, workspace, shut, "关着的", at=moment(2))
    arguments = {"project_id": shut, "case_id": ids["case_id"], "revision_id": ids["revision_id"]}[key]

    async with live_session(settings) as client:
        error = await error_body(client, tool, {"tenant_id": workspace["tenant_id"], key: arguments})

    assert error["code"] == "MCP_PROJECT_DISABLED"
    assert error["details"]["tool"] == tool
    assert content_reads(database, workspace["tenant_id"]) == []


async def test_a_content_bearing_read_writes_its_access_audit_before_it_sends(
    mcp_settings, database, mcp_workspace, project
):
    ids = new_case(database, mcp_workspace, project, "被读过的", at=moment(2))

    async with live_session(mcp_settings) as client:
        data = await ok_data(
            client, "aita_get_case", {**ask(mcp_workspace, case_id=ids["case_id"]), "include_markdown": True}
        )
        listed, _ = await page_items(client, "aita_list_cases", listing(mcp_workspace, project))
    assert listed

    rows = content_reads(database, mcp_workspace["tenant_id"])
    assert [(row.operation, row.resource_type) for row in rows] == [
        ("mcp.content.read", "case"),
        ("mcp.content.read", "case"),
    ]
    assert rows[0].resource_id == ids["case_id"]
    assert rows[0].detail["markdown_sent"] is True
    assert rows[1].resource_id is None
    assert rows[1].detail["tool"] == "aita_list_cases"
    assert data["markdown"] == DEFAULT_MARKDOWN
    # The trail records that content left and under which flag - never the content itself (§11).
    assert "被读过的" not in json.dumps([row.detail for row in rows], ensure_ascii=False)


async def test_a_compile_read_audits_the_artifact_it_described(mcp_settings, database, mcp_workspace, project):
    ids = new_case(database, mcp_workspace, project, "被编译过的", at=moment(2))
    artifact = new_compile(
        database,
        mcp_workspace,
        project,
        ids["revision_id"],
        status=CompileStatus.FAILED.value,
        at=moment(3),
        diagnostics=({"code": "E1", "message": "缺少断言"},),
    )

    async with live_session(mcp_settings) as client:
        data = await ok_data(client, "aita_get_compilation", ask(mcp_workspace, compile_artifact_id=artifact))

    rows = content_reads(database, mcp_workspace["tenant_id"])
    assert len(rows) == 1
    assert rows[0].resource_type == "compile_artifact"
    assert rows[0].resource_id == artifact
    assert rows[0].detail["diagnostics"] == len(data["diagnostics"])
    assert rows[0].detail["ir_sent"] is False


async def test_an_audit_failure_sends_no_content(mcp_settings, database, mcp_workspace, project, monkeypatch):
    """§11 - 审计失败不外发: if the access row cannot be written, the case text does not leave."""

    def refused(*_args: Any, **_kwargs: Any) -> None:
        raise SQLAlchemyError("the audit store is down")

    monkeypatch.setattr(AuditRepository, "append", refused)
    ids = new_case(database, mcp_workspace, project, "发不出去的", at=moment(2))

    async with live_session(mcp_settings) as client:
        error = await error_body(
            client, "aita_get_case", {**ask(mcp_workspace, case_id=ids["case_id"]), "include_markdown": True}
        )

    assert error["code"] == "DEPENDENCY_UNAVAILABLE"
    assert error["retryable"] is True
    assert "发不出去的" not in json.dumps(error, ensure_ascii=False)
    assert content_reads(database, mcp_workspace["tenant_id"]) == []


def test_the_case_page_and_the_latest_attempt_lookup_use_their_indexes(database: Any, mcp_workspace) -> None:
    """§11 - a bounded page is a plan, not an intention: both reads must reach the composite indexes.

    SQLite reports a sort it could not serve from an index as `USE TEMP B-TREE FOR ORDER BY`, which is the
    shape this page is not allowed to have: the case list is ordered by `(created_at DESC, id DESC)` and
    the compile summary is a `LIMIT 1` on the newest attempt for each row (§6.3).
    """
    project_id = new_project(database, mcp_workspace, "planned", policy=OPEN, at=moment(1))
    with database.session() as session:
        page = _plan(
            session,
            case_queries.case_page_statement(tenant_id=mcp_workspace["tenant_id"], project_id=project_id, limit=20),
        )
        attempt = _plan(
            session,
            latest_attempt(select(CompileArtifact.id)).where(
                CompileArtifact.tenant_id == mcp_workspace["tenant_id"],
                CompileArtifact.revision_id == "revision-under-test",
            ),
        )

    assert "ix_test_case_project_created" in page
    assert "TEMP B-TREE" not in page.upper()
    assert "ix_compile_artifact_revision_latest" in attempt
    assert "TEMP B-TREE" not in attempt.upper()


def _plan(session: Any, statement: Any) -> str:
    sql = str(statement.compile(compile_kwargs={"literal_binds": True}))
    rows = session.execute(text(f"EXPLAIN QUERY PLAN {sql}")).all()
    return "\n".join(" ".join(str(value) for value in tuple(row)) for row in rows)
