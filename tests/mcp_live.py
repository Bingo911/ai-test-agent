"""The live-protocol harness the M2 read tests share.

These cases drive a connected MCP client through an in-process ASGI transport rather than calling the
adapters, because what is under test only exists at the wire: the published argument bounds, the cursor a
client gets back and replays, whether a hidden name is *absent* or `null`, and whether one call holds one
database connection.

It lives here rather than in one test module because the read plane spans nine tools, and a harness
copied twice is a harness that gets fixed twice.
"""

from __future__ import annotations

import json
import time
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import datetime, timedelta
from typing import Any

import httpx2
from backend.app.config import Settings
from backend.app.db.base import utcnow
from backend.app.db.bootstrap import ensure_development_workspace
from backend.app.db.models import AuditLog, CompileStatus, Project, Tenant
from backend.app.domain.enums import Role
from backend.app.domain.mcp_policy import POLICY_KEY
from backend.app.main import create_app
from backend.app.mcp.auth import VerifiedPrincipal
from backend.app.mcp.callcontext import CALL_STARTED_STATE_KEY, PRINCIPAL_STATE_KEY, REQUEST_ID_STATE_KEY
from backend.app.mcp.server import McpServices, McpToolGateMiddleware
from backend.app.mcp.transport import MCP_PATH
from mcp.client.client import Client
from mcp.client.streamable_http import streamable_http_client
from sqlalchemy import select
from starlette.requests import Request

HOST = "127.0.0.1:8000"
BASE = f"http://{HOST}"
CLOSED = {"enabled": False, "allow_case_content": False, "allow_report_details": False, "allow_server_ai": False}
#: `allow_server_ai` stays off even here: it governs the platform calling a model, which no read tests (§5.5).
OPEN = {"enabled": True, "allow_case_content": True, "allow_report_details": True, "allow_server_ai": False}
CONTENT_OFF = dict(OPEN, allow_case_content=False)
#: The three documents a run holds that no MCP read may put on the wire (§11). Seeded with markers rather
#: than left empty, because an assertion that a field is absent proves nothing about a row that has nothing
#: in it - and "the handler dumped the ORM row" and "the handler projected the columns" look identical then.
SENTINEL_IR = "SENTINEL-IR-DOCUMENT"
SENTINEL_SNAPSHOT = "SENTINEL-RUN-VARIABLE"
SENTINEL_DETAIL = "SENTINEL-STACK-TRACE"
SENTINEL_STEP_DETAIL = "SENTINEL-STEP-STACK-TRACE"
SENTINELS = (SENTINEL_IR, SENTINEL_SNAPSHOT, SENTINEL_DETAIL, SENTINEL_STEP_DETAIL)
#: One case body, shared because the reads under test never parse it: only its heading and its size are
#: ever part of an answer.
DEFAULT_MARKDOWN = """---
dsl_version: "1.0"
tags: [smoke]
---
# 登录冒烟测试

1. goto ${base_url}/login
2. fill #username demo
3. click #submit
4. expect text #welcome contains Welcome
"""


def title_of(markdown: str) -> str:
    """The revision's title as the parser would derive it: the first heading, not the front matter."""
    for line in markdown.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return markdown.splitlines()[0].strip() or "untitled"


def live_settings(database: Any, tmp_path: Any, **overrides: Any) -> Settings:
    """One replica's Settings: the defaults every MCP test shares, with this test's own keys on top.

    The overrides land on a dict rather than on the constructor, so a caller can give a replica its own
    `data_dir` or `redis_url` - which is what a second process in the same test has to differ in.
    """
    overrides.setdefault("mcp_enabled", True)
    # Off unless a caller asks: the read and write planes never reach a model, and a test that turns the
    # provider on has to say so in its own arguments rather than inherit it from the environment.
    overrides.setdefault("ai_enabled", False)
    values: dict[str, Any] = {
        "app_env": "test",
        "database_url": database.url,
        "data_dir": tmp_path / "data",
        "queue_backend": "inprocess",
        "object_store": "local",
        "redis_url": "",
        "log_level": "WARNING",
    }
    values.update(overrides)
    settings = Settings(**values)
    settings.validate_runtime()
    settings.ensure_dirs()
    return settings


