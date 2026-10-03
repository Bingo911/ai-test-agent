"""The MCP plane against a public site: one client authors, compiles and runs a case on a real browser.

Every other MCP test navigates to the loopback fixture site, which is what keeps the suite fast and
repeatable. It also means nothing in the suite has ever proved the claim the product rests on - that a case
written through `/mcp`, compiled by the platform's own worker and started by `aita_run_test` ends up driving
a browser to the address an environment names and comes back with evidence. This module does that against a
real origin, with the real outbox, dispatcher, compile worker, execution worker and Chromium.

It is opt-in because it leaves the machine. `AITA_LIVE_MCP=1` runs the site visit; the default target is
`https://www.baidu.com` and `AITA_LIVE_BASE_URL` names another one. The case whose compile asks the
platform's own model for help additionally needs `AITA_LIVE_AI_BASE_URL`, `AITA_LIVE_AI_MODEL` and
`AITA_LIVE_AI_API_KEY` - the key is only ever read from the environment, because a credential in a tracked
file is a credential that has been published. A run that reports this module as *skipped* has verified
none of it, which is why the reason strings say what would have been covered.

"""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections.abc import Iterator
from typing import Any
from urllib.parse import urlparse

import pytest
from backend.app.config import Settings, get_settings
from backend.app.db.base import Database
from backend.app.db.bootstrap import ensure_development_workspace
from backend.app.db.models import CompileArtifact
from backend.app.orchestrator.queue import InProcessQueue
from backend.app.orchestrator.runtime import Supervisor
from backend.app.orchestrator.tasks import task_handlers
from mcp.client.client import Client

from .mcp_live import error_body, live_session, live_settings, moment, new_environment, new_project, ok_data

LIVE_BASE_URL = os.environ.get("AITA_LIVE_BASE_URL", "https://www.baidu.com").rstrip("/")
AI_BASE_URL = os.environ.get("AITA_LIVE_AI_BASE_URL", "")
AI_MODEL = os.environ.get("AITA_LIVE_AI_MODEL", "")
AI_API_KEY = os.environ.get("AITA_LIVE_AI_API_KEY", "")
#: What the run types into the box. A run variable rather than case text, so the assertion compares against
#: the value this run resolved and not against a string the case happens to contain.
KEYWORD = os.environ.get("AITA_LIVE_KEYWORD", "人工智能")

COMPILE_SETTLED = {"SUCCEEDED", "NEEDS_REVIEW", "FAILED"}
COMPILE_TIMEOUT_SECONDS = float(os.environ.get("AITA_LIVE_COMPILE_TIMEOUT", "300"))
RUN_TIMEOUT_SECONDS = float(os.environ.get("AITA_LIVE_RUN_TIMEOUT", "300"))
POLL_SECONDS = 0.5

OPEN_POLICY = {"enabled": True, "allow_case_content": True, "allow_report_details": True, "allow_server_ai": False}

LIVE_MARKDOWN = """---
dsl_version: "1.0"
tags: [live, public-site]
variables:
  keyword:
    type: string
    required: true
defaults:
  timeout_ms: 20000
---
# 在 {host} 完成一次检索

## Step 1
```yaml
action: open
url: "${env.base_url}/"
wait_until: domcontentloaded
```

## Step 2
```yaml
action: assert
condition:
  kind: page_contains
  expected: 百度
```

## Step 3
```yaml
action: input
target:
  description: 首页搜索输入框
  type: textarea
  css: "#chat-textarea"
value: "${vars.keyword}"
```

## Step 4
```yaml
action: assert
condition:
  kind: value_equals
  expected: "${vars.keyword}"
  target:
    description: 首页搜索输入框
    type: textarea
    css: "#chat-textarea"
```

## Step 5
```yaml
action: click
target:
  description: 百度一下按钮
  type: button
  css: "#chat-submit-button"
```

## Step 6
```yaml
action: wait
condition:
  kind: url_contains
  expected: "/s?"
```

## Step 7
```yaml
action: screenshot
name: live-result
```
"""

