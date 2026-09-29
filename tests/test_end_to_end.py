"""The platform as a whole: save DSL -> compile -> queue -> browser -> evidence -> report (§15.3).

Nothing here calls a worker directly. The case is saved through the service, the scheduler reserves a
slot, the dispatcher publishes, the in-process queue runs the real compile and browser workers, and
the assertions read only what those workers left in the database and the object store.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from backend.app.db.models import WorkerLease
from backend.app.domain.enums import (
    AnalysisStatus,
    ArtifactKind,
    DispatchState,
    ExecutionStatus,
    Outcome,
    StepStatus,
)
from backend.app.domain.errors import ApiError, ErrorCode
from backend.app.orchestrator.queue import InProcessQueue
from backend.app.orchestrator.runtime import Supervisor
from backend.app.orchestrator.tasks import task_handlers
from backend.app.reporting.report import build_report
from backend.app.repositories.artifacts import ArtifactRepository, FailureAnalysisRepository
from backend.app.repositories.cases import CompileRepository
from backend.app.repositories.executions import ExecutionRepository
from backend.app.repositories.reservations import PoolRepository, ReservationRepository
from backend.app.repositories.resources import EnvironmentRepository
from backend.app.services.cases import CaseService
from backend.app.services.executions import ExecutionService
from backend.app.services.object_store import get_object_store
from backend.app.services.secret_store import get_secret_store
from sqlalchemy import select

SECRET_VALUE = "sup3r-s3cret-login-password"


LOGIN_MARKDOWN = """---
dsl_version: "1.0"
tags: [smoke]
variables:
  username:
    type: string
    required: true
defaults:
  timeout_ms: 8000
---
# 登录冒烟测试

## Step 1
```yaml
action: open
url: "${env.base_url}/index.html"
```

## Step 2
```yaml
action: input
target:
  description: 用户名输入框
  type: input
  css: 'input[name="username"]'
value: "${vars.username}"
```

## Step 3
```yaml
action: input
target:
  description: 密码输入框
  type: input
  css: 'input[name="password"]'
value: "${secrets.login_password}"
```

## Step 4
```yaml
action: click
target:
  description: 登录按钮
  type: button
  role: button
  name: 登录
```

## Step 5
```yaml
action: assert
condition:
  kind: page_contains
  expected: 欢迎回来
```

## Step 6
```yaml
action: screenshot
name: after-login
```
"""

FAILING_MARKDOWN = """---
dsl_version: "1.0"
---
# 断言缺陷

## Step 1
```yaml
action: open
url: "${env.base_url}/index.html"
```

