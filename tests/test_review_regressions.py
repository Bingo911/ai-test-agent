"""Regressions for the PRD/design review: real budgets, repair attempts and evidence boundaries."""

from __future__ import annotations

import asyncio
import io
import json
import zipfile
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from backend.app.ai.adapter import AiAdapter
from backend.app.analysis.evidence import trace_summary
from backend.app.compiler.pipeline import compile_revision
from backend.app.db.encrypted_types import EncryptedJSON, EncryptedText
from backend.app.domain.errors import ApiError
from backend.app.evidence.collector import EvidenceCollector
from backend.app.evidence.sink import DatabaseEvidenceSink
from backend.app.executors.contracts import RunContext
from backend.app.executors.playwright.adapter import PlaywrightExecutor
from backend.app.human.gate import HumanGate, HumanGateError, PauseDecision
from backend.app.ir.models import LiteralValue, OpenStep, Source
from backend.app.services.object_store import EncryptedObjectStore, LocalObjectStore, S3ObjectStore, set_object_store
from backend.app.services.secret_store import SecretStore
from backend.app.services.storage_crypto import ENVELOPE_PREFIX, StorageCipher
from backend.app.workers.analysis import AnalysisWorker
from backend.app.workers.execution import ExecutionWorker
from cryptography.fernet import InvalidToken
from scripts.encrypt_storage import migrate_database, migrate_local_objects, migrate_s3_objects
from sqlalchemy import Column, MetaData, String, Table, select, text


def context() -> RunContext:
    return RunContext("run", "tenant", "project", "environment", "revision", 1)


@pytest.mark.parametrize("satisfied", [True, False])
def test_challenge_resume_checks_the_condition_without_unpacking_errors(settings, database, monkeypatch, satisfied):
    gate = HumanGate("run", "tenant", "project", "worker", None, context(), None, settings)
    state = SimpleNamespace(human_task_id="human", controller_id=None)
    decision = PauseDecision("s1", "on_challenge", "OTP", "after_action", {"kind": "page_contains"})
    monkeypatch.setattr(gate, "_task_state", lambda _: ("RESUME_REQUESTED", None, "operator"))
    monkeypatch.setattr(gate, "_resume_payload", lambda _: {"step_completed": False})
    monkeypatch.setattr(gate, "_evaluate_condition", AsyncMock(return_value=(satisfied, "condition result")))
    complete, reject = Mock(), Mock()
    monkeypatch.setattr(gate, "_complete", complete)
    monkeypatch.setattr(gate, "_return_to_claimed", reject)
    resolution = asyncio.run(gate._check_resume(state, decision))
    assert resolution == ("condition_verified" if satisfied else None)
    assert complete.called is satisfied
    assert reject.called is not satisfied


@pytest.mark.parametrize("hard_limit", [None, 12_000])
def test_action_budget_survives_a_long_human_pause_but_never_exceeds_the_hard_limit(settings, monkeypatch, hard_limit):
    from backend.app.executors.playwright import actions, adapter

    clock = [1000.0]
    monkeypatch.setattr(adapter, "_now_ms", lambda: clock[0])
    monkeypatch.setattr(actions, "_now_ms", lambda: clock[0])
    ctx = context()
    ctx.hard_deadline_ms = hard_limit

    async def pause(_):
        clock[0] += 15_000
        ctx.human_waited_ms += 15_000

    ctx.human_hook = pause
    executor = PlaywrightExecutor(settings)
    ac = actions.ActionContext(None, SimpleNamespace(vision_allowed=False), None, ctx, None, ())
    monkeypatch.setattr(executor, "action_context", lambda *_: ac)
    called = []

    async def action(_step, _ac, *, deadline_monotonic_ms, budget):
        called.append(deadline_monotonic_ms)
        assert deadline_monotonic_ms - clock[0] == 10_000
        clock[0] += 1000
        return {}

    monkeypatch.setitem(actions.HANDLERS, "open", action)
    step = OpenStep(
        id="s1",
        action="open",
        source=Source(start_line=1, end_line=1, text="open"),
        url=LiteralValue(kind="literal", value="https://example.com"),
    )
    result = asyncio.run(executor.execute_step(None, step, ctx, deadline_ms=11_000))
    assert result.status == ("PASSED" if hard_limit is None else "ERROR")
    assert result.duration_ms == (1000 if hard_limit is None else 0)
    assert len(called) == (1 if hard_limit is None else 0)