#: One prose step and nothing else. The deterministic compiler cannot read it, so this is the shape that
#: makes the platform reach a model (§6.3) - and the shape whose result a person has to confirm. The prose
#: asks for a page-text check rather than for an element, because a model that cannot see the DOM has no
#: honest way to name a locator, and a compile that failed for want of one would test the prompt instead of
#: the contract.
AI_MARKDOWN = """---
dsl_version: "1.0"
tags: [live, ai]
defaults:
  timeout_ms: 20000
---
# 让模型读懂一步自然语言

## Step 1
```yaml
action: open
url: "${env.base_url}/"
```

## Step 2
打开页面后，确认页面上的文本中包含"百度"这两个字
"""

pytestmark = pytest.mark.skipif(
    os.environ.get("AITA_LIVE_MCP") != "1",
    reason="outbound run: set AITA_LIVE_MCP=1 to drive a real site through /mcp on a real browser",
)


def live_environment_config(base_url: str) -> dict[str, Any]:
    """The document a real environment publishes, whose allowlist is the live host and nothing else.

    Derived rather than hardcoded so that pointing `AITA_LIVE_BASE_URL` at another origin tests that origin
    and cannot quietly wander elsewhere: a run that navigated to a host the operator did not name would be
    an egress failure (§14.2), and this module would then have to explain a red run it did not intend.
    """
    host = urlparse(base_url).hostname or ""
    parent = ".".join(host.split(".")[1:]) or host
    return {
        "base_url": base_url,
        "allowed_domains": sorted({host, parent}),
        "allowed_protocols": ["https", "http"],
        "browsers": ["chromium", "chrome"],
        "viewport": {"width": 1280, "height": 720},
        "evidence": {"mode": "NORMAL", "trace": "off", "video": "off"},
        "variables": {},
    }


