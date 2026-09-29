"""Repository behaviour required by §9, §10 and §11.3."""

from __future__ import annotations

import pytest

# aliased: a bare `TestExecution` name in a test module makes pytest try to collect the ORM model
from backend.app.db.models import Outbox
from backend.app.db.models import TestExecution as ExecutionRow
from backend.app.domain.enums import (
    DispatchState,
    ExecutionStatus,
    HumanTaskStatus,
    Outcome,
    ReservationStatus,
    StepStatus,
)
from backend.app.domain.errors import ApiError, ErrorCode
from backend.app.orchestrator.events import COMPILE_REQUEST
from backend.app.repositories import (
    ArtifactRepository,
    CaseRepository,
    CommandRepository,
    ElementMemoryRepository,
    ExecutionRepository,
    HumanTaskRepository,
    IdempotencyRepository,
    OutboxRepository,
    PoolRepository,
    ReservationRepository,
    TagRepository,
    request_digest,
    source_digest,
)
from backend.app.services.cases import CaseService, compile_now
from sqlalchemy import select


@pytest.fixture
def case(session, workspace, settings):
    markdown = "# smoke\n\n## Step 1\n```yaml\naction: open\nurl: https://example.com\n```\n"
    repo = CaseRepository(session, workspace["tenant_id"])
    case, revision = repo.create(
        project_id=workspace["project_id"],
        name="smoke",
        markdown=markdown,
        dsl_version="1.0",
        title="smoke",
        created_by=workspace["engineer_user_id"],
    )
    session.commit()
    return case, revision


def make_execution(session, workspace, case, revision) -> ExecutionRow:
    from backend.app.db.base import new_id
    from backend.app.db.models import CompileArtifact

    artifact = CompileArtifact(
        id=new_id(),
        tenant_id=workspace["tenant_id"],
        project_id=workspace["project_id"],
        revision_id=revision.id,
        status="SUCCEEDED",
        source_digest=revision.source_digest,
        compiler_version="1.0.0",
        ir={"ir_version": "1.0", "steps": []},
        ir_digest=source_digest("ir"),
        dedupe_key=f"compile:{revision.id}:{new_id()}",
    )
    session.add(artifact)
    session.flush()
    repo = ExecutionRepository(session, workspace["tenant_id"])
    execution = repo.create(
        project_id=workspace["project_id"],
        case_id=case.id,
        revision_id=revision.id,
        compile_artifact_id=artifact.id,
        environment_id=workspace["environment_id"],
        environment_revision_id=workspace["environment_revision_id"],
        ir={"ir_version": "1.0", "steps": []},
        ir_digest=source_digest("x"),
        snapshot={"variables": {"base_url": "https://example.com"}},
        requested_by=workspace["engineer_user_id"],
        browser="chromium",
        evidence_mode="NORMAL",
    )
    session.commit()
    return execution


def test_revision_is_immutable_and_row_version_guards_edits(session, workspace, case):
    existing, revision = case
    repo = CaseRepository(session, workspace["tenant_id"])
    next_revision = repo.add_revision(
        existing, markdown="# smoke2\n", dsl_version="1.0", title="smoke2", created_by=None
    )
    assert next_revision.version == 2
    assert repo.revision(revision.id).markdown == existing_markdown(session, revision.id)
    with pytest.raises(ApiError) as excinfo:
        repo.add_revision(
            existing, markdown="# conflict\n", dsl_version="1.0", title="x", created_by=None, expected_row_version=1
        )
    assert excinfo.value.code is ErrorCode.VERSION_CONFLICT


def existing_markdown(session, revision_id: str) -> str:
    from backend.app.db.models import CaseRevision

    return session.get(CaseRevision, revision_id).markdown


def test_tags_and_digest_helpers(session, workspace, case):
    existing, _ = case
    tags = TagRepository(session, workspace["tenant_id"])
    applied = tags.set_case_tags(existing, ["smoke", "New Tag"])
    session.commit()
    assert {tag.name for tag in applied} == {"smoke", "New Tag"}
    assert len(tags.tag_ids_for_case(existing.id)) == 2
    assert source_digest("a\nb") == source_digest("a\r\nb")