def test_hard_limit_is_checked_during_human_wait(settings, database, monkeypatch):
    ctx = context()
    ctx.hard_deadline_ms = 0
    gate = HumanGate("run", "tenant", "project", "worker", None, ctx, None, settings)
    closed = Mock()
    monkeypatch.setattr(gate, "_close_task", closed)
    state = SimpleNamespace()
    with pytest.raises(HumanGateError) as error:
        asyncio.run(gate._wait_for_resume(state, PauseDecision("s1", "before", "confirm", "BEFORE_ACTION")))
    assert error.value.error_code == "ACTIVE_TIMEOUT"
    closed.assert_called_once()


@pytest.mark.parametrize("error_code", ["ACTIVE_TIMEOUT", "HUMAN_WAIT_TIMEOUT", "HUMAN_BUDGET_EXCEEDED"])
def test_executor_preserves_human_gate_termination_for_the_worker(settings, error_code):
    ctx = context()
    failure = HumanGateError(error_code, "human wait ended", outcome="TIMED_OUT")
    ctx.human_hook = Mock(side_effect=failure)
    step = OpenStep(
        id="s1",
        action="open",
        source=Source(start_line=1, end_line=1, text="open"),
        url=LiteralValue(kind="literal", value="https://example.com"),
    )
    with pytest.raises(HumanGateError) as error:
        asyncio.run(PlaywrightExecutor(settings).execute_step(None, step, ctx, deadline_ms=10_000))
    assert error.value is failure


def test_worker_reports_exhausted_step_budgets_as_timeouts(settings, database):
    from backend.app.domain.enums import StepStatus

    result = SimpleNamespace(status="ERROR", failure_kind="timeout", step_id="s1", message="step budget exhausted")
    conclusion = ExecutionWorker(settings=settings)._conclude(result, [StepStatus.ERROR])
    assert conclusion.outcome == "TIMED_OUT"
    assert conclusion.error_code == "ACTIVE_TIMEOUT"


@pytest.mark.parametrize("pause_phase", ["before", "after"])
def test_worker_active_budget_excludes_all_human_pauses(settings, database, monkeypatch, pause_phase):
    import backend.app.workers.execution as module

    clock = [0.0]
    monkeypatch.setattr(module, "monotonic_ms", lambda: clock[0])
    ctx = context()
    markdown = (
        "# Case\n## Step 1\n```yaml\naction: open\nurl: https://example.com\n```\n"
        "## Step 2\n```yaml\naction: open\nurl: https://example.com\n```"
    )
    compiled = compile_revision(markdown, revision_id="revision", settings=settings)
    assert compiled.status == "SUCCEEDED"
    calls = []

    async def execute(_session, step, _ctx, *, deadline_ms):
        calls.append(step.id)
        clock[0] += 1000
        if len(calls) == 1 and pause_phase == "before":
            clock[0] += 20_000
            ctx.human_waited_ms += 20_000
        return SimpleNamespace(ok=True, status="PASSED")

    async def after_step(step, result):
        if step.id == "s1" and pause_phase == "after":
            clock[0] += 20_000
            ctx.human_waited_ms += 20_000

    worker = ExecutionWorker(
        settings=settings.model_copy(update={"active_timeout_seconds": 3}),
        executor=SimpleNamespace(execute_step=execute),
    )
    monkeypatch.setattr(worker, "_gate", lambda *_: SimpleNamespace(hook=None, after_step=after_step))
    monkeypatch.setattr(worker, "_step_rows", lambda _: [SimpleNamespace(step_id="s1"), SimpleNamespace(step_id="s2")])
    monkeypatch.setattr(worker, "_start_step", Mock())
    monkeypatch.setattr(worker, "_persist_step", AsyncMock())
    execution = SimpleNamespace(ir=compiled.ir)
    result = asyncio.run(
        worker._run_steps(execution, None, ctx, SimpleNamespace(navigation_timeout_ms=1000), hard_deadline=100_000)
    )
    assert result.outcome == "PASSED"
    assert calls == ["s1", "s2"]