@pytest.fixture
def live(database: Database, tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Iterator[Settings]:
    """Settings that are allowed to reach the network and, when configured, a model.

    `ai_enabled` follows the credential rather than a flag of its own: a run without a key must not build an
    adapter that fails on its first call, and the test that needs a model skips before it gets here. The
    step budget is raised because a public page is not the loopback fixture the default is sized for.

    A configured provider is also written into this process's environment, because that is where a real
    worker gets it from: `CompileWorker` is built from the cached `get_settings()`, not from the `Settings`
    object this test hands the MCP application. Setting it on the app alone would make the queue answer
    `AI_UNAVAILABLE` for a deployment that does have a model, and the compile would fail for a reason no
    operator would recognise.
    """
    configured = bool(AI_API_KEY and AI_BASE_URL and AI_MODEL)
    if configured:
        for name, value in {
            "AI_ENABLED": "true",
            "AI_BASE_URL": AI_BASE_URL,
            "AI_MODEL": AI_MODEL,
            "AI_API_KEY": AI_API_KEY,
            "AI_TIMEOUT_SECONDS": os.environ.get("AITA_LIVE_AI_TIMEOUT", "120"),
        }.items():
            monkeypatch.setenv(name, value)
        get_settings.cache_clear()
    settings = live_settings(
        database,
        tmp_path,
        ai_enabled=configured,
        ai_base_url=AI_BASE_URL,
        ai_model=AI_MODEL,
        ai_api_key=AI_API_KEY or None,
        step_timeout_max_ms=60_000,
    )
    try:
        yield settings
    finally:
        # The next test must not inherit a provider this one borrowed from the environment.
        if configured:
            get_settings.cache_clear()


@pytest.fixture
def plane(database: Database, live: Settings) -> dict[str, str]:
    """The workspace the tools will write into, seeded by the same code path a development server uses."""
    with database.session() as session:
        workspace = ensure_development_workspace(session, live)
        session.commit()
    return workspace


@pytest.fixture
def queue() -> Any:
    """The real handlers on a queue of this test's own; the supervisor is what publishes to it."""
    delivered = InProcessQueue(task_handlers(), concurrency=2)
    yield delivered
    delivered.close()


class Transcript:
    """Every answer this client received, so the leak assertions run over what was actually sent.

    Reading only the final payload would miss the step page that carried a path, and an assertion written
    against one tool's fields would miss the next tool that grows one.
    """

    def __init__(self) -> None:
        self.entries: list[str] = []

    async def ok(self, client: Client, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        data = await ok_data(client, tool, arguments)
        self.entries.append(json.dumps(data, ensure_ascii=False))
        return data

    @property
    def text(self) -> str:
        return "\n".join(self.entries)


async def settle_compile(client: Client, transcript: Transcript, tenant_id: str, artifact_id: str) -> dict[str, Any]:
    """Poll the artifact with the read tool until the worker has written its verdict (§6.3)."""
    deadline = time.monotonic() + COMPILE_TIMEOUT_SECONDS
    view: dict[str, Any] = {}
    while time.monotonic() < deadline:
        view = await transcript.ok(
            client, "aita_get_compilation", {"tenant_id": tenant_id, "compile_artifact_id": artifact_id}
        )
        if view.get("compile_status") in COMPILE_SETTLED:
            return view
        await asyncio.sleep(POLL_SECONDS)
    raise AssertionError(f"compilation {artifact_id} never settled: {view}")


async def settle_execution(
    client: Client, transcript: Transcript, tenant_id: str, execution_id: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Poll the run until it is terminal, then read its steps and its report once more."""
    deadline = time.monotonic() + RUN_TIMEOUT_SECONDS
    view: dict[str, Any] = {}
    while time.monotonic() < deadline:
        view = await transcript.ok(
            client, "aita_get_execution", {"tenant_id": tenant_id, "execution_id": execution_id}
        )
        if view.get("terminal"):
            break
        await asyncio.sleep(POLL_SECONDS)
    else:
        raise AssertionError(f"execution {execution_id} never finished: {view}")
    steps = await transcript.ok(
        client,
        "aita_get_execution_steps",
        {"tenant_id": tenant_id, "execution_id": execution_id, "limit": 20},
    )
    report = await transcript.ok(client, "aita_get_report", {"tenant_id": tenant_id, "execution_id": execution_id})
    return view, {"steps": steps, "report": report}


def summary(view: dict[str, Any], steps: dict[str, Any]) -> str:
    """Why a run is not what this test expected, in the words the platform used to say it."""
    return json.dumps(
        {
            "status": view.get("status"),
            "outcome": view.get("outcome"),
            "error_code": view.get("error_code"),
            "step_counts": view.get("step_counts"),
            "steps": [
                {
                    "step": row.get("step_id"),
                    "action": row.get("action"),
                    "status": row.get("status"),
                    "error_code": row.get("error_code"),
                }
                for row in steps.get("items", [])
            ],
        },
        ensure_ascii=False,
    )


def provider_attempt(database: Database, artifact_id: str) -> tuple[dict[str, Any], str | None]:
    """What the worker recorded about the model call, taken from the row rather than from the answer.

    The MCP projection of a compile carries no usage (§6.3 sends verdicts, not accounting), so a test that
    wants to know whether a model was *reached* has to look where the platform itself wrote it. Without
    this, an `ai_assisted` compile that failed because the provider refused the credential and one that
    failed because the model returned an unusable step look identical - and only the second is the case
    this module is about.
    """
    with database.session() as session:
        artifact = session.get(CompileArtifact, artifact_id)
        assert artifact is not None, artifact_id
        return dict(artifact.usage or {}), artifact.model


async def test_a_case_authored_over_mcp_runs_against_the_public_site(
    database: Database, live: Settings, plane: dict[str, str], tmp_path: Any, queue: Any
) -> None:
    """The whole promise in one test: `/mcp` in, a browser on a public site out, evidence back.

    Nothing here is stubbed. The project, the environment and the case are what the tools and their own
    worker produced, and the run drives Chromium against the address that environment publishes.
    """
    transcript = Transcript()
    tenant_id = plane["tenant_id"]
    project_id = new_project(database, plane, "live-public", policy=OPEN_POLICY, at=moment(1))
    environment_revision_id = new_environment(
        database,
        plane,
        project_id,
        at=moment(1),
        name="live-public",
        config=live_environment_config(LIVE_BASE_URL),
    )
    host = urlparse(LIVE_BASE_URL).hostname or "public site"

    async with live_session(live) as client:
        projects = await transcript.ok(client, "aita_list_projects", {"tenant_id": tenant_id, "limit": 50})
        assert project_id in {row["project_id"] for row in projects["items"]}, projects

        created = await transcript.ok(
            client,
            "aita_create_case",
            {
                "tenant_id": tenant_id,
                "project_id": project_id,
                "name": "live-search",
                "markdown": LIVE_MARKDOWN.replace("{host}", host),
                "idempotency_key": "live-create-search",
                "tags": ["live"],
            },
        )
        queued = await transcript.ok(
            client,
            "aita_compile_case_revision",
            {
                "tenant_id": tenant_id,
                "revision_id": created["revision_id"],
                "idempotency_key": "live-compile-search",
                "use_ai": False,
            },
        )
        assert queued["compiler_mode"] == "deterministic"

        supervisor = Supervisor(live, queue=queue, announce=True).start()
        try:
            artifact = await settle_compile(client, transcript, tenant_id, queued["compile_artifact_id"])
            assert artifact["compile_status"] == "SUCCEEDED", artifact["diagnostics"]
            # A real page under the real compiler still needs no human: every locator here was authored.
            assert artifact["review_items"] == [], artifact
            assert artifact["executable"] is True, artifact

            environments = await transcript.ok(
                client, "aita_list_environments", {"tenant_id": tenant_id, "project_id": project_id}
            )
            revision_id = environments["items"][0]["current_revision_id"]
            assert revision_id == environment_revision_id, environments

            started = await transcript.ok(
                client,
                "aita_run_test",
                {
                    "tenant_id": tenant_id,
                    "compile_artifact_id": artifact["compile_artifact_id"],
                    "expected_ir_digest": artifact["ir_digest"],
                    "environment_revision_id": revision_id,
                    "idempotency_key": "live-run-search",
                    "variables": {"keyword": KEYWORD},
                    "browser": "chromium",
                    "evidence_mode": "NORMAL",
                },
            )
            assert started["worker_available"] is True, started
            view, detail = await settle_execution(client, transcript, tenant_id, started["execution_id"])
        finally:
            supervisor.stop()

    steps, report = detail["steps"], detail["report"]
    assert view["outcome"] == "PASSED", summary(view, steps)
    rows = steps["items"]
    assert [row["action"] for row in rows] == [
        "open",
        "assert",
        "input",
        "assert",
        "click",
        "wait",
        "screenshot",
    ], rows
    assert {row["status"] for row in rows} == {"PASSED"}, summary(view, steps)
    # The evidence is the point of the run: a screenshot of a page no fixture could have produced.
    shots = [ref for row in rows for ref in row["artifact_refs"] if ref["kind"] == "SCREENSHOT"]
    assert len(shots) == 1, shots
    shot = shots[0]
    # §8.2: a reference is something to point at, never something to fetch - an id and a state, no location.
    assert set(shot) == {"ref", "kind", "size", "upload_status", "publishable"}, shot
    assert shot["ref"].startswith("artifact:"), shot
    assert shot["upload_status"] == "READY", shot
    assert shot["size"] > 0, shot
    assert report["outcome"] == "PASSED", report
    assert report["terminal"] is True, report
    # Nothing the platform holds privately may ride out with a passing answer (§11): no path on this disk,
    # no database URL, no traceback, not even the name of the field that would hold a storage key.
    leaks = [
        needle
        for needle in (str(tmp_path), "sqlite:///", "Traceback", "object_key")
        if needle and needle in transcript.text
    ]
    assert leaks == [], leaks


@pytest.mark.skipif(
    not (AI_API_KEY and AI_BASE_URL and AI_MODEL),
    reason="set AITA_LIVE_AI_BASE_URL, AITA_LIVE_AI_MODEL and AITA_LIVE_AI_API_KEY to reach a model",
)
async def test_the_platform_compiles_a_prose_step_with_its_own_model_and_says_so(
    database: Database, live: Settings, plane: dict[str, str], queue: Any, tmp_path: Any
) -> None:
    """§6.3 with MCP-AC-07: the model reads the step, and what it wrote does not become executable by itself.

    A model-authored locator is the one compile result that must stop at `NEEDS_REVIEW`, and the one the MCP
    surface must not be able to wave through - there is no confirm tool in this version. Both halves are
    asserted, because either one alone would let a quiet downgrade through.
    """
    transcript = Transcript()
    tenant_id = plane["tenant_id"]
    project_id = new_project(
        database, plane, "live-ai", policy=dict(OPEN_POLICY, allow_server_ai=True), at=moment(2)
    )
    environment_revision_id = new_environment(
        database,
        plane,
        project_id,
        at=moment(2),
        name="live-ai",
        config=live_environment_config(LIVE_BASE_URL),
    )

    async with live_session(live) as client:
        created = await transcript.ok(
            client,
            "aita_create_case",
            {
                "tenant_id": tenant_id,
                "project_id": project_id,
                "name": "live-prose-step",
                "markdown": AI_MARKDOWN,
                "idempotency_key": "live-create-prose",
            },
        )
        supervisor = Supervisor(live, queue=queue, announce=False).start()
        try:
            queued = await transcript.ok(
                client,
                "aita_compile_case_revision",
                {
                    "tenant_id": tenant_id,
                    "revision_id": created["revision_id"],
                    "idempotency_key": "live-compile-prose",
                    "use_ai": True,
                },
            )
            # The receipt reports the mode this call was accepted under, so a downgrade cannot be quiet.
            assert queued["compiler_mode"] == "ai_assisted", queued
            artifact = await settle_compile(client, transcript, tenant_id, queued["compile_artifact_id"])
            usage, model = provider_attempt(database, queued["compile_artifact_id"])
            refused = await error_body(
                client,
                "aita_run_test",
                {
                    "tenant_id": tenant_id,
                    "compile_artifact_id": queued["compile_artifact_id"],
                    "expected_ir_digest": artifact.get("ir_digest") or "sha256:" + "0" * 64,
                    "environment_revision_id": environment_revision_id,
                    "idempotency_key": "live-run-prose",
                    "variables": {},
                },
            )
        finally:
            supervisor.stop()

    # The provider was reached, not merely asked: `calls` counts completed round trips, and a refused
    # credential, a 404 or an unreachable host leaves it at zero because nothing was ever merged. Without
    # this line the test below could pass on a compile that failed for the wrong reason entirely.
    assert usage.get("calls", 0) >= 1, usage
    assert usage.get("latency_ms", 0) > 0, usage
    assert str(usage.get("model") or ""), usage
    # The row and the usage document name one model, and it is the one this deployment configured.
    assert model == usage["model"], (model, usage)
    assert AI_MODEL in str(model), (model, AI_MODEL)

    assert artifact["compiler_mode"] == "ai_assisted", artifact
    # What this proves is not that a live model got the step right. Across runs here it has returned a valid
    # assertion, an element the prose never named, and a reply that echoed its own step id - and the second
    # and third of those were correct failures. The promise under test is the one §6.3 and MCP-AC-07 make:
    # nothing a model wrote becomes executable by itself, and the reason the platform names matches the
    # artifact it actually has. The deterministic path of the same contract is covered hermetically in
    # `test_review_regressions.py`, where the model's answer is fixed and both branches are asserted exactly.
    assert artifact["compile_status"] in {"NEEDS_REVIEW", "FAILED"}, artifact["diagnostics"]
    assert artifact["executable"] is False, artifact
    if artifact["compile_status"] == "NEEDS_REVIEW":
        assert [row["reason"] for row in artifact["review_items"]] == ["ai_generated_step"], artifact["review_items"]
        assert artifact["review_url"], artifact
    else:
        errors = [item for item in artifact["diagnostics"] if item["severity"] == "ERROR"]
        assert errors, artifact["diagnostics"]
        # A model that failed is allowed to say which step it failed on; a compile with nothing to review is
        # not one that can be confirmed.
        assert any(item.get("step_id") == "s2" for item in errors), errors
        assert artifact["review_items"] == [], artifact
    assert refused["code"] == "COMPILE_REVIEW_REQUIRED", refused
    if artifact["compile_status"] == "NEEDS_REVIEW":
        wanted = "needs a human confirmation"
    else:
        wanted = "did not produce an executable IR"
    assert wanted in refused["message"], refused
    # A model's prose goes into `diagnostics`, and diagnostics go out over `/mcp`: the credential that paid
    # for the call, a path on this disk and a storage key must none of them ride along (§11).
    leaks = [
        needle
        for needle in (AI_API_KEY, "sk-", str(tmp_path), "sqlite:///", "Traceback", "object_key")
        if needle and needle in transcript.text
    ]
    assert leaks == [], leaks