def test_transition_enforces_the_state_machine(session, workspace, case):
    existing, revision = case
    executions = ExecutionRepository(session, workspace["tenant_id"])
    execution = make_execution(session, workspace, existing, revision)
    assert execution.status == ExecutionStatus.CREATED.value

    queued = executions.transition(execution.id, to_status=ExecutionStatus.QUEUED.value, expected_state_version=1)
    assert (queued.status, queued.state_version, queued.outcome) == ("QUEUED", 2, None)

    with pytest.raises(ApiError) as excinfo:
        executions.transition(execution.id, to_status=ExecutionStatus.FINISHED.value, outcome=Outcome.PASSED.value)
    assert excinfo.value.code is ErrorCode.CONFLICT

    with pytest.raises(ApiError) as stale:
        executions.transition(execution.id, to_status=ExecutionStatus.RUNNING.value, expected_state_version=1)
    assert stale.value.code is ErrorCode.CONFLICT

    # FINALIZING without a decided outcome is rejected, and the CHECK constraint would reject the row too
    with pytest.raises(ApiError) as missing_outcome:
        executions.transition(execution.id, to_status=ExecutionStatus.FINALIZING.value)
    assert missing_outcome.value.code is ErrorCode.CONFLICT
    assert executions.load(execution.id).status == ExecutionStatus.QUEUED.value


def test_claim_bumps_lease_epoch_and_heartbeat_keeps_it(session, workspace, case):
    existing, revision = case
    executions = ExecutionRepository(session, workspace["tenant_id"])
    execution = make_execution(session, workspace, existing, revision)
    executions.transition(execution.id, to_status=ExecutionStatus.QUEUED.value, expected_state_version=1)
    claimed, epoch = executions.mark_running(execution.id, worker_id="w1", expected_state_version=2, generation=1)
    assert (claimed.status, claimed.owner_worker_id, claimed.lease_epoch) == ("RUNNING", "w1", epoch)
    assert epoch == 1
    assert executions.heartbeat(execution.id, epoch=epoch) is True
    assert executions.heartbeat(execution.id, epoch=epoch + 5) is False
    assert executions.epoch_is_current(execution.id, epoch=epoch) is True
    assert executions.epoch_is_current(execution.id, epoch=epoch + 1) is False

    with pytest.raises(ApiError) as second:
        executions.mark_running(
            execution.id, worker_id="w2", expected_state_version=claimed.state_version, generation=1
        )
    assert second.value.code is ErrorCode.LEASE_LOST

    finished = executions.transition(
        execution.id,
        to_status=ExecutionStatus.FINALIZING.value,
        outcome=Outcome.PASSED.value,
        expected_epoch=epoch,
    )
    final = executions.transition(
        execution.id, to_status=ExecutionStatus.FINISHED.value, expected_state_version=finished.state_version
    )
    assert final.outcome == Outcome.PASSED.value
    with pytest.raises(ApiError) as late:
        executions.transition(execution.id, to_status=ExecutionStatus.FINALIZING.value, outcome=Outcome.FAILED.value)
    assert late.value.code is ErrorCode.CONFLICT
    assert executions.load(execution.id).outcome == Outcome.PASSED.value


def test_events_are_sequential_per_execution(session, workspace, case):
    existing, revision = case
    executions = ExecutionRepository(session, workspace["tenant_id"])
    execution = make_execution(session, workspace, existing, revision)
    executions.transition(execution.id, to_status=ExecutionStatus.QUEUED.value, expected_state_version=1)
    executions.append_event(execution, "execution.log", {"line": "hello"})
    session.commit()
    rows = executions.events_after(execution.id, 0)
    assert [row.seq for row in rows] == list(range(1, len(rows) + 1))
    assert rows[-1].payload["line"] == "hello"
    assert executions.load(execution.id).last_event_seq == len(rows)