def ai(settings, monkeypatch, replies):
    adapter = AiAdapter(
        settings.model_copy(update={"ai_enabled": True, "ai_base_url": "https://example.com"}), purpose="compiler"
    )
    responses = iter(replies)
    monkeypatch.setattr(
        adapter, "_post", lambda _: {"choices": [{"message": {"content": json.dumps(next(responses))}}]}
    )
    return adapter


def test_three_prose_steps_compile_with_the_default_compiler_budget(settings, monkeypatch):
    adapter = ai(settings, monkeypatch, [{"action": "open", "url": "https://example.com"}] * 3)
    markdown = "# Case\n\n" + "\n\n".join(f"## Step {i}\n打开页面 https://example.com" for i in range(1, 4))
    result = compile_revision(markdown, revision_id="revision", settings=settings, ai_adapter=adapter)
    assert result.status == "NEEDS_REVIEW"
    assert len(result.ir["steps"]) == 3
    assert adapter.usage.calls == 3
    assert AiAdapter(settings, purpose="vision").max_calls == settings.ai_max_calls_per_run


def test_an_assertion_may_expect_a_variable_the_case_declares(settings):
    """§5.3 compares against the *resolved* expected, so `${vars.*}` is a legal assertion input.

    The run either supplies the variable or fails with `VARIABLE_MISSING`; a declared variable can never
    quietly assert against an empty string, which is the only thing the empty-literal rule protects.
    """
    markdown = (
        "---\nvariables:\n  keyword:\n    type: string\n    required: true\n---\n"
        "# Case\n## Step 1\n```yaml\naction: assert\ncondition:\n"
        '  kind: page_contains\n  expected: "${vars.keyword}"\n```'
    )
    compiled = compile_revision(markdown, revision_id="revision", settings=settings)
    assert compiled.status == "SUCCEEDED", compiled.diagnostics
    assert compiled.ir["steps"][0]["condition"]["expected"] == {
        "kind": "variable",
        "namespace": "vars",
        "key": "keyword",
    }


def test_an_assertion_still_needs_something_to_compare_against(settings):
    """The guard the fix keeps: an empty expected is an assertion that can never hold."""
    for expected in ('""', "null"):
        markdown = (
            "# Case\n## Step 1\n```yaml\naction: assert\ncondition:\n"
            f"  kind: page_contains\n  expected: {expected}\n```"
        )
        compiled = compile_revision(markdown, revision_id="revision", settings=settings)
        assert compiled.status == "FAILED", (expected, compiled.diagnostics)
        assert {item["code"] for item in compiled.diagnostics} == {"CONDITION_EXPECTED_REQUIRED"}, compiled.diagnostics


def test_a_valid_ai_repair_does_not_keep_the_previous_normalization_error(settings, monkeypatch):
    adapter = ai(
        settings,
        monkeypatch,
        [{"action": "wait", "duration_ms": "invalid"}, {"action": "open", "url": "https://example.com"}],
    )
    result = compile_revision(
        "# Case\n## Step 1\n打开页面 https://example.com", revision_id="revision", settings=settings, ai_adapter=adapter
    )
    assert result.status == "NEEDS_REVIEW", result.diagnostics
    assert adapter.usage.calls == 2
    assert not any(item["severity"] == "ERROR" for item in result.diagnostics)


