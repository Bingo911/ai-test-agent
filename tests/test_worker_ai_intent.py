"""§6.4: the AI decision a queued job makes when it runs is not the one its caller made.

Three workers can put platform data in front of a model - the compile worker (case text), the execution
worker's vision resolver (screenshots) and the analysis worker (failure evidence). Each has to combine the
intent the request carried with the project's policy *as it stands at the moment of the call*, and record
the downgrade where a reader will find it. None of that shows up in a receipt, so it is tested here against
the rows the workers leave behind.

The console half is as much part of the rule as the MCP half (§12, AC-40): `mcp_policy` governs MCP, so a
job that came in through REST keeps the AI behaviour it has always had even where a project has switched
every MCP flag off.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import Mock

import pytest
from backend.app.config import Settings
from backend.app.db.models import CompileArtifact, ExecutionEvent, Outbox, Project, TestExecution
from backend.app.domain.enums import AnalysisStatus, CompileStatus, Sensitivity
from backend.app.services import cases as case_service
from backend.app.services.ai_intent import AI_STOPPED_EVENT, MCP_ORIGIN, STOPPED_BY_POLICY, server_ai_allowed
from backend.app.workers import execution as execution_module
from backend.app.workers.analysis import AnalysisWorker
from backend.app.workers.compile import CompileWorker
from backend.app.workers.execution import ExecutionWorker
from sqlalchemy import select

from .mcp_live import OPEN, live_settings, moment, new_case, new_project, seed_workspace, seeded_run

AI_ON = dict(OPEN, allow_server_ai=True)
AI_OFF = dict(OPEN, allow_server_ai=False)

#: A case the deterministic compiler can finish on its own: every step is an explicit instruction, so the
#: only thing that moves between the rows below is whether the platform was willing to reach for a model.
DETERMINISTIC_BODY = """---
dsl_version: "1.0"
---
# 结算回归

## Step 1
```yaml
action: open
url: "${env.base_url}/index.html"
```