def test_steps_dispatch_state_and_skip_semantics(session, workspace, case):
    existing, revision = case
    executions = ExecutionRepository(session, workspace["tenant_id"])
    execution = make_execution(session, workspace, existing, revision)
    executions.add_steps(
        execution,
        [
            {"id": "s1", "action": "open", "target": {"description": "首页"}},
            {"id": "s2", "action": "click", "target": {"description": "登录按钮"}},
            {
                "id": "s3",
                "action": "assert",
                "condition": {"kind": "page_contains", "expected": {"kind": "literal", "value": "欢迎"}},
            },
        ],
    )
    session.commit()
    executions.start_step(execution.id, "s1", epoch=1)
    executions.set_dispatch_state(
        execution.id, "s1", state=DispatchState.INTENT_RECORDED.value, detail={"key": "navigate"}
    )
    executions.finish_step(execution.id, "s1", status=StepStatus.PASSED.value)
    executions.start_step(execution.id, "s2", epoch=1)
    executions.set_dispatch_state(execution.id, "s2", state=DispatchState.ACKNOWLEDGED.value, detail={"key": "click"})
    executions.finish_step(
        execution.id, "s2", status=StepStatus.FAILED.value, error_code=ErrorCode.ASSERTION_FAILED.value, duration_ms=42
    )
    skipped = executions.skip_pending_after(execution.id, from_step_no=2)
    session.commit()
    assert skipped == ["s3"]
    steps = executions.steps(execution.id)
    assert [step.status for step in steps] == ["PASSED", "FAILED", "SKIPPED"]
    assert steps[0].dispatch_state == DispatchState.INTENT_RECORDED.value
    assert steps[1].dispatch_state == DispatchState.ACKNOWLEDGED.value
    assert steps[1].duration_ms == 42
    assert steps[2].step_no == 3
    assert steps[1].description == "登录按钮"
    assert executions.load(execution.id).last_event_seq >= 5


def test_cancel_only_stamps_intent_for_a_running_execution(session, workspace, case):
    existing, revision = case
    executions = ExecutionRepository(session, workspace["tenant_id"])
    execution = make_execution(session, workspace, existing, revision)
    executions.transition(execution.id, to_status=ExecutionStatus.QUEUED.value, expected_state_version=1)
    executions.mark_running(execution.id, worker_id="w1", expected_state_version=2, generation=1)
    executions.request_cancel(execution.id, requested_by=workspace["engineer_user_id"], reason="user pressed stop")
    session.commit()
    reloaded = executions.load(execution.id)
    assert reloaded.status == ExecutionStatus.RUNNING.value
    assert reloaded.cancel_requested_at is not None

    # a queued-but-unclaimed run may be closed directly by the cancel path
    other = make_execution(session, workspace, existing, revision)
    executions.transition(other.id, to_status=ExecutionStatus.QUEUED.value, expected_state_version=1)
    cancelled = executions.request_cancel(other.id, requested_by=None, reason="before start")
    session.commit()
    assert cancelled.status == ExecutionStatus.FINALIZING.value
    assert cancelled.outcome == Outcome.CANCELLED.value