def test_a_model_that_echoes_the_step_id_it_was_shown_still_compiles(settings, monkeypatch):
    """The contract the model is shown names `id`, so echoing it is the model following instructions.

    It failed once, as `Unsupported field 'id' for assert`: the normalizer takes the step id from the heading
    and treats any other key as DSL noise. A reply that is otherwise perfect then needed a repair round whose
    instruction was "drop the field the prompt told you to include", and the compile ended FAILED.
    """
    adapter = ai(
        settings,
        monkeypatch,
        [{"id": "s1", "action": "assert", "condition": {"kind": "page_contains", "expected": "Welcome"}}],
    )
    result = compile_revision(
        "# Case\n## Step 1\n确认页面上出现了 Welcome", revision_id="revision", settings=settings, ai_adapter=adapter
    )
    assert result.status == "NEEDS_REVIEW", result.diagnostics
    assert result.ir["steps"][0]["id"] == "s1"
    assert adapter.usage.calls == 1, "accepted as it stood, with no repair round spent"


def test_a_model_answer_naming_a_different_step_is_still_refused(settings, monkeypatch):
    """Consuming the echo is not the same as trusting it: a reply about another step is a wrong reply."""
    adapter = ai(
        settings,
        monkeypatch,
        [
            {"id": "s2", "action": "assert", "condition": {"kind": "page_contains", "expected": "Welcome"}},
            {"id": "s1", "action": "assert", "condition": {"kind": "page_contains", "expected": "Welcome"}},
        ],
    )
    result = compile_revision(
        "# Case\n## Step 1\n确认页面上出现了 Welcome", revision_id="revision", settings=settings, ai_adapter=adapter
    )
    assert result.status == "NEEDS_REVIEW", result.diagnostics
    assert adapter.usage.calls == 2
    assert result.ir["steps"][0]["id"] == "s1"


def test_a_prose_step_the_model_cannot_compile_fails_the_case_instead_of_shortening_it(settings, monkeypatch):
    """A dropped prose step used to compile SUCCEEDED with one step fewer - and that IR was runnable.

    Both attempts failing returned only the INFO notes from each try, so the pipeline saw no error, skipped
    the step, and shipped a shorter case marked `deterministic`. The live model produced exactly this shape
    when it wrote `{"action": "assert", "assert": {...}}`, which is why the guard has to be an error and not
    a guess.
    """
    rejected = {"action": "assert", "assert": {"kind": "page_contains", "expected": "Welcome"}}
    adapter = ai(settings, monkeypatch, [rejected, rejected])
    result = compile_revision(
        "# Case\n## Step 1\n```yaml\naction: open\nurl: https://example.com\n```\n"
        "## Step 2\n确认页面上出现了 Welcome",
        revision_id="revision",
        settings=settings,
        ai_adapter=adapter,
    )
    assert result.status == "FAILED", result.diagnostics
    errors = [item for item in result.diagnostics if item["severity"] == "ERROR"]
    assert [item["code"] for item in errors] == ["AI_OUTPUT_INVALID"], errors
    assert errors[0]["step_id"] == "s2"
    assert "after one repair attempt" in errors[0]["message"]
    assert adapter.usage.calls == 2
    # Two calls went out, so two calls must be on the record: filing this failure under `deterministic`
    # would let a compile that reached a model be read as one that never needed to.
    assert result.compiler_mode == "ai_assisted"


def test_a_prose_step_whose_model_call_never_returns_is_still_an_assisted_compile(settings, monkeypatch):
    """The mode follows the hand-off, not the response.

    Counting successful calls made a provider that was reached and then failed read as `deterministic`, which
    is the one case where the difference matters: the step's text did leave the platform, and a compile filed
    as local says it did not.
    """
    from backend.app.ai.adapter import AiUnavailable

    def died(_messages):
        raise AiUnavailable("the provider did not answer")

    adapter = ai(settings, monkeypatch, [])
    monkeypatch.setattr(adapter, "chat_json", died)
    result = compile_revision(
        "# Case\n## Step 1\n确认页面上出现了 Welcome", revision_id="revision", settings=settings, ai_adapter=adapter
    )
    assert result.status == "FAILED", result.diagnostics
    assert [item["code"] for item in result.diagnostics if item["severity"] == "ERROR"] == ["AI_UNAVAILABLE"]
    assert result.compiler_mode == "ai_assisted"
    assert adapter.usage.calls == 0