def seed_workspace(database: Any, settings: Settings) -> dict[str, str]:
    """The development workspace, seeded with these settings so the demo project carries an open policy.

    `ensure_development_workspace` writes the policy only for a build that serves MCP, and these tests are
    about what the policy does, so the seed has to be made with the same Settings the app uses (§5.5).
    """
    with database.session() as session:
        ids = ensure_development_workspace(session, settings)
        session.commit()
    return ids


class Transport:
    """Where a client's HTTP messages go: one app, or whichever app a caller's transport decides on.

    `transport` is the seam a multi-replica test needs - one conversation whose requests land on
    different apps is exactly the thing a sticky-session dependency would break, and it cannot be
    expressed by handing the client a single ASGI app.
    """

    def __init__(self, app: Any, token: str, *, transport: Any | None = None) -> None:
        self._client = httpx2.AsyncClient(
            transport=transport or httpx2.ASGITransport(app=app),
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
async def live_session(settings: Settings):
    """One started app and one connected client, sharing the event loop this test runs on (§4.3)."""
    app = create_app(settings)
    bundle = app.state.mcp
    await bundle.start()
    try:
        async with Client(Transport(app, settings.dev_engineer_token), mode="auto") as client:  # type: ignore[arg-type]
            yield client
    finally:
        await bundle.stop()


async def call_tool(client: Client, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
    result = await client.call_tool(tool, arguments)
    envelope = result.structured_content
    assert envelope is not None, result.content
    # Both channels carry the same document, so a test can read one and still mean the other (§6.1).
    assert json.loads(result.content[0].text) == envelope
    return envelope


async def ok_data(client: Client, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
    envelope = await call_tool(client, tool, arguments)
    assert envelope["ok"] is True, envelope
    assert envelope["error"] is None
    return envelope["data"]


async def error_body(client: Client, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
    envelope = await call_tool(client, tool, arguments)
    assert envelope["ok"] is False, envelope
    assert envelope["data"] is None
    return envelope["error"]


async def page_items(client: Client, tool: str, arguments: dict[str, Any]) -> tuple[list[dict[str, Any]], str | None]:
    """A list answer as its items plus the envelope's page marker: `next_cursor` is not inside `data` (§6.1).

    Keeping the cursor on the envelope rather than repeating it in the payload is what stops a client from
    having two sources of truth for one page, so a test that read it out of `data` would be asserting a
    shape this build never sends.
    """
    envelope = await call_tool(client, tool, arguments)
    assert envelope["ok"] is True, envelope
    return envelope["data"]["items"], envelope["next_cursor"]


def moment(offset_seconds: int) -> datetime:
    """A creation time for a row this test writes, measured from now rather than from a calendar date.

    `created_at DESC, id DESC` is the order under test, and the seeded demo workspace joined its tenant at
    `utcnow()` when the seed ran. Rows pinned to a past date would therefore land *behind* the seed and make
    every expected page depend on a row the test did not write, so this test's own rows are newer.
    """
    return utcnow() + timedelta(seconds=offset_seconds)


def new_project(
    database: Any, workspace: dict[str, str], name: str, *, policy: dict[str, bool], at: datetime
) -> str:
    """A project written directly: the ordering tests need creation times no REST call can set.

    No membership row is written, because the seeded engineer holds a tenant-wide role and therefore sees
    every project in the tenant; a test that wants the non-member answer writes that case on purpose.
    """
    from backend.app.db.base import new_id

    project_id = new_id()
    with database.session() as session:
        session.add(
            Project(
                id=project_id,
                tenant_id=workspace["tenant_id"],
                name=name,
                display_name=f"Display {name}",
                settings={POLICY_KEY: dict(policy)},
                created_at=at,
                updated_at=at,
            )
        )
        session.commit()
    return project_id


#: What a real environment publishes its config as: the same shape the development seed writes, because a
#: run rejects a browser or a host this document does not allow (§6.4).
RUN_ENVIRONMENT_CONFIG: dict[str, Any] = {
    "base_url": "https://example.com",
    "allowed_domains": ["example.com", "localhost", "127.0.0.1"],
    "allowed_protocols": ["https", "http"],
    "browsers": ["chromium", "chrome"],
    "viewport": {"width": 1280, "height": 720},
    "evidence": {"mode": "NORMAL", "trace": "off", "video": "off"},
    "variables": {},
}


def new_environment(
    database: Any,
    workspace: dict[str, str],
    project_id: str,
    *,
    at: datetime,
    name: str = "run-harness",
    config: dict[str, Any] | None = None,
    archived: bool = False,
) -> str:
    """One environment with a published revision, in the project a run will be started in.

    A run names an environment revision and nothing else (§6.4), and the platform refuses a revision that
    belongs to another project - so a write test that starts a run in its own project needs its own
    environment rather than the seeded workspace's. Published through the repository the console uses, so the
    digest and the current-revision pointer are what a real publish leaves behind.
    """
    from backend.app.repositories.resources import EnvironmentRepository

    with database.session() as session:
        environments = EnvironmentRepository(session, workspace["tenant_id"])
        environment = environments.create(
            project_id=project_id, name=name, created_by=workspace["engineer_user_id"]
        )
        revision = environments.publish_revision(
            environment,
            config=dict(config or RUN_ENVIRONMENT_CONFIG),
            secret_bindings={},
            created_by=workspace["engineer_user_id"],
        )
        if archived:
            environments.archive(environment)
        environment.created_at = at
        environment.updated_at = at
        revision.created_at = at
        revision.updated_at = at
        session.commit()
        return str(revision.id)


def new_case(
    database: Any,
    workspace: dict[str, str],
    project_id: str,
    name: str,
    *,
    at: datetime,
    markdown: str = DEFAULT_MARKDOWN,
    tags: tuple[str, ...] = (),
    revision: bool = True,
    archived: bool = False,
    row_version: int = 4,
) -> dict[str, str]:
    """One case with its current revision, as the platform stores them.

    Written directly because the reads under test page by `created_at`, which no REST call lets a test
    pin, and because a case whose revision carries a title the parser derived from the Markdown is the
    only shape where "the name and the title are both case content" means anything.
    """
    from backend.app.db.base import new_id
    from backend.app.db.models import CaseTag, Tag, TestCase

    case_id = new_id()
    revision_id: str | None = None
    with database.session() as session:
        case = TestCase(
            id=case_id,
            tenant_id=workspace["tenant_id"],
            project_id=project_id,
            name=name,
            description=f"Description of {name}",
            row_version=row_version,
            created_at=at,
            updated_at=at,
            archived_at=at if archived else None,
        )
        session.add(case)
        session.flush()
        if revision:
            revision_id = add_revision(session, workspace, project_id, case_id, version=1, markdown=markdown, at=at)
            case.current_revision_id = revision_id
        for tag_name in tags:
            tag = session.scalar(
                select(Tag).where(
                    Tag.tenant_id == workspace["tenant_id"], Tag.project_id == project_id, Tag.name == tag_name
                )
            )
            if tag is None:
                tag = Tag(
                    id=new_id(),
                    tenant_id=workspace["tenant_id"],
                    project_id=project_id,
                    name=tag_name,
                    created_at=at,
                    updated_at=at,
                )
                session.add(tag)
                session.flush()
            session.add(
                CaseTag(
                    id=new_id(),
                    tenant_id=workspace["tenant_id"],
                    case_id=case_id,
                    tag_id=tag.id,
                    created_at=at,
                    updated_at=at,
                )
            )
        session.commit()
    return {"case_id": case_id, "revision_id": revision_id or ""}


def add_revision(
    session: Any,
    workspace: dict[str, str],
    project_id: str,
    case_id: str,
    *,
    version: int,
    markdown: str,
    at: datetime,
    title: str | None = None,
) -> str:
    """A revision inside a session the caller already holds, so a case can point at it in the same write."""
    from backend.app.db.base import new_id
    from backend.app.db.models import CaseRevision
    from backend.app.repositories.cases import source_digest

    revision_id = new_id()
    session.add(
        CaseRevision(
            id=revision_id,
            tenant_id=workspace["tenant_id"],
            project_id=project_id,
            case_id=case_id,
            version=version,
            markdown=markdown,
            source_digest=source_digest(markdown),
            dsl_version="1.0",
            title=title or title_of(markdown),
            created_at=at,
            updated_at=at,
        )
    )
    session.flush()
    return str(revision_id)


def new_revision(
    database: Any,
    workspace: dict[str, str],
    project_id: str,
    case_id: str,
    *,
    version: int,
    markdown: str,
    at: datetime,
    make_current: bool = True,
) -> str:
    """A further revision, and by default the case's new current one: what a page must report instead."""
    from backend.app.db.models import TestCase

    with database.session() as session:
        revision_id = add_revision(session, workspace, project_id, case_id, version=version, markdown=markdown, at=at)
        if make_current:
            case = session.get(TestCase, case_id)
            case.current_revision_id = revision_id
            case.row_version = int(case.row_version) + 1
            case.updated_at = at
        session.commit()
    return revision_id


def new_compile(
    database: Any,
    workspace: dict[str, str],
    project_id: str,
    revision_id: str,
    *,
    status: str,
    at: datetime,
    markdown: str = DEFAULT_MARKDOWN,
    diagnostics: tuple[dict[str, Any], ...] = (),
    review_items: tuple[dict[str, Any], ...] = (),
    ir: dict[str, Any] | None = None,
    confirmed: bool = False,
) -> str:
    """One compile attempt for a revision, dated by the caller because `created_at DESC, id DESC` is the rule."""
    from backend.app.db.base import new_id
    from backend.app.db.models import CompileArtifact
    from backend.app.repositories.cases import source_digest

    artifact_id = new_id()
    with database.session() as session:
        session.add(
            CompileArtifact(
                id=artifact_id,
                tenant_id=workspace["tenant_id"],
                project_id=project_id,
                revision_id=revision_id,
                status=status,
                ir=ir,
                ir_digest="sha256:" + "b" * 64 if ir is not None else None,
                source_digest=source_digest(markdown),
                compiler_version="1.0.0",
                compiler_mode="deterministic",
                diagnostics=list(diagnostics),
                review_items=list(review_items),
                usage={},
                confirmed_at=at if confirmed else None,
                confirmed_by=workspace["engineer_user_id"] if confirmed else None,
                dedupe_key=f"seed-{artifact_id}",
                created_at=at,
                updated_at=at,
            )
        )
        session.commit()
    return artifact_id


def new_execution(
    database: Any,
    workspace: dict[str, str],
    project_id: str,
    *,
    case_id: str,
    revision_id: str,
    compile_artifact_id: str,
    at: datetime,
    status: str = "FINISHED",
    outcome: str | None = "PASSED",
    error_code: str | None = None,
    state_version: int = 7,
    evidence_mode: str = "NORMAL",
    analysis_status: str = "NOT_REQUIRED",
    artifact_status: str = "COMPLETE",
    cleanup_status: str = "CLEAN",
    requested_by: str | None = None,
    steps: tuple[dict[str, Any], ...] = (),
    human_task: dict[str, Any] | None = None,
    analyses: tuple[dict[str, Any], ...] = (),
) -> str:
    """One run with its steps, evidence, open human task and analysis rows, as the platform stores them.

    Written directly for the same reason the case rows are: §8.2 is a promise about *reads* - that a report
    costs a page rather than a whole run - and the only way to test that is with a run that has more rows
    than the answer has fields. Starting a browser to make them would test the worker instead, and would
    still not let a test pin `created_at`.

    `steps` are in execution order; `step_no` defaults to the position and `step_id` to `s<no>`. A step item
    may carry `description`, `expected`, `actual`, `locator_attempts`, `resume_phase`, `duration_ms`,
    `error_code`, `status`, and `artifacts` (how many evidence rows hang off it, with `artifact_kind`,
    `upload_status` and `publish_allowed` for the aggregate's sake).
    """
    from backend.app.db.base import new_id
    from backend.app.db.models import Artifact, FailureAnalysis, HumanTask, StepExecution, TestExecution

    execution_id = new_id()
    with database.session() as session:
        session.add(
            TestExecution(
                id=execution_id,
                tenant_id=workspace["tenant_id"],
                project_id=project_id,
                case_id=case_id,
                revision_id=revision_id,
                compile_artifact_id=compile_artifact_id,
                status=status,
                outcome=outcome,
                error_code=error_code,
                # Three documents no read on this path may send, each carrying a marker so a test can prove
                # it checked rather than assumed: the IR, the environment snapshot with its run variables,
                # and the run's own failure prose (§11).
                snapshot={"variables": {"base_url": SENTINEL_SNAPSHOT}},
                ir={"case_id": case_id, "steps": [{"id": "s1", "url": SENTINEL_IR}]},
                ir_digest="sha256:" + "c" * 64,
                error_detail={"traceback": SENTINEL_DETAIL} if error_code else None,
                evidence_mode=evidence_mode,
                analysis_status=analysis_status,
                artifact_status=artifact_status,
                cleanup_status=cleanup_status,
                state_version=state_version,
                trigger="manual",
                browser="chromium",
                # Cancel authority is the requester's own run or a lead's grant (§14.2), so a test that
                # asks whether a stop is legal has to name who asked for the run.
                requested_by=requested_by,
                human_tasks_used=1 if human_task else 0,
                started_at=at,
                ended_at=at if status == "FINISHED" else None,
                active_ms=4321,
                human_ms=0 if human_task is None else 1500,
                created_at=at,
                updated_at=at,
            )
        )
        # The child rows below point back at this one through a composite foreign key onto
        # `(tenant_id, id)` rather than the primary key, and SQLAlchemy does not order those by itself;
        # with SQLite's `foreign_keys` pragma on, the flush would insert the steps before the run.
        session.flush()
        for index, item in enumerate(steps, start=1):
            no = int(item.get("step_no", index))
            step_id = str(item.get("step_id", f"s{no}"))
            assertion = {key: item[key] for key in ("expected", "actual") if item.get(key) is not None}
            if item.get("error_code"):
                # The step's diagnostics live in the same document as an assertion's two words, and only
                # those two words may be sent: a handler that handed back the document would leak this.
                assertion["traceback"] = SENTINEL_STEP_DETAIL
            session.add(
                StepExecution(
                    id=new_id(),
                    tenant_id=workspace["tenant_id"],
                    project_id=project_id,
                    execution_id=execution_id,
                    step_id=step_id,
                    step_no=no,
                    action=str(item.get("action", "goto")),
                    description=item.get("description"),
                    status=str(item.get("status", "PASSED")),
                    duration_ms=item.get("duration_ms", 100 * no),
                    error_code=item.get("error_code"),
                    error_detail=assertion or None,
                    locator_attempts=list(item.get("locator_attempts") or []),
                    resume_phase=item.get("resume_phase"),
                    artifact_ids=[],
                    created_at=at,
                    updated_at=at,
                )
            )
            for position in range(int(item.get("artifacts", 0))):
                session.add(
                    Artifact(
                        id=new_id(),
                        tenant_id=workspace["tenant_id"],
                        project_id=project_id,
                        execution_id=execution_id,
                        step_id=step_id,
                        kind=str(item.get("artifact_kind", "screenshot")),
                        name=f"{step_id}-{position}.png",
                        # Globally unique, not merely per-run unique: `uq_artifact_object_key` covers the
                        # whole table, so a fixed seed key would collide with an earlier test's run.
                        object_key=f"mcp-seed/{execution_id}/{step_id}/{position}",
                        sha256="d" * 64,
                        size=1024 + position,
                        media_type="image/png",
                        sensitivity=evidence_mode,
                        upload_status=str(item.get("upload_status", "READY")),
                        publish_allowed=bool(item.get("publish_allowed", True)),
                        metadata={},
                        created_at=at,
                        updated_at=at,
                    )
                )
        if human_task is not None:
            session.add(
                HumanTask(
                    id=new_id(),
                    tenant_id=workspace["tenant_id"],
                    project_id=project_id,
                    execution_id=execution_id,
                    step_id=str(human_task.get("step_id", "s1")),
                    reason=str(human_task.get("reason", "captcha")),
                    # Operator prose, deliberately present so a test that finds it in an answer is real.
                    detail=human_task.get("detail", "Type the six digits shown on the screen"),
                    status=str(human_task.get("status", "PENDING")),
                    deadline=human_task.get("deadline", at + timedelta(minutes=5)),
                    session_epoch=1,
                    pause_token=f"pause-{execution_id}",
                    created_at=at,
                    updated_at=at,
                )
            )
        for item in analyses:
            session.add(
                FailureAnalysis(
                    id=new_id(),
                    tenant_id=workspace["tenant_id"],
                    project_id=project_id,
                    execution_id=execution_id,
                    revision=int(item.get("revision", 1)),
                    status=str(item.get("status", "SUCCEEDED")),
                    failure_type=item.get("failure_type"),
                    reason=item.get("reason", "an analysis note that is not a stable code"),
                    suggestion=item.get("suggestion"),
                    evidence_refs=[],
                    source=str(item.get("source", "rules")),
                    usage={},
                    created_at=at,
                    updated_at=at,
                )
            )
        session.commit()
    return execution_id


def other_tenant(database: Any, workspace: dict[str, str], engineer_id: str) -> tuple[str, str]:
    """A second tenant the same subject belongs to, with one project of its own (§14.2 isolation)."""
    from backend.app.db.base import new_id
    from backend.app.db.models import ProjectMembership, TenantMembership

    tenant_id = new_id()
    project_id = new_id()
    with database.session() as session:
        session.add(Tenant(id=tenant_id, name="other", display_name="Other tenant"))
        session.flush()
        session.add(
            TenantMembership(id=new_id(), tenant_id=tenant_id, user_id=engineer_id, role=Role.ENGINEER.value)
        )
        session.add(
            Project(
                id=project_id,
                tenant_id=tenant_id,
                name="other-project",
                display_name="Belongs to the other tenant",
                settings={POLICY_KEY: dict(OPEN)},
                created_at=moment(1),
                updated_at=moment(1),
            )
        )
        session.add(
            ProjectMembership(
                id=new_id(),
                tenant_id=tenant_id,
                project_id=project_id,
                user_id=engineer_id,
                role=Role.ENGINEER.value,
            )
        )
        session.commit()
    return tenant_id, project_id


def revoke_subject(database: Any, workspace: dict[str, str]) -> None:
    """Take the caller's membership away, which is the state §9.3.1 says a call must be re-checked against.

    Both rows go: the tenant role is what answers for a project when no project row does, so leaving it
    behind would test a half-revoked subject instead of a revoked one.
    """
    from backend.app.db.models import ProjectMembership, TenantMembership

    with database.session() as session:
        session.query(TenantMembership).where(
            TenantMembership.tenant_id == workspace["tenant_id"],
            TenantMembership.user_id == workspace["engineer_user_id"],
        ).delete()
        session.query(ProjectMembership).where(
            ProjectMembership.tenant_id == workspace["tenant_id"],
            ProjectMembership.user_id == workspace["engineer_user_id"],
        ).delete()
        session.commit()


# --------------------------------------------------------------------------------------
# the rows a read test needs, and the audit it owes
# --------------------------------------------------------------------------------------


def ask(workspace: dict[str, str], **extra: Any) -> dict[str, Any]:
    """Arguments for a call in this workspace: every read names its tenant explicitly (§6.2)."""
    return {"tenant_id": workspace["tenant_id"], **extra}


def passing(count: int) -> tuple[dict[str, Any], ...]:
    """`count` steps that passed, so a page can be tested against a run with more of them than it carries."""
    return tuple({} for _ in range(count))


def failing(count: int, *, first: int = 1) -> tuple[dict[str, Any], ...]:
    return tuple(
        {
            "step_no": index,
            "status": "FAILED",
            "error_code": "assertion_failed",
            "description": f"第 {index} 步的断言失败",
            "expected": f"expected-{index}",
            "actual": f"actual-{index}",
        }
        for index in range(first, first + count)
    )


def seeded_run(database: Any, workspace: dict[str, str], project_id: str, **extra: Any) -> str:
    """A whole run, with the case, revision and compile artifact its foreign keys demand.

    The run is created from a real revision id rather than an invented one because `test_execution`
    carries foreign keys to both the revision and the artifact, and §6.2's promise that a run reports the
    digest it ran only means anything when those ids point at rows that exist.
    """
    ids = new_case(database, workspace, project_id, "回归用例", at=moment(2))
    artifact = new_compile(
        database,
        workspace,
        project_id,
        ids["revision_id"],
        status=CompileStatus.SUCCEEDED.value,
        at=moment(3),
    )
    return new_execution(
        database,
        workspace,
        project_id,
        case_id=ids["case_id"],
        revision_id=ids["revision_id"],
        compile_artifact_id=artifact,
        at=moment(4),
        **extra,
    )


def content_reads(database: Any, tenant_id: str) -> list[AuditLog]:
    """The content audit rows this tenant has, and only those: an observation log is not an audit (§11)."""
    with database.session() as session:
        rows = list(session.scalars(select(AuditLog).where(AuditLog.tenant_id == tenant_id)))
    return [row for row in rows if row.operation == "mcp.content.read"]


async def walk(client: Client, tool: str, arguments: dict[str, Any]) -> list[dict[str, Any]]:
    """Every row of a paged read, following only the cursors the platform itself minted."""
    seen: list[dict[str, Any]] = []
    cursor: str | None = None
    while True:
        envelope = await call_tool(client, tool, {**arguments, "cursor": cursor})
        assert envelope["ok"] is True, envelope
        seen.extend(envelope["data"]["items"])
        cursor = envelope["next_cursor"]
        if cursor is None:
            return seen
        assert len(seen) < 400, "the page never ended"


# --------------------------------------------------------------------------------------
# the gate, driven without a transport
# --------------------------------------------------------------------------------------


class GateContext:
    """A stand-in for the SDK's server context: the gate only ever reads these four fields.

    `method` and `params` are what it discriminates on, `request` carries the verified principal, and
    `request_id` is what separates a request from a notification.
    """

    def __init__(self, principal: VerifiedPrincipal, params: dict[str, Any], *, method: str = "tools/call") -> None:
        self.method = method
        self.request_id: str | None = "1"
        self.params = params
        self.request = Request(
            {
                "type": "http",
                "method": "POST",
                "path": MCP_PATH,
                "headers": [],
                "state": {
                    PRINCIPAL_STATE_KEY: principal,
                    REQUEST_ID_STATE_KEY: "req_gate",
                    CALL_STARTED_STATE_KEY: time.monotonic(),
                },
            }
        )


def tool_specs(services: McpServices) -> dict[str, Any]:
    """The registry's tool specs, with the import that fills the registry done here.

    `build_mcp_server` imports the adapters module for its side effect, and a gate built without that
    import sees an empty registry - which makes it wave every call through, because a name it does not
    know is the SDK's to refuse. A check that only fires when some other file happened to import first
    is not a check, so the fill and the proof that it filled are both part of this helper.
    """
    from backend.app.mcp import server as registry
    from backend.app.mcp import tools  # noqa: F401  - importing this is what registers the tools

    specs = {name: factory(services) for name, factory in registry._TOOL_FACTORIES.items()}
    assert "aita_get_context" in specs, "the tool registry was never filled"
    return specs


def gate(services: McpServices) -> tuple[McpToolGateMiddleware, Any, list[str]]:
    """The middleware under test, a `call_next` spy, and the list it records reaching a handler in."""
    specs = tool_specs(services)
    reached: list[str] = []

    async def call_next(_ctx: Any) -> str:
        reached.append("handler")
        return "reached"

    return McpToolGateMiddleware(services, specs), call_next, reached