## Step 2
```yaml
action: click
target:
  css: "#submit"
  description: 提交按钮
```
"""

#: Project names are unique per tenant, and every test here makes its own projects.
_sequence = iter(range(1, 10_000))


@pytest.fixture
def mcp_settings(database: Any, tmp_path: Any) -> Settings:
    return live_settings(database, tmp_path)


@pytest.fixture
def workspace(database: Any, mcp_settings: Settings) -> dict[str, str]:
    return seed_workspace(database, mcp_settings)


def run_ai(*, asked: bool, origin: str = MCP_ORIGIN) -> dict[str, Any]:
    """The document the run command freezes, in the shape it actually writes (§6.4)."""
    return {"origin": origin, "use_server_ai": asked, "policy": {"allow_server_ai": True}}


def _project(database: Any, workspace: dict[str, str], policy: dict[str, bool]) -> str:
    return new_project(database, workspace, f"ai-intent-{next(_sequence)}", policy=policy, at=moment(1))


def _read(database: Any, model: Any, row_id: str) -> Any:
    with database.session() as session:
        return session.get(model, row_id)


def _record_run_ai(database: Any, execution_id: str, snapshot_ai: dict[str, Any] | None) -> None:
    """Freeze an AI intent on a run the way the create-execution transaction does."""
    with database.session() as session:
        row = session.get(TestExecution, execution_id)
        snapshot = dict(row.snapshot or {})
        snapshot.pop("run_ai", None)
        if snapshot_ai is not None:
            snapshot["run_ai"] = dict(snapshot_ai)
        row.snapshot = snapshot
        session.commit()


def stop_events(database: Any, execution_id: str) -> list[dict[str, Any]]:
    """What the run itself recorded about a stopped model call, through the journal the SSE stream reads."""
    with database.session() as session:
        rows = session.scalars(
            select(ExecutionEvent).where(
                ExecutionEvent.execution_id == execution_id, ExecutionEvent.event_type == AI_STOPPED_EVENT
            )
        )
        return [dict(row.payload) for row in rows]


# ------------------------------------------------------------------------ compile worker


def _queued_compile(
    database: Any, workspace: dict[str, str], project_id: str, *, origin: str
) -> dict[str, Any]:
    """A real queue entry, made by the service both MCP and REST go through.

    Handing the worker an invented payload would only test its reading of a document nobody writes: this
    asserts the entrypoint is carried at all, which is the first half of §6.4's second paragraph.
    """
    ids = new_case(database, workspace, project_id, "AI 意图回归", at=moment(2), markdown=DETERMINISTIC_BODY)
    with database.session() as session:
        case_service.request_compile(
            session,
            workspace["tenant_id"],
            project_id=project_id,
            revision_id=ids["revision_id"],
            created_by=workspace["engineer_user_id"],
            use_ai=True,
            origin=origin,
        )
        session.commit()
    with database.session() as session:
        row = session.scalar(select(Outbox).where(Outbox.aggregate_id == ids["revision_id"]))
        assert row is not None, "the compile was never queued"
        return dict(row.payload)


@pytest.mark.parametrize(
    ("origin", "policy", "expect_adapter"),
    [(MCP_ORIGIN, AI_OFF, False), (MCP_ORIGIN, AI_ON, True), ("rest", AI_OFF, True)],
)
def test_the_compile_worker_asks_the_policy_that_is_true_when_it_runs(
    database: Any,
    workspace: dict[str, str],
    mcp_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    origin: str,
    policy: dict[str, bool],
    expect_adapter: bool,
) -> None:
    """§6.4, AC-40: an MCP job is re-read, a console job is not, and the queue entry says which it is."""
    built: list[str] = []

    def spy(settings: Settings, *, purpose: str | None = None) -> Mock:
        built.append(str(purpose))
        # `enabled=False` is how the pipeline already reads "no provider configured", so the only thing
        # this observes is whether the worker was willing to hand the case text over at all.
        return Mock(enabled=False, usage=Mock(as_dict=Mock(return_value={}), model=None))

    monkeypatch.setattr("backend.app.workers.compile.AiAdapter", spy)
    project_id = _project(database, workspace, policy)
    payload = _queued_compile(database, workspace, project_id, origin=origin)
    assert payload["origin"] == origin
    assert payload["use_ai"] is True, "the queue entry lost the caller's intent"

    result = CompileWorker(settings=mcp_settings).run(payload)

    assert result["status"] == CompileStatus.SUCCEEDED.value, result
    assert (built == ["compiler"]) is expect_adapter
    artifact = _read(database, CompileArtifact, result["artifact_id"])
    # Deterministic in all three rows: this case body never needed the model, which is the point - the only
    # difference between the rows is whether the platform was willing to reach for one.
    assert artifact.compiler_mode == "deterministic"
    stopped = [item for item in artifact.diagnostics or [] if item.get("code") == STOPPED_BY_POLICY]
    assert (artifact.usage or {}).get("ai_stopped") == (STOPPED_BY_POLICY if stopped else None)
    if expect_adapter:
        assert stopped == []
        return
    # The reason has to be readable without a log, and must not make the compile look broken: it succeeded,
    # just without the model. An ERROR here would become the artifact's error code for a reader (§13.2).
    assert len(stopped) == 1
    assert stopped[0]["severity"] == "WARNING"
    assert artifact.error_code is None
    assert artifact.status == CompileStatus.SUCCEEDED.value


# ---------------------------------------------------------------------- execution worker


def _vision_for(
    database: Any,
    workspace: dict[str, str],
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    *,
    policy: dict[str, bool],
    snapshot_ai: dict[str, Any] | None,
) -> tuple[Any, list[str], str]:
    project_id = _project(database, workspace, policy)
    execution_id = seeded_run(database, workspace, project_id)
    _record_run_ai(database, execution_id, snapshot_ai)
    execution = _read(database, TestExecution, execution_id)

    purposes: list[str] = []

    def resolver(_settings: Settings, adapter: Any = None) -> str:
        purposes.append(str(getattr(adapter, "purpose", "")))
        return "resolver"

    monkeypatch.setattr(execution_module, "AiVisionResolver", resolver)
    worker = ExecutionWorker(settings=settings.model_copy(update={"ai_vision_enabled": True}), executor=Mock())
    plan = Mock(session_config=Mock(allow_vision=True))
    found = worker._vision(plan, execution, dict(execution.snapshot or {}), workspace["tenant_id"])
    return found, purposes, execution_id


def test_a_run_that_never_asked_for_the_model_does_not_borrow_it(
    database: Any, workspace: dict[str, str], mcp_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§6.4: an MCP run defaults to no server AI even where the project would have allowed it."""
    found, purposes, execution_id = _vision_for(
        database, workspace, mcp_settings, monkeypatch, policy=AI_ON, snapshot_ai=run_ai(asked=False)
    )
    assert found is None
    assert purposes == []
    # Nothing was withdrawn, so nothing is recorded as withdrawn: the reason says the policy moved.
    assert stop_events(database, execution_id) == []