def test_analysis_reads_a_dom_prefix_and_the_tail_of_large_json_rings(settings, database, tmp_path):
    store = LocalObjectStore(tmp_path)
    store.put_bytes("dom.html", b"<body>" + b"x" * 5000, "text/html")
    entries = [{"message": "x" * 6000, "index": i} for i in range(20)]
    store.put_bytes("console.json", json.dumps(entries).encode(), "application/json")
    worker = AnalysisWorker(settings=settings)
    assert len(worker._read_text(store, "dom.html", 4000)) == 4000
    assert [entry["index"] for entry in worker._read_ring(store, "console.json")] == list(range(8, 20))
    with pytest.raises(ApiError):
        store.read_bytes("dom.html", max_bytes=4000)


def test_trace_projection_never_contains_params_errors_or_page_content():
    payload = io.BytesIO()
    events = [
        {
            "type": "before",
            "callId": "1",
            "apiName": "page.fill",
            "startTime": 2,
            "params": {"value": "PRIVATE_PASSWORD"},
        },
        {"type": "after", "callId": "1", "endTime": 12, "error": {"message": "PRIVATE_PASSWORD"}},
        {"type": "frame-snapshot", "html": "PRIVATE_PASSWORD"},
    ]
    with zipfile.ZipFile(payload, "w") as archive:
        archive.writestr("0.trace", "\n".join(json.dumps(event) for event in events))
    result = trace_summary(payload.getvalue())
    assert result == [{"action": "page.fill", "failed": True, "duration_ms": 10}]
    assert "PRIVATE_PASSWORD" not in json.dumps(result)


def test_analysis_uses_failure_dom_and_private_trace_projection_without_sensitive_text(
    settings, database, tmp_path, monkeypatch
):
    import backend.app.workers.analysis as module

    store = LocalObjectStore(tmp_path)
    store.put_bytes("old.html", b"old step DOM", "text/html")
    store.put_bytes("failure.html", b"failing step DOM", "text/html")
    store.put_bytes("sensitive.html", b"PRIVATE_TEXT", "text/html")
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        archive.writestr(
            "0.trace",
            json.dumps({"type": "before", "callId": "1", "apiName": "page.fill", "params": {"value": "PRIVATE_INPUT"}}),
        )
    store.put_bytes("trace.zip", payload.getvalue(), "application/zip")
    set_object_store(store)

    def artifact(key, *, step="s2", kind="DOM", sensitivity="NORMAL", publish=True, upload="READY"):
        return SimpleNamespace(
            id=key,
            object_key=key,
            name=key,
            size=10,
            step_id=step,
            kind=kind,
            sensitivity=sensitivity,
            publish_allowed=publish,
            upload_status=upload,
        )

    rows = [
        artifact("old.html", step="s1"),
        artifact("failure.html"),
        artifact("sensitive.html", sensitivity="SENSITIVE"),
        artifact("trace.zip", step=None, kind="TRACE", sensitivity="SENSITIVE", publish=False),
        artifact("missing.html", upload="FAILED"),
    ]
    monkeypatch.setattr(module.ArtifactRepository, "for_execution", lambda *_: rows)
    worker = AnalysisWorker(settings=settings)
    facts = {"tenant_id": "tenant", "execution_id": "run", "evidence_mode": "NORMAL", "artifact_refs": []}
    result = worker._evidence(facts, {"step_id": "s2"})
    assert result["dom_snippets"] == ["artifact:failure.html: failing step DOM"]
    assert result["trace_actions"]["actions"] == [{"action": "page.fill"}]
    assert "PRIVATE" not in json.dumps(result)
    refs = [item["ref"] for item in result["available"]]
    assert "artifact:sensitive.html" not in refs
    assert "artifact:missing.html" not in refs
    assert "artifact:trace.zip" in refs
    assert worker._ref_exists("artifact:trace.zip", facts)
    assert not worker._ref_exists("artifact:other-run", facts)
    assert worker._evidence({**facts, "evidence_mode": "SENSITIVE"}, {"step_id": "s2"}).get("trace_actions") is None