## Step 2
```yaml
action: assert
timeout_ms: 1500
condition:
  kind: page_contains
  expected: 这段文字永远不会出现在页面上
```
"""


@pytest.fixture
def queue() -> InProcessQueue:
    """A queue of this test's own: the handlers are the real ones, the database is the fixture's."""
    delivered = InProcessQueue(task_handlers(), concurrency=3)
    yield delivered
    delivered.close()


def bind_site(session, workspace, settings, base_url: str) -> str:
    """Publish an environment revision that points at the loopback fixture site (§5.4)."""
    environments = EnvironmentRepository(session, workspace["tenant_id"])
    environment = environments.by_name(workspace["project_id"], "local")
    assert environment is not None
    secret = get_secret_store(settings).put(
        session,
        tenant_id=workspace["tenant_id"],
        project_id=workspace["project_id"],
        logical_name="login_password",
        value=SECRET_VALUE,
        created_by=workspace["admin_user_id"],
    )
    environments.publish_revision(
        environment,
        config={
            "base_url": base_url,
            "allowed_domains": ["127.0.0.1", "localhost"],
            "allowed_protocols": ["http", "https"],
            "browsers": ["chromium"],
            "viewport": {"width": 1280, "height": 720},
            "evidence": {"mode": "NORMAL", "trace": "off", "video": "off"},
            "variables": {},
        },
        secret_bindings={"login_password": secret.version},
        created_by=workspace["admin_user_id"],
    )
    session.commit()
    return environment.id


def wait_for_quiet(supervisor: Supervisor, queue: InProcessQueue, *, timeout: float) -> None:
    """A queue that drained while swallowing a task error is not a finished run."""
    assert supervisor.drain(timeout=timeout), queue.errors()
    assert not queue.errors(), queue.errors()


def compile_of(revision_id: str, *, tenant_id: str, database) -> dict[str, Any]:
    with database.session(tenant_id) as scope:
        artifact = CompileRepository(scope, tenant_id).latest(revision_id)
        if artifact is None:
            return {"status": None, "diagnostics": [], "ir": None}
        return {
            "status": artifact.status,
            "compiler_mode": artifact.compiler_mode,
            "diagnostics": artifact.diagnostics or [],
            "ir": artifact.ir,
        }


def steps_of(database, tenant_id: str, execution_id: str) -> list[Any]:
    with database.session(tenant_id) as scope:
        return list(ExecutionRepository(scope, tenant_id).steps(execution_id))


def step_report(database, tenant_id: str, execution_id: str) -> list[dict[str, Any]]:
    return [
        {
            "step": row.step_no,
            "action": row.action,
            "status": row.status,
            "error": row.error_code,
            "detail": row.error_detail,
        }
        for row in steps_of(database, tenant_id, execution_id)
    ]


def conclude(settings, database, tenant_id: str, execution_id: str, *, outcome: str) -> Any:
    """The finished run, or a step-by-step explanation of why it is not what this test expected."""
    done = ExecutionService(settings).get(tenant_id=tenant_id, execution_id=execution_id)
    if (done.status, done.outcome) != (ExecutionStatus.FINISHED.value, outcome):
        raise AssertionError(
            json.dumps(
                {
                    "expected": outcome,
                    "status": done.status,
                    "error_code": done.error_code,
                    "error_detail": done.error_detail,
                    "steps": step_report(database, tenant_id, execution_id),
                },
                ensure_ascii=False,
                indent=1,
                default=str,
            )
        )
    return done


def test_case_compiles_runs_and_reports(site, settings, workspace, database, session, queue):
    tenant_id, project_id = workspace["tenant_id"], workspace["project_id"]
    environment_id = bind_site(session, workspace, settings, site.base_url)

    saved = CaseService(settings).create(
        tenant_id=tenant_id,
        project_id=project_id,
        name="login-smoke",
        markdown=LOGIN_MARKDOWN,
        dsl_version="1.0",
        title="登录冒烟测试",
        created_by=workspace["engineer_user_id"],
        tags=["smoke"],
    )

    supervisor = Supervisor(settings, queue=queue, announce=True).start()
    try:
        wait_for_quiet(supervisor, queue, timeout=90)
        artifact = compile_of(saved["revision_id"], tenant_id=tenant_id, database=database)
        assert artifact["status"] == "SUCCEEDED", artifact["diagnostics"]
        assert artifact["compiler_mode"] == "deterministic"
        assert len(artifact["ir"]["steps"]) == 6

        execution = ExecutionService(settings).create(
            tenant_id=tenant_id,
            project_id=project_id,
            case_id=saved["case_id"],
            environment_id=environment_id,
            requested_by=workspace["engineer_user_id"],
            run_variables={"username": "demo"},
        )
        execution_id = execution.id
        assert execution.status == ExecutionStatus.QUEUED.value
        assert [step.status for step in steps_of(database, tenant_id, execution_id)] == [StepStatus.PENDING.value] * 6

        wait_for_quiet(supervisor, queue, timeout=150)
    finally:
        supervisor.stop()

    done = conclude(settings, database, tenant_id, execution_id, outcome=Outcome.PASSED.value)
    assert done.error_code is None, done.error_detail
    assert done.lease_epoch == 1
    assert done.owner_worker_id
    assert done.browser_version, "the worker records which browser actually ran"
    assert done.active_ms > 0
    assert done.analysis_status == AnalysisStatus.NOT_REQUIRED.value
    assert done.artifact_status == "COMPLETE"

    with database.session(tenant_id) as scope:
        repo = ExecutionRepository(scope, tenant_id)
        steps = repo.steps(execution_id)
        assert [step.status for step in steps] == [StepStatus.PASSED.value] * 6
        assert steps[3].locator_strategy == "role", "the click resolved on the accessible name"
        assert steps[2].error_detail["secret"] is True
        assert steps[2].error_detail["typed_length"] == len(SECRET_VALUE)
        assert steps[5].artifact_ids, "the explicit screenshot is indexed against its step"

        artifacts = ArtifactRepository(scope, tenant_id).for_execution(execution_id)
        assert ArtifactKind.SCREENSHOT.value in {row.kind for row in artifacts}
        assert all(row.upload_status == "READY" for row in artifacts)

        events = repo.events_after(execution_id, 0, limit=500)
        assert [event.seq for event in events] == list(range(1, len(events) + 1))
        assert {"execution.status_changed", "step.started", "step.finished"} <= {event.event_type for event in events}
        assert repo.load(execution_id).last_event_seq == len(events)

        report = build_report(scope, done, settings=settings)
        assert report["report_phase"] == "complete"
        assert report["analysis_ready"] is True
        assert report["analysis"] is None, "a passing report carries no invented diagnosis"
        assert report["execution"]["outcome"] == Outcome.PASSED.value
        assert report["case"]["step_count"] == 6
        assert report["environment"]["base_url"] == site.base_url
        assert [section["status"] for section in report["steps"]] == [StepStatus.PASSED.value] * 6
        assert report["evidence"]["total"] == len(artifacts)
        assert report["evidence"]["missing"] == []

        screenshot = next(row for row in artifacts if row.kind == ArtifactKind.SCREENSHOT.value)
        journal = json.dumps([row.payload for row in events], ensure_ascii=False)
        detail = json.dumps([row.error_detail for row in steps], ensure_ascii=False)

    # the evidence file is really there, and no secret survived anywhere readable
    payload = get_object_store(settings).read_bytes(screenshot.object_key, max_bytes=4_000_000)
    assert payload[:8] == b"\x89PNG\r\n\x1a\n"
    for written in (journal, detail, json.dumps(report, ensure_ascii=False)):
        assert SECRET_VALUE not in written

    with database.session(tenant_id) as scope:
        # A pass has no failure to diagnose: the analyser never ran, and it refuses to be asked to.
        assert FailureAnalysisRepository(scope, tenant_id).for_execution(execution_id) == []

    with pytest.raises(ApiError) as refused:
        ExecutionService(settings).analyze(tenant_id=tenant_id, execution_id=execution_id)
    assert refused.value.code is ErrorCode.CONFLICT

    with database.session(tenant_id) as scope:
        # every slot the scheduler took has come back (§11.3)
        assert PoolRepository(scope, "").by_name("default").reserved_count == 0
        assert ReservationRepository(scope, tenant_id).current_for_execution(execution_id) is None
        assert scope.scalar(select(WorkerLease)) is not None, "the worker announced itself to the pool"

    # a retry is a new execution; the first one's evidence stays as it was (§12.4)
    again = ExecutionService(settings).retry(
        tenant_id=tenant_id, execution_id=execution_id, requested_by=workspace["engineer_user_id"]
    )
    assert again.id != execution_id
    assert again.retry_of_execution_id == execution_id
    assert steps_of(database, tenant_id, execution_id)[0].status == StepStatus.PASSED.value


def test_failure_is_classified_by_rules_when_the_model_is_off(site, settings, workspace, database, session, queue):
    tenant_id, project_id = workspace["tenant_id"], workspace["project_id"]
    environment_id = bind_site(session, workspace, settings, site.base_url)
    saved = CaseService(settings).create(
        tenant_id=tenant_id,
        project_id=project_id,
        name="assertion-defect",
        markdown=FAILING_MARKDOWN,
        dsl_version="1.0",
        title="断言缺陷",
        created_by=workspace["engineer_user_id"],
    )

    supervisor = Supervisor(settings, queue=queue, announce=False).start()
    try:
        wait_for_quiet(supervisor, queue, timeout=90)
        assert compile_of(saved["revision_id"], tenant_id=tenant_id, database=database)["status"] == "SUCCEEDED"
        execution = ExecutionService(settings).create(
            tenant_id=tenant_id,
            project_id=project_id,
            case_id=saved["case_id"],
            environment_id=environment_id,
            requested_by=workspace["engineer_user_id"],
        )
        execution_id = execution.id
        wait_for_quiet(supervisor, queue, timeout=150)
    finally:
        supervisor.stop()

    done = conclude(settings, database, tenant_id, execution_id, outcome=Outcome.FAILED.value)
    assert done.error_code == "ASSERTION_FAILED"
    # the analysis pass ran on its own queue, asynchronously, and still landed (§12.3)
    assert done.analysis_status == AnalysisStatus.SUCCEEDED.value

    with database.session(tenant_id) as scope:
        steps = ExecutionRepository(scope, tenant_id).steps(execution_id)
        assert [step.status for step in steps] == [StepStatus.PASSED.value, StepStatus.FAILED.value]
        # the §9.4 intent/ack ladder guards the side-effecting actions, not a re-navigation
        assert steps[0].dispatch_state == DispatchState.NOT_STARTED.value
        assert steps[1].error_code == "ASSERTION_FAILED"
        # only the failed step's evidence is missing from the plan: the run still captured its own proof
        assert steps[1].artifact_ids, "a failure captures evidence without changing the verdict (§12.1)"

        artifacts = ArtifactRepository(scope, tenant_id).for_execution(execution_id)
        assert {ArtifactKind.SCREENSHOT.value, ArtifactKind.DOM.value} <= {row.kind for row in artifacts}

        rows = FailureAnalysisRepository(scope, tenant_id).for_execution(execution_id)
        assert len(rows) == 1
        assert rows[0].source == "rules"
        assert rows[0].failure_type == "ASSERTION_FAILURE"
        assert rows[0].is_hypothesis is False
        assert rows[0].confidence > 0.5
        cited = list(rows[0].evidence_refs or [])
        assert all(ref in {f"artifact:{row.id}" for row in artifacts} for ref in cited)

        report = build_report(scope, done, settings=settings)
        assert report["analysis"]["failure_type"] == "ASSERTION_FAILURE"
        assert report["analysis"]["status"] == AnalysisStatus.SUCCEEDED.value
        assert report["report_phase"] == "complete"


def test_cancel_before_the_worker_claims_it_finishes_immediately(site, settings, workspace, database, session, queue):
    """A queued run nobody holds is closed by the cancel path, not left for the archive deadline."""
    tenant_id, project_id = workspace["tenant_id"], workspace["project_id"]
    environment_id = bind_site(session, workspace, settings, site.base_url)
    saved = CaseService(settings).create(
        tenant_id=tenant_id,
        project_id=project_id,
        name="cancelled-run",
        markdown=FAILING_MARKDOWN,
        dsl_version="1.0",
        title="取消",
        created_by=workspace["engineer_user_id"],
    )

    supervisor = Supervisor(settings, queue=queue, announce=False).start()
    try:
        wait_for_quiet(supervisor, queue, timeout=90)
        execution = ExecutionService(settings).create(
            tenant_id=tenant_id,
            project_id=project_id,
            case_id=saved["case_id"],
            environment_id=environment_id,
            requested_by=workspace["engineer_user_id"],
        )
        cancelled = ExecutionService(settings).cancel(
            tenant_id=tenant_id,
            execution_id=execution.id,
            requested_by=workspace["admin_user_id"],
            reason="smoke aborted",
        )
    finally:
        supervisor.stop()

    assert (cancelled.status, cancelled.outcome) == (ExecutionStatus.FINISHED.value, Outcome.CANCELLED.value)
    assert cancelled.cancel_requested_at is not None
    assert cancelled.error_code == "CANCELLED"
    assert [step.status for step in steps_of(database, tenant_id, execution.id)] == [StepStatus.SKIPPED.value] * 2
    with database.session(tenant_id) as scope:
        assert PoolRepository(scope, "").by_name("default").reserved_count == 0
        assert ReservationRepository(scope, tenant_id).current_for_execution(execution.id) is None