def test_a_policy_tightened_while_the_run_waited_stops_the_vision_call(
    database: Any, workspace: dict[str, str], mcp_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§6.4: the intersection is taken when the worker runs, and the downgrade is recorded on the run."""
    found, purposes, execution_id = _vision_for(
        database, workspace, mcp_settings, monkeypatch, policy=AI_OFF, snapshot_ai=run_ai(asked=True)
    )
    assert found is None
    assert purposes == []
    assert stop_events(database, execution_id) == [{"purpose": "vision", "reason": STOPPED_BY_POLICY}]


def test_a_run_the_project_still_agrees_to_gets_its_vision_resolver(
    database: Any, workspace: dict[str, str], mcp_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    found, purposes, execution_id = _vision_for(
        database, workspace, mcp_settings, monkeypatch, policy=AI_ON, snapshot_ai=run_ai(asked=True)
    )
    assert found == "resolver"
    assert purposes == ["vision"]
    assert stop_events(database, execution_id) == []


def test_a_console_run_is_not_governed_by_the_mcp_policy(
    database: Any, workspace: dict[str, str], mcp_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-40: the flag that decides MCP traffic has no say over a run the web console started."""
    found, purposes, execution_id = _vision_for(
        database, workspace, mcp_settings, monkeypatch, policy=AI_OFF, snapshot_ai=None
    )
    assert found == "resolver"
    assert purposes == ["vision"]
    assert stop_events(database, execution_id) == []


def test_a_rest_run_that_named_its_intent_is_still_not_governed_by_the_mcp_policy(
    database: Any, workspace: dict[str, str], mcp_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§6.4: `origin` is what the intersection reads, and only one value of it is re-checked.

    The run command writes an intent only for MCP today, so this is the shape a REST `use_server_ai` field
    would take - and the worker gate has to already answer it the way the design says, or the day that
    field lands the console's own AI silently moves behind an MCP flag.
    """
    found, purposes, execution_id = _vision_for(
        database,
        workspace,
        mcp_settings,
        monkeypatch,
        policy=AI_OFF,
        snapshot_ai=run_ai(asked=True, origin="rest"),
    )
    assert found == "resolver"
    assert purposes == ["vision"]
    assert stop_events(database, execution_id) == []


# -------------------------------------------------------------------------- analysis worker


def _analysis_for(
    database: Any,
    workspace: dict[str, str],
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    *,
    policy: dict[str, bool],
    snapshot_ai: dict[str, Any] | None,
    evidence_mode: str = "NORMAL",
) -> tuple[dict[str, Any], list[dict[str, Any]], Mock, str]:
    """One analysis pass over a run that carries `snapshot_ai`, with everything but the decision mocked.

    The facts document is the worker's own reading of the run, so the test hands over the shape `_facts`
    produces and keeps `_model_intent` real: the question under test is which of the two halves moves the
    answer, and that answer has to come from the project row.
    """
    project_id = _project(database, workspace, policy)
    execution_id = seeded_run(database, workspace, project_id)
    worker = AnalysisWorker(settings=settings.model_copy(update={"ai_enabled": True}))
    facts = {
        "tenant_id": workspace["tenant_id"],
        "project_id": project_id,
        "execution_id": execution_id,
        "outcome": "FAILED",
        "error_code": "ASSERTION_FAILED",
        "message": "expected Welcome",
        "steps": [{"step_id": "s1", "step_no": 1, "action": "expect_text", "status": "FAILED"}],
        "failing_step": {"step_id": "s1", "action": "expect_text", "status": "FAILED", "error_detail": {}},
        "evidence_mode": evidence_mode,
        "artifact_refs": [],
        "run_ai": snapshot_ai,
    }
    written: list[dict[str, Any]] = []
    monkeypatch.setattr(worker, "_facts", lambda *_: facts)
    monkeypatch.setattr(worker, "_start", lambda *_: ("analysis-row", project_id))
    monkeypatch.setattr(worker, "_write", lambda _tenant, _row, **fields: written.append(fields))
    monkeypatch.setattr(worker, "_set_status", Mock())
    explained = Mock(return_value={"failure_type": "ASSERTION_FAILURE", "reason": "from the model"})
    monkeypatch.setattr(worker, "_explain", explained)
    result = worker.run({"tenant_id": workspace["tenant_id"], "execution_id": execution_id})
    return result, written, explained, execution_id


def test_analysis_stays_on_rules_when_the_policy_tightened_after_the_run_was_queued(
    database: Any, workspace: dict[str, str], mcp_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§6.4: evidence from a run the project no longer agrees to send stays inside the platform."""
    result, written, explained, execution_id = _analysis_for(
        database, workspace, mcp_settings, monkeypatch, policy=AI_OFF, snapshot_ai=run_ai(asked=True)
    )
    explained.assert_not_called()
    assert result["source"] == "rules"
    # The rule answer is still the durable one, and it carries why the model half is missing.
    assert written[0]["status"] == AnalysisStatus.SUCCEEDED.value
    assert written[0]["source"] == "rules"
    assert written[0]["usage"] == {"ai_stopped": STOPPED_BY_POLICY}
    assert stop_events(database, execution_id) == [{"purpose": "analysis", "reason": STOPPED_BY_POLICY}]


def test_a_run_that_never_asked_for_analysis_gets_no_model_call(
    database: Any, workspace: dict[str, str], mcp_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§6.4: the promise an MCP run makes is about failure analysis as well as vision."""
    result, written, explained, execution_id = _analysis_for(
        database, workspace, mcp_settings, monkeypatch, policy=AI_ON, snapshot_ai=run_ai(asked=False)
    )
    explained.assert_not_called()
    assert result["source"] == "rules"
    assert written[0]["usage"] is None
    assert stop_events(database, execution_id) == []


def test_analysis_explains_a_run_the_project_agreed_to(
    database: Any, workspace: dict[str, str], mcp_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, written, explained, execution_id = _analysis_for(
        database, workspace, mcp_settings, monkeypatch, policy=AI_ON, snapshot_ai=run_ai(asked=True)
    )
    explained.assert_called_once()
    assert result["source"] == "ai"
    assert written[0]["usage"] is None
    assert stop_events(database, execution_id) == []


def test_analysis_of_a_console_run_is_not_governed_by_the_mcp_policy(
    database: Any, workspace: dict[str, str], mcp_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC-40: a console run names no intent, so no MCP flag can take its model away."""
    result, written, explained, execution_id = _analysis_for(
        database, workspace, mcp_settings, monkeypatch, policy=AI_OFF, snapshot_ai=None
    )
    explained.assert_called_once()
    assert result["source"] == "ai"
    assert written[0]["usage"] is None
    assert stop_events(database, execution_id) == []


def test_analysis_of_a_rest_run_that_named_its_intent_is_not_governed_by_the_mcp_policy(
    database: Any, workspace: dict[str, str], mcp_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§6.4: the same origin split on the analysis side, where the evidence is even more sensitive."""
    result, written, explained, execution_id = _analysis_for(
        database,
        workspace,
        mcp_settings,
        monkeypatch,
        policy=AI_OFF,
        snapshot_ai=run_ai(asked=True, origin="rest"),
    )
    explained.assert_called_once()
    assert result["source"] == "ai"
    assert written[0]["usage"] is None
    assert stop_events(database, execution_id) == []


def test_a_sensitive_run_records_no_policy_reason_it_could_not_have_reached(
    database: Any, workspace: dict[str, str], mcp_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§6.4: the recorded reason is about a downgrade that actually happened, not a condition that coexisted.

    A sensitive run never reaches the model whatever the policy says, so writing "the project withdrew its
    agreement" there would tell a reader the flag is what stopped this analysis.
    """
    result, written, explained, execution_id = _analysis_for(
        database,
        workspace,
        mcp_settings,
        monkeypatch,
        policy=AI_OFF,
        snapshot_ai=run_ai(asked=True),
        evidence_mode=Sensitivity.SENSITIVE.value,
    )
    explained.assert_not_called()
    assert result["source"] == "rules"
    assert written[0]["usage"] is None
    assert stop_events(database, execution_id) == []


def test_a_project_that_says_nothing_about_ai_is_not_an_agreement(database: Any, workspace: dict[str, str]) -> None:
    """The workers read the same fail-closed policy document the rest of the platform reads (§5.5).

    A project row with no policy in it is not consent, and neither is one nobody could parse: an unreadable
    settings document has to fall to the same answer as an explicit `allow_server_ai: false`.
    """
    agreed = _project(database, workspace, AI_ON)
    silent = _project(database, workspace, AI_ON)
    with database.session() as session:
        session.get(Project, silent).settings = {"something_else": "value"}
        session.commit()

    with database.session() as session:
        assert server_ai_allowed(session, workspace["tenant_id"], agreed) is True
        assert server_ai_allowed(session, workspace["tenant_id"], silent) is False
        assert server_ai_allowed(session, workspace["tenant_id"], "no-such-project") is False


def test_the_facts_document_carries_the_intent_the_run_froze(
    database: Any, workspace: dict[str, str], mcp_settings: Settings
) -> None:
    """`_facts` is the only way the worker learns the intent, so a snapshot read is part of the promise."""
    project_id = _project(database, workspace, AI_ON)
    execution_id = seeded_run(database, workspace, project_id)
    _record_run_ai(database, execution_id, run_ai(asked=True))
    worker = AnalysisWorker(settings=mcp_settings)
    facts = worker._facts(workspace["tenant_id"], execution_id)
    assert facts["run_ai"] == run_ai(asked=True)
    _record_run_ai(database, execution_id, None)
    assert worker._facts(workspace["tenant_id"], execution_id)["run_ai"] is None