def test_sensitive_run_keeps_rule_analysis_and_never_calls_the_model(settings, database, monkeypatch):
    worker = AnalysisWorker(settings=settings.model_copy(update={"ai_enabled": True}))
    facts = {
        "error_code": "ASSERTION_FAILED",
        "message": "PRIVATE_INPUT",
        "evidence_mode": "SENSITIVE",
        "failing_step": {"step_id": "s1", "error_detail": {"actual": "PRIVATE_INPUT"}},
    }
    monkeypatch.setattr(worker, "_facts", lambda *_: facts)
    monkeypatch.setattr(worker, "_start", lambda *_: ("analysis", "project"))
    monkeypatch.setattr(worker, "_write", Mock())
    monkeypatch.setattr(worker, "_set_status", Mock())
    explanation = Mock(side_effect=AssertionError("sensitive content reached the model"))
    monkeypatch.setattr(worker, "_explain", explanation)
    assert worker.run({"tenant_id": "tenant", "execution_id": "run"})["source"] == "rules"
    explanation.assert_not_called()


def test_analysis_images_are_opt_in_and_exclude_sensitive_or_other_steps(settings, database, tmp_path, monkeypatch):
    import backend.app.workers.analysis as module

    store = LocalObjectStore(tmp_path)
    store.put_bytes("shot.png", b"\x89PNG\r\n\x1a\n" + b"image", "image/png")
    set_object_store(store)
    row = SimpleNamespace(
        id="artifact",
        kind="SCREENSHOT",
        step_id="s1",
        publish_allowed=True,
        sensitivity="NORMAL",
        upload_status="READY",
        media_type="image/png",
        object_key="shot.png",
    )
    monkeypatch.setattr(module.ArtifactRepository, "for_execution", lambda *_: [row])
    facts = {"tenant_id": "tenant", "execution_id": "run", "evidence_mode": "NORMAL", "failing_step": {"step_id": "s1"}}
    worker = AnalysisWorker(settings=settings)
    monkeypatch.setattr(worker, "_brief", lambda *_: {})
    model = SimpleNamespace(
        chat_json=Mock(return_value=SimpleNamespace(content="{}", usage=SimpleNamespace(model="fake"))),
        complete_json=lambda _: {},
    )
    worker._explain(model, facts, {})
    assert isinstance(model.chat_json.call_args.args[0][1]["content"], str)
    worker.settings = settings.model_copy(update={"ai_analysis_images_enabled": True})
    worker._explain(model, facts, {})
    assert any(part["type"] == "image_url" for part in model.chat_json.call_args.args[0][1]["content"])
    assert worker._images({**facts, "evidence_mode": "SENSITIVE"}) == []
    row.sensitivity = "SENSITIVE"
    assert worker._images(facts) == []
    row.sensitivity, row.step_id = "NORMAL", "s2"
    assert worker._images(facts) == []


def test_screenshot_mask_failure_never_publishes_an_unmasked_image():
    sink = SimpleNamespace(put_bytes=Mock())
    collector = EvidenceCollector(sink)
    page = SimpleNamespace(evaluate=AsyncMock(side_effect=RuntimeError("page unavailable")), screenshot=AsyncMock())
    assert asyncio.run(collector.screenshot(page, name="failure")) is None
    page.screenshot.assert_not_called()
    sink.put_bytes.assert_not_called()