def test_single_active_human_task_and_claim_cas(session, workspace, case):
    existing, revision = case
    executions = ExecutionRepository(session, workspace["tenant_id"])
    humans = HumanTaskRepository(session, workspace["tenant_id"])
    execution = make_execution(session, workspace, existing, revision)
    executions.transition(execution.id, to_status=ExecutionStatus.QUEUED.value, expected_state_version=1)
    executions.mark_running(execution.id, worker_id="w1", expected_state_version=2, generation=1)
    executions.transition(
        execution.id, to_status=ExecutionStatus.WAIT_HUMAN.value, expected_epoch=1, extra={"human_tasks_used": 1}
    )
    task = humans.create(
        project_id=workspace["project_id"],
        execution_id=execution.id,
        step_id="s1",
        reason="human_policy_before",
        detail="需要人工确认",
        session_epoch=1,
        resume_phase="before_action",
        resume_condition=None,
        timeout_seconds=300,
    )
    session.commit()
    assert task.status == HumanTaskStatus.PENDING.value

    with pytest.raises(ApiError) as duplicate:
        humans.create(
            project_id=workspace["project_id"],
            execution_id=execution.id,
            step_id="s1",
            reason="again",
            detail=None,
            session_epoch=1,
            resume_phase=None,
            resume_condition=None,
            timeout_seconds=60,
        )
    assert duplicate.value.code is ErrorCode.CONFLICT

    claimed = humans.claim(task.id, actor_id=workspace["admin_user_id"], control_ttl_seconds=60)
    session.commit()
    assert claimed.assignee_id == workspace["admin_user_id"]
    with pytest.raises(ApiError) as taken:
        humans.claim(task.id, actor_id=workspace["engineer_user_id"], control_ttl_seconds=60)
    assert taken.value.code is ErrorCode.HUMAN_TASK_TAKEN

    resumed = humans.request_resume(task.id, actor_id=workspace["admin_user_id"])
    session.commit()
    assert resumed.status == HumanTaskStatus.RESUME_REQUESTED.value
    humans.finish(task.id, status=HumanTaskStatus.COMPLETED.value, note="done")
    session.commit()
    assert humans.active_for(execution.id) is None


def test_control_commands_are_durable_and_deduplicated(session, workspace, case):
    existing, revision = case
    commands = CommandRepository(session, workspace["tenant_id"])
    execution = make_execution(session, workspace, existing, revision)
    first = commands.enqueue(
        project_id=workspace["project_id"],
        execution_id=execution.id,
        command_type="CANCEL",
        dedupe_key="req-1",
        requested_by=workspace["engineer_user_id"],
        payload={"reason": "stop"},
    )
    session.commit()
    again = commands.enqueue(
        project_id=workspace["project_id"],
        execution_id=execution.id,
        command_type="CANCEL",
        dedupe_key="req-1",
        requested_by=workspace["engineer_user_id"],
    )
    assert first.id == again.id
    assert [command.id for command in commands.pending(execution.id)] == [first.id]
    commands.mark(first.id, status="PROCESSED", result={"accepted": True})
    session.commit()
    assert commands.pending(execution.id) == []


def test_reservation_accounting_locks_the_pool(session, workspace, case):
    existing, revision = case
    pools = PoolRepository(session, None)
    reservations = ReservationRepository(session, workspace["tenant_id"])
    pool = pools.by_name("default")
    assert pool.capacity == 4

    execution = make_execution(session, workspace, existing, revision)
    assert pools.reserve(pool.id, expected_reserved_count=0) is True
    reservation = reservations.create(
        tenant_id=workspace["tenant_id"],
        project_id=workspace["project_id"],
        execution_id=execution.id,
        pool_id=pool.id,
        ttl_seconds=60,
    )
    session.commit()
    assert reservation.status == ReservationStatus.RESERVED.value
    assert pools.occupying_count(pool.id) == 1
    assert pools.reserve(pool.id, expected_reserved_count=99) is False

    assert reservations.activate(reservation.id, generation=2) is None
    activated = reservations.activate(reservation.id, generation=1)
    assert activated is not None
    assert activated.status == ReservationStatus.ACTIVE.value
    assert activated.expires_at is None
    # an already claimed reservation is never recycled back to the queue (§9.3)
    assert reservations.requeue(activated.id, ttl_seconds=60) is None
    reservations.release(activated.id)
    pools.release(pool.id)

    second = make_execution(session, workspace, existing, revision)
    unclaimed = reservations.create(
        tenant_id=workspace["tenant_id"],
        project_id=workspace["project_id"],
        execution_id=second.id,
        pool_id=pool.id,
        ttl_seconds=60,
    )
    assert pools.reserve(pool.id) is True
    session.commit()
    recycled = reservations.requeue(unclaimed.id, ttl_seconds=60)
    session.commit()
    assert recycled is not None
    assert recycled.generation == 2
    assert reservations.current_for_execution(second.id).id == recycled.id
    reservations.release(recycled.id)
    reservations.release(unclaimed.id)
    pools.release(pool.id)
    session.commit()
    assert pools.occupying_count(pool.id) == 0
    assert reservations.current_for_execution(execution.id) is None


def test_outbox_deduplicates_and_claims_once(session, workspace):
    outbox = OutboxRepository(session, workspace["tenant_id"])
    row = outbox.enqueue(
        aggregate_id="exec-1", event_type="execution.enqueue", payload={"execution_id": "exec-1"}, discriminator="1"
    )
    session.commit()
    duplicate = outbox.enqueue(
        aggregate_id="exec-1", event_type="execution.enqueue", payload={"execution_id": "exec-1"}, discriminator="1"
    )
    assert duplicate.id == row.id
    claimed = outbox.claim_pending(limit=5)
    assert [item.id for item in claimed] == [row.id]
    assert outbox.claim_pending(limit=5) == []
    outbox.mark_published(row.id)
    assert outbox.pending_count() == 0


def test_saves_compile_deterministically_until_the_caller_opts_into_ai(session, workspace, settings, database):
    """A plain save must never put case text in front of a model; the AI path is an explicit opt-in (§6.2)."""
    tenant_id, project_id = workspace["tenant_id"], workspace["project_id"]
    # The service opens its own transaction, so the fixture's seeding has to be committed first.
    session.commit()
    markdown = "# smoke\n\n## Step 1\n```yaml\naction: open\nurl: https://example.com\n```\n"
    saved = CaseService(settings).create(
        tenant_id=tenant_id,
        project_id=project_id,
        name="ai-default",
        markdown=markdown,
        dsl_version="1.0",
        title="smoke",
        created_by=None,
    )

    def queued_use_ai() -> list[bool]:
        with database.session(tenant_id) as scope:
            rows = scope.execute(
                select(Outbox.payload).where(Outbox.event_type == COMPILE_REQUEST).order_by(Outbox.created_at)
            ).all()
            return [bool(row[0].get("use_ai")) for row in rows]

    assert queued_use_ai() == [False], "saving alone must not queue a model call"

    with database.session(tenant_id) as scope:
        compile_now(
            scope,
            tenant_id,
            project_id=project_id,
            revision_id=str(saved["revision_id"]),
            created_by=None,
            use_ai=True,
            force=True,
        )
        scope.commit()

    assert queued_use_ai() == [False, True], "compile_now has to carry the caller's choice to the worker"


def test_idempotency_rejects_a_changed_payload(session, workspace):
    records = IdempotencyRepository(session, workspace["tenant_id"])
    payload = {"environment_id": "env-1"}
    record = records.reserve(
        actor_id=workspace["engineer_user_id"], route="POST /executions", key="k1", payload=payload, ttl_hours=24
    )
    records.complete(record, resource_id="exec-1", response={"execution_id": "exec-1"})
    session.commit()

    found = records.lookup(actor_id=workspace["engineer_user_id"], route="POST /executions", key="k1", payload=payload)
    assert found.resource_id == "exec-1"
    assert found.request_digest == request_digest(payload)
    with pytest.raises(ApiError) as conflict:
        records.lookup(
            actor_id=workspace["engineer_user_id"],
            route="POST /executions",
            key="k1",
            payload={"environment_id": "env-2"},
        )
    assert conflict.value.code is ErrorCode.IDEMPOTENCY_CONFLICT