def test_database_payloads_are_encrypted_and_legacy_migration_is_idempotent(database):
    metadata = MetaData()
    table = Table(
        "encryption_probe",
        metadata,
        Column("id", String, primary_key=True),
        Column("markdown", EncryptedText),
        Column("payload", EncryptedJSON),
    )
    metadata.create_all(database.engine)
    secret = "PRIVATE_TEST_DATA"
    with database.engine.begin() as connection:
        connection.execute(table.insert().values(id="encrypted", markdown=secret, payload={"value": secret}))
        raw = connection.execute(text("SELECT markdown, payload FROM encryption_probe")).first()
        assert secret not in " ".join(raw)
        assert ENVELOPE_PREFIX.decode() in raw.markdown
        decoded = connection.execute(select(table)).first()
        assert decoded.markdown == secret
        assert decoded.payload == {"value": secret}
        connection.execute(
            text("INSERT INTO encryption_probe VALUES (:id, :md, :payload)"),
            {"id": "legacy", "md": secret, "payload": json.dumps({"value": secret})},
        )
        connection.execute(table.insert().values(id="empty", markdown=None, payload=None))
    assert migrate_database(database, metadata=metadata) == 1
    assert migrate_database(database, metadata=metadata) == 0
    with database.engine.connect() as connection:
        assert connection.execute(select(table.c.payload).where(table.c.id == "legacy")).scalar_one() == {
            "value": secret
        }


def test_objects_are_authenticated_encrypted_and_legacy_migration_keeps_bytes(settings, tmp_path):
    config = settings.model_copy(update={"data_dir": tmp_path, "artifact_max_bytes": 10_000})
    raw = LocalObjectStore(config.local_store_dir)
    store = EncryptedObjectStore(raw, config)
    secret = b"PRIVATE_TEST_DATA" * 300
    stored = store.put_bytes("object.bin", secret, "application/octet-stream")
    assert stored.size == len(secret)
    encrypted = raw.read_bytes("object.bin")
    assert secret[:17] not in encrypted
    assert encrypted.startswith(ENVELOPE_PREFIX)
    assert store.read_bytes("object.bin") == secret
    assert store.read_prefix("object.bin", max_bytes=10) == secret[:10]
    with pytest.raises(ApiError):
        store.read_bytes("object.bin", max_bytes=10)
    with pytest.raises(InvalidToken):
        StorageCipher(settings.model_copy(update={"data_dir": tmp_path / "other-key"})).decrypt(encrypted)
    raw.put_bytes("legacy.bin", secret, "application/octet-stream")
    assert store.read_bytes("legacy.bin") == secret
    assert migrate_local_objects(config) == 1
    assert migrate_local_objects(config) == 0
    assert store.read_bytes("legacy.bin") == secret


def test_secret_references_force_sensitive_mode_even_after_normalization():
    assert SecretStore.evidence_mode_for({"steps": [{"value": {"kind": "secret", "key": "password"}}]}) == "SENSITIVE"
    assert (
        SecretStore.evidence_mode_for({"steps": [{"value": "${secrets.password}"}]}, requested="NORMAL") == "SENSITIVE"
    )
    assert SecretStore.evidence_mode_for({"steps": [{"value": {"kind": "literal", "value": "normal"}}]}) == "NORMAL"


def test_s3_full_read_rejects_oversize_and_prefix_read_is_bounded():
    bodies = []
    requests = []

    def get_object(**kwargs):
        requests.append(kwargs)
        body = io.BytesIO(b"0123456789")
        bodies.append(body)
        return {"Body": body}

    store = S3ObjectStore.__new__(S3ObjectStore)
    store.bucket = "test"
    store.client = SimpleNamespace(get_object=get_object, exceptions=SimpleNamespace(NoSuchKey=FileNotFoundError))
    with pytest.raises(ApiError):
        store.read_bytes("object.bin", max_bytes=4)
    assert "Range" not in requests[0]
    assert bodies[0].closed
    assert store.read_prefix("object.bin", max_bytes=4) == b"0123"
    assert requests[1]["Range"] == "bytes=0-3"
    assert bodies[1].closed


def test_s3_migration_handles_empty_objects_preserves_types_and_is_idempotent(settings, monkeypatch):
    import scripts.encrypt_storage as migration

    config = settings.model_copy(update={"artifact_max_bytes": 100})
    cipher = StorageCipher(config)
    objects = {
        "tenants/empty.bin": b"",
        "tenants/plain.txt": b"private text",
        "tenants/encrypted.bin": cipher.encrypt(b"x" * 100),
        "tenants/incomplete.part": b"unfinished",
        "tenants/directory/": b"",
    }
    requests = []

    def get_object(**kwargs):
        requests.append(kwargs)
        assert "Range" not in kwargs
        return {"Body": io.BytesIO(objects[kwargs["Key"]])}

    def put_object(**kwargs):
        assert kwargs["ContentType"] == "text/plain"
        objects[kwargs["Key"]] = kwargs["Body"]

    raw = S3ObjectStore.__new__(S3ObjectStore)
    raw.bucket = "test"
    raw.client = SimpleNamespace(
        get_object=get_object,
        put_object=put_object,
        head_object=lambda **_: {"ContentType": "text/plain"},
        get_paginator=lambda _: SimpleNamespace(paginate=lambda **_: [{"Contents": [{"Key": key} for key in objects]}]),
        exceptions=SimpleNamespace(NoSuchKey=FileNotFoundError),
    )
    monkeypatch.setattr(migration, "S3ObjectStore", lambda _: raw)
    assert migrate_s3_objects(config) == 2
    assert migrate_s3_objects(config) == 0
    assert cipher.decrypt(objects["tenants/empty.bin"]) == b""
    assert cipher.decrypt(objects["tenants/plain.txt"]) == b"private text"
    assert objects["tenants/incomplete.part"] == b"unfinished"
    assert len(requests) == 6


def test_log_budget_preserves_valid_json_and_recent_entries(settings, monkeypatch):
    sink = DatabaseEvidenceSink(
        tenant_id="tenant",
        project_id="project",
        execution_id="run",
        settings=settings.model_copy(update={"dom_evidence_max_bytes": 50}),
        store=None,
    )
    write = Mock(return_value="artifact")
    monkeypatch.setattr(sink, "put_bytes", write)
    sink.record_console([{"index": i, "message": "abcdefgh"} for i in range(10)])
    payload = write.call_args.kwargs["data"]
    assert len(payload) <= 50
    assert json.loads(payload) == [{"index": 9, "message": "abcdefgh"}]
    assert sink.truncations


def test_local_store_handles_long_object_keys(tmp_path):
    store = LocalObjectStore(tmp_path)
    key = "tenants/" + "a" * 36 + "/projects/" + "b" * 36 + "/executions/" + "c" * 36 + "/" + "d" * 36 + ".png"
    store.put_bytes(key, b"image", "image/png")
    assert store.read_bytes(key) == b"image"


def test_human_input_disables_ai_and_blocks_the_video_created_at_session_close(
    settings, database, tmp_path, monkeypatch
):
    from backend.app.repositories.executions import ExecutionRepository

    ctx = context()
    ctx.vision = object()
    ctx.evidence = SimpleNamespace(evidence_mode="NORMAL")
    raw_video = tmp_path / "human.webm"
    raw_video.write_bytes(b"private-human-input")
    page = SimpleNamespace(
        video=SimpleNamespace(path=AsyncMock(return_value=str(raw_video))), is_closed=lambda: False, close=AsyncMock()
    )
    browser = SimpleNamespace(
        trace_started=True,
        video_publish_allowed=True,
        config=SimpleNamespace(record_video=True),
        context=SimpleNamespace(tracing=SimpleNamespace(stop=AsyncMock())),
        page=page,
        scratch_dir=tmp_path,
    )
    monkeypatch.setattr(ExecutionRepository, "countersigned_update", Mock(return_value=True))
    gate = HumanGate("run", "tenant", "project", "worker", browser, ctx, None, settings)
    asyncio.run(gate._suspend_sensitive_capture())
    assert ctx.evidence_mode == ctx.evidence.evidence_mode == "SENSITIVE"
    assert ctx.vision is None
    assert not browser.trace_started
    assert not browser.video_publish_allowed
    collector = SimpleNamespace(put_file=Mock())
    asyncio.run(PlaywrightExecutor(settings)._capture_video(browser, collector, SimpleNamespace(artifact_ids=[])))
    collector.put_file.assert_not_called()
    assert not raw_video.exists()