def test_artifact_and_memory_indexes(session, workspace, case):
    existing, revision = case
    artifacts = ArtifactRepository(session, workspace["tenant_id"])
    memory = ElementMemoryRepository(session, workspace["tenant_id"])
    execution = make_execution(session, workspace, existing, revision)
    row = artifacts.record(
        project_id=workspace["project_id"],
        execution_id=execution.id,
        step_id="s1",
        kind="SCREENSHOT",
        name="failure",
        object_key="t/p/e/a.png",
        media_type="image/png",
        sensitivity="SENSITIVE",
        size=1234,
        sha256="sha256:abc",
        metadata={"viewport": "1280x720"},
    )
    session.commit()
    assert artifacts.for_execution(execution.id, kinds=("SCREENSHOT",))[0].id == row.id
    assert artifacts.summary(execution.id)["by_kind"] == {"SCREENSHOT": 1}
    assert row.publish_allowed is True
    artifacts.forbid_publish_for(execution_id=execution.id, kinds=("SCREENSHOT",), reason="sensitive mode")
    session.commit()
    assert artifacts.by_id(row.id).publish_allowed is False

    for _ in range(3):
        memory.note_success(
            project_id=workspace["project_id"],
            environment_id=workspace["environment_id"],
            origin="https://example.com",
            route_pattern="/",
            browser_family="chromium",
            target_fingerprint="fp-1",
            description="登录按钮",
            strategy="css",
            selector="#login",
            app_version="1.0",
        )
    session.commit()
    approved = memory.candidates(
        environment_id=workspace["environment_id"],
        origin="https://example.com",
        route_pattern="/",
        target_fingerprint="fp-1",
        browser_family="chromium",
    )
    assert approved == []  # UNREVIEWED candidates are never offered automatically (§8.3)
    entry = memory.list_for_project(workspace["project_id"])[0]
    memory.approve(entry.id)
    session.commit()
    assert (
        len(
            memory.candidates(
                environment_id=workspace["environment_id"],
                origin="https://example.com",
                route_pattern="/",
                target_fingerprint="fp-1",
                browser_family="chromium",
            )
        )
        == 1
    )
    for _ in range(3):
        memory.note_failure(
            environment_id=workspace["environment_id"],
            origin="https://example.com",
            route_pattern="/",
            strategy="css",
            selector="#login",
            target_fingerprint="fp-1",
            browser_family="chromium",
        )
    session.commit()
    assert memory.by_id(entry.id).approval_status == "REVOKED"


def test_tenant_isolation_blocks_cross_tenant_reads(session, workspace, case):
    existing, revision = case
    other_tenant = "ffffffff-0000-4000-8000-000000000001"
    execution = make_execution(session, workspace, existing, revision)
    intruder = ExecutionRepository(session, other_tenant)
    assert intruder.by_id(execution.id) is None
    with pytest.raises(ApiError) as excinfo:
        intruder.require(execution.id)
    assert excinfo.value.code is ErrorCode.NOT_FOUND
    assert CaseRepository(session, other_tenant).by_id(existing.id) is None


def test_dispatch_state_only_moves_forward(session, workspace, case):
    """§9.4: a late ACK for an already-recorded intent must not resurrect a stale state."""
    existing, revision = case
    executions = ExecutionRepository(session, workspace["tenant_id"])
    execution = make_execution(session, workspace, existing, revision)
    executions.add_steps(execution, [{"id": "s1", "action": "open", "target": {"description": "首页"}}])
    session.commit()
    executions.start_step(execution.id, "s1", epoch=1)
    step = executions.step(execution.id, "s1")

    executions.set_dispatch_state(execution.id, "s1", state=DispatchState.ACKNOWLEDGED.value)
    assert executions.step(execution.id, "s1").dispatch_state == DispatchState.ACKNOWLEDGED.value

    rows = executions.set_dispatch_state(execution.id, "s1", state=DispatchState.INTENT_RECORDED.value)
    session.commit()
    assert rows == 0
    assert executions.step(execution.id, "s1").dispatch_state == DispatchState.ACKNOWLEDGED.value

    executions.set_dispatch_state(execution.id, "s1", state=DispatchState.ACKNOWLEDGED.value, for_step=step)
    session.commit()
    assert executions.step(execution.id, "s1").dispatch_state == DispatchState.ACKNOWLEDGED.value
