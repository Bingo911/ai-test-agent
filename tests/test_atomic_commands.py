"""M1: one transaction, one committer, one answer per key (§9.2, §9.3, AC-11~17, AC-40).

These are wire-level promises about what a retry does, so they are exercised through the REST adapter
rather than against the helper in isolation: the diff a client feels is the combination of the route,
the unit of work, the record format and the order of the refusals. The legacy-format cases matter most,
because a record written before this change is the one piece of state the new code cannot rewrite.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from backend.app.application.idempotency import ATOMIC_PREFIX, INTERNAL_FORMAT, atomic_request_digest
from backend.app.application.unit_of_work import UnitOfWork, _is_busy_wait, _lock_digest
from backend.app.compiler.pipeline import compile_revision as compile_text
from backend.app.db.base import new_id, utcnow
from backend.app.db.models import (
    AuditLog,
    CaseRevision,
    CompileArtifact,
    IdempotencyRecord,
    Outbox,
    ProjectMembership,
    TenantMembership,
    TestCase,
    TestExecution,
)
from backend.app.domain.enums import CompileStatus, ExecutionStatus, Outcome, Permission, Role
from backend.app.domain.errors import ApiError, ErrorCode
from backend.app.domain.rbac import Identity
from backend.app.ir.models import COMPILER_VERSION
from backend.app.main import create_app
from backend.app.repositories.cases import CaseRepository, CompileRepository, compile_dedupe_key
from backend.app.repositories.platform import request_digest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

MARKDOWN = """---
dsl_version: "1.0"
---
# 登录冒烟测试

## Step 1
```yaml
action: open
url: "${env.base_url}/index.html"
```
"""


@pytest.fixture
def client(database, session, workspace, settings) -> TestClient:
    # The seeded workspace is only visible to a request's own session once the fixture's has committed.
    session.commit()
    # No context manager: the lifespan starts the supervisor, and nothing a case save does needs it.
    return TestClient(create_app(settings))


@pytest.fixture
def auth(settings) -> dict[str, str]:
    return {"Authorization": f"Bearer {settings.dev_engineer_token}"}


@pytest.fixture
def admin_auth(settings) -> dict[str, str]:
    return {"Authorization": f"Bearer {settings.dev_admin_token}"}


def save(client: TestClient, workspace: dict[str, str], auth: dict[str, str], *, key: str | None = None, **body: Any):
    headers = dict(auth)
    if key is not None:
        headers["Idempotency-Key"] = key
    payload = {"name": "login-smoke", "markdown": MARKDOWN, "dsl_version": "1.0", **body}
    return client.post(f"/api/v1/projects/{workspace['project_id']}/cases", json=payload, headers=headers)


def counts(database) -> dict[str, int]:
    """Every row a command can write, so a replay can be proved to have written none of them."""
    with database.session() as session:
        return {
            "cases": int(session.scalar(select(func.count()).select_from(TestCase)) or 0),
            "revisions": int(session.scalar(select(func.count()).select_from(CaseRevision)) or 0),
            "compiles": int(session.scalar(select(func.count()).select_from(CompileArtifact)) or 0),
            "executions": int(session.scalar(select(func.count()).select_from(TestExecution)) or 0),
            "outbox": int(session.scalar(select(func.count()).select_from(Outbox)) or 0),
            "audit": int(session.scalar(select(func.count()).select_from(AuditLog)) or 0),
            "records": int(session.scalar(select(func.count()).select_from(IdempotencyRecord)) or 0),
        }


def record(database, *, route: str, key: str) -> IdempotencyRecord | None:
    with database.session() as session:
        return session.scalar(
            select(IdempotencyRecord).where(
                IdempotencyRecord.route == route, IdempotencyRecord.key == key
            )
        )


def remember_legacy(
    database,
    workspace: dict[str, str],
    *,
    route: str,
    key: str,
    payload: Any,
    answer: Any,
    resource_id: str | None = None,
) -> None:
    """Insert a record shaped exactly like the one the pre-upgrade code left behind (§9.2.2)."""
    with database.session() as session:
        session.add(
            IdempotencyRecord(
                id=new_id(),
                tenant_id=workspace["tenant_id"],
                actor_id=workspace["engineer_user_id"],
                route=route,
                key=key,
                request_digest=request_digest(payload),
                resource_id=resource_id,
                response=answer,
                expires_at=utcnow() + timedelta(hours=1),
            )
        )
        session.commit()


def run(client: TestClient, auth: dict[str, str], body: dict[str, Any], *, key: str | None = None):
    headers = dict(auth)
    if key is not None:
        headers["Idempotency-Key"] = key
    return client.post("/api/v1/executions", json=body, headers=headers)


def compile_offline(database, workspace: dict[str, str], settings, revision_id: str) -> CompileArtifact:
    """Write the artifact a worker would have written, without starting the queue.

    A run reads an artifact's status, IR and digest and nothing else, so a deterministic compile
    recorded here is the same input the worker produces (§6.2). Starting the supervisor would test
    the worker rather than the command.
    """
    tenant_id = workspace["tenant_id"]
    with database.session(tenant_id) as session:
        revision = CaseRepository(session, tenant_id).require_revision(revision_id)
        outcome = compile_text(revision.markdown, revision_id=revision.id, settings=settings)
        assert outcome.status == CompileStatus.SUCCEEDED.value, outcome.diagnostics
        compiles = CompileRepository(session, tenant_id)
        artifact = compiles.create_pending(
            project_id=revision.project_id,
            revision_id=revision.id,
            source_digest_value=revision.source_digest,
            compiler_version=COMPILER_VERSION,
            dedupe_key=compile_dedupe_key(
                tenant_id,
                revision.id,
                revision.source_digest,
                compiler_version=COMPILER_VERSION,
                use_ai=False,
            ),
        )
        compiles.record_result(
            artifact,
            status=outcome.status,
            ir=outcome.ir,
            ir_digest=outcome.ir_digest,
            diagnostics=outcome.diagnostics,
            review_items=outcome.review_items,
            usage=outcome.usage,
            model=outcome.model,
            prompt_version=outcome.prompt_version,
            compiler_mode=outcome.compiler_mode,
        )
        session.commit()
        return artifact


def current_tag(client: TestClient, auth: dict[str, str], case_id: str) -> str:
    """The ETag as the resource reports it, because a test must not guess how many versions a save left."""
    response = client.get(f"/api/v1/cases/{case_id}", headers=auth)
    assert response.status_code == 200, response.text
    return str(response.headers["etag"])


# ----------------------------------------------------------------------- replay


def test_a_saved_case_answers_the_same_key_with_the_same_resource(client, database, workspace, auth):
    """The whole point of the key: one resource, and a retry that cannot create a second one (§9.1)."""
    first = save(client, workspace, auth, key="case-key")
    assert first.status_code == 201, first.text
    second = save(client, workspace, auth, key="case-key")
    assert second.status_code == 201, second.text
    assert second.json() == first.json()
    assert first.headers["location"] == second.headers["location"]
    assert counts(database) == {
        "cases": 1,
        "revisions": 1,
        # The queued compile is part of the command, so replaying must not queue a second one.
        "compiles": 0,
        "executions": 0,
        "outbox": 1,
        "audit": 1,
        "records": 1,
    }


def test_the_same_key_with_different_text_is_a_conflict_not_a_second_case(client, database, workspace, auth):
    assert save(client, workspace, auth, key="intent").status_code == 201
    clash = save(client, workspace, auth, key="intent", markdown=MARKDOWN + "\n")
    assert clash.status_code == 409
    assert clash.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    assert counts(database)["cases"] == 1


def test_without_a_key_the_route_still_accepts_every_save(client, database, workspace, auth):
    """A client that names no key keeps the old contract: accepted, and no deduplication promised."""
    assert save(client, workspace, auth).status_code == 201
    assert save(client, workspace, auth).status_code == 201
    assert counts(database) == {
        "cases": 2,
        "revisions": 2,
        "compiles": 0,
        "executions": 0,
        "outbox": 2,
        "audit": 2,
        "records": 0,
    }


def test_an_expired_key_is_the_clients_to_use_again(client, database, workspace, auth):
    assert save(client, workspace, auth, key="stale").status_code == 201
    stored = record(database, route=f"POST /projects/{workspace['project_id']}/cases", key="stale")
    with database.session() as session:
        session.merge(stored).expires_at = utcnow() - timedelta(seconds=1)
        session.commit()
    assert save(client, workspace, auth, key="stale").status_code == 201
    assert counts(database)["cases"] == 2


def test_a_new_intent_needs_a_new_key_but_a_different_project_shares_the_name(client, database, workspace, auth):
    """The route is part of the key, so two projects cannot answer for each other (§9.2)."""
    assert save(client, workspace, auth, key="shared").status_code == 201
    other = client.post(
        "/api/v1/projects/other-project/cases",
        json={"name": "login-smoke", "markdown": MARKDOWN, "dsl_version": "1.0"},
        headers={**auth, "Idempotency-Key": "shared"},
    )
    assert other.status_code == 404


# ----------------------------------------------------------------- record format


def test_an_atomic_record_names_its_format_and_wraps_the_business_result(client, database, workspace, auth):
    saved = save(client, workspace, auth, key="shape")
    stored = record(database, route=f"POST /projects/{workspace['project_id']}/cases", key="shape")
    assert stored is not None
    assert stored.request_digest.startswith(ATOMIC_PREFIX)
    assert len(stored.request_digest) == 74
    assert stored.response == {"format": INTERNAL_FORMAT, "result": saved.json()}
    assert stored.resource_id == saved.json()["case_id"]


def test_a_legacy_record_is_replayed_as_the_answer_it_already_gave(client, database, workspace, auth):
    """Pre-upgrade rows stay readable, and reading one must not re-execute the write (§9.2.2)."""
    body = {"name": "login-smoke", "markdown": MARKDOWN, "title": None, "tags": [], "dsl_version": "1.0"}
    answer = {
        "case_id": "case-from-the-old-code",
        "revision_id": "rev-old",
        "revision_no": 1,
        "source_digest": "sha256:old",
    }
    remember_legacy(
        database,
        workspace,
        route=f"POST /projects/{workspace['project_id']}/cases",
        key="written-before",
        payload=body,
        answer=answer,
        resource_id="case-from-the-old-code",
    )

    replayed = save(client, workspace, auth, key="written-before")
    assert replayed.status_code == 201
    assert replayed.json() == answer
    assert counts(database) == {
        "cases": 0,
        "revisions": 0,
        "compiles": 0,
        "executions": 0,
        "outbox": 0,
        "audit": 0,
        "records": 1,
    }


def test_a_legacy_record_of_other_inputs_is_refused_rather_than_rerun(client, database, workspace, auth):
    """`sha256:<hex>` over a different body proves a different intent, so nothing may be written (§9.2.2)."""
    body = {"name": "login-smoke", "markdown": "different", "title": None, "tags": [], "dsl_version": "1.0"}
    remember_legacy(
        database,
        workspace,
        route=f"POST /projects/{workspace['project_id']}/cases",
        key="half-known",
        payload=body,
        answer={"case_id": "x"},
    )
    clash = save(client, workspace, auth, key="half-known")
    assert clash.status_code == 409
    assert clash.json()["error"]["details"] == {"record_format": "legacy"}
    assert counts(database)["cases"] == 0


def test_a_legacy_record_with_no_answer_is_never_taken_over(client, database, workspace, auth):
    """An incomplete old reservation proves nothing about the write, so the command stops (§9.3)."""
    remember_legacy(
        database,
        workspace,
        route=f"POST /projects/{workspace['project_id']}/cases",
        key="in-flight-then",
        payload={"name": "login-smoke"},
        answer=None,
    )
    unknown = save(client, workspace, auth, key="in-flight-then")
    assert unknown.status_code == 409
    assert unknown.json()["error"]["code"] == "IDEMPOTENCY_RESULT_UNKNOWN"
    assert counts(database)["cases"] == 0


def test_an_atomic_record_with_no_answer_is_equally_unknowable(client, database, workspace, auth):
    """A reservation that never completed says nothing about the write, so it is not taken over (§9.3)."""
    assert save(client, workspace, auth, key="v2-in-flight").status_code == 201
    route = f"POST /projects/{workspace['project_id']}/cases"
    stored = record(database, route=route, key="v2-in-flight")
    with database.session() as session:
        row = session.merge(stored)
        row.resource_id, row.response = None, None
        session.commit()

    unknown = save(client, workspace, auth, key="v2-in-flight")
    assert unknown.status_code == 409
    assert unknown.json()["error"]["code"] == "IDEMPOTENCY_RESULT_UNKNOWN"
    assert counts(database)["cases"] == 1


def test_an_atomic_record_of_other_inputs_conflicts(client, database, workspace, auth):
    with database.session() as session:
        session.add(
            IdempotencyRecord(
                id="atomic-other",
                tenant_id=workspace["tenant_id"],
                actor_id=workspace["engineer_user_id"],
                route=f"POST /projects/{workspace['project_id']}/cases",
                key="v2-other",
                request_digest=atomic_request_digest({"name": "something else"}),
                resource_id="case-x",
                response={"format": INTERNAL_FORMAT, "result": {"case_id": "case-x"}},
                expires_at=utcnow() + timedelta(hours=1),
            )
        )
        session.commit()
    clash = save(client, workspace, auth, key="v2-other")
    assert clash.status_code == 409
    assert clash.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"


# -------------------------------------------------------------------- atomicity


def test_a_failure_after_the_reservation_leaves_the_key_usable(client, database, workspace, auth, monkeypatch):
    """Reservation, business rows, outbox and audit are one transaction or none of them (§9.3.5)."""
    from backend.app.application import cases as case_commands

    def explode(*_args: Any, **_kwargs: Any) -> None:
        raise ApiError(ErrorCode.SEMANTIC_ERROR, "the compiler rejected this text")

    monkeypatch.setattr(case_commands.cases_core, "save_case", explode)
    failed = save(client, workspace, auth, key="doomed")
    assert failed.status_code == 422
    assert counts(database) == {
        "cases": 0,
        "revisions": 0,
        "compiles": 0,
        "executions": 0,
        "outbox": 0,
        "audit": 0,
        "records": 0,
    }

    monkeypatch.undo()
    assert save(client, workspace, auth, key="doomed").status_code == 201
    assert counts(database)["cases"] == 1


def test_a_replay_is_refused_when_the_caller_lost_the_grant(client, database, workspace, auth):
    """Replaying re-checks authority: a revoked membership cannot collect a stored answer (§9.3.1)."""
    assert save(client, workspace, auth, key="withdrawn").status_code == 201
    _revoke_everything(database, workspace)

    refused = save(client, workspace, auth, key="withdrawn")
    assert refused.status_code == 403, refused.text
    assert refused.json()["error"]["code"] == "FORBIDDEN"
    assert counts(database)["cases"] == 1


def _revoke_everything(database, workspace: dict[str, str]) -> None:
    """Drop both membership rows: the tenant role is what answers for a project when no project row does."""
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


def test_the_grant_is_re_read_inside_the_lock_rather_than_taken_from_the_request(
    database, session, workspace, settings
):
    """The request's identity is a snapshot taken before the command waited, so it cannot be the check (§9.3.1).

    Wire-level revocation is visible to both authorisations at once, which makes it a poor probe of the
    order; this drives the second one directly, with a stale snapshot of the caller in hand.
    """
    from backend.app.application import authorization
    from backend.app.application.context import CallContext
    from backend.app.application.unit_of_work import UnitOfWork

    session.commit()  # the seeded workspace only answers for the command's own session once committed
    stale = Identity(
        user_id=workspace["engineer_user_id"],
        tenant_id=workspace["tenant_id"],
        roles={"*": Role.ENGINEER, workspace["project_id"]: Role.ENGINEER},
    )
    call = CallContext(identity=stale, request_id="req", settings=settings)
    _revoke_everything(database, workspace)

    with UnitOfWork(database=database, call=call, write=True) as uow:
        assert stale.can(Permission.CASE_WRITE, workspace["project_id"])
        with pytest.raises(ApiError) as refused:
            authorization.require_after_lock(
                uow.scope, call, project_id=workspace["project_id"], permission=Permission.CASE_WRITE
            )
    assert refused.value.code is ErrorCode.FORBIDDEN


def test_a_stale_identity_is_refused_before_it_may_replay(database, session, workspace, settings):
    """The re-read sits between the lock and the replay, and that order is itself the promise (§9.3.1).

    A wire-level revocation cannot probe the order: it is visible to the route's own pre-lock authorisation
    too, so the request is refused long before the lock is taken. Only a caller who was still authorised
    when the request was admitted, and is not any more, separates the two orders - replay the stored answer,
    or refuse. That is why this drives the command directly with a snapshot in hand.
    """
    from backend.app.application import idempotency
    from backend.app.application.context import CallContext
    from backend.app.application.unit_of_work import UnitOfWork

    session.commit()  # the seeded workspace only answers for the command's own session once committed
    stale = Identity(
        user_id=workspace["engineer_user_id"],
        tenant_id=workspace["tenant_id"],
        roles={"*": Role.ENGINEER, workspace["project_id"]: Role.ENGINEER},
    )
    call = CallContext(identity=stale, request_id="req", settings=settings)
    route = f"POST /projects/{workspace['project_id']}/cases"
    command = {"name": "login-smoke", "markdown": MARKDOWN}
    key = "withdrawn-after-the-lock"

    def action() -> tuple[str, dict[str, Any]]:
        return "case-earned-before-revocation", {"case_id": "case-earned-before-revocation"}

    with UnitOfWork(database=database, call=call, write=True) as uow:
        earned = idempotency.execute_atomic_command(
            uow,
            route=route,
            key=key,
            command=command,
            action=action,
            project_id=workspace["project_id"],
            permission=Permission.CASE_WRITE,
        )
    assert earned["case_id"] == "case-earned-before-revocation"

    _revoke_everything(database, workspace)

    with UnitOfWork(database=database, call=call, write=True) as uow, pytest.raises(ApiError) as refused:
        idempotency.execute_atomic_command(
            uow,
            route=route,
            key=key,
            command=command,
            action=action,
            project_id=workspace["project_id"],
            permission=Permission.CASE_WRITE,
        )
    assert refused.value.code is ErrorCode.FORBIDDEN
    assert record(database, route=route, key=key).resource_id == "case-earned-before-revocation"


def test_a_precondition_that_only_applies_to_a_new_action_waits_for_the_replay_check(
    client, database, workspace, auth
):
    """Archiving a case must not invalidate a key that already saved it (§9.3.1)."""
    saved = save(client, workspace, auth, key="then-archived")
    case_id = saved.json()["case_id"]
    assert client.patch(f"/api/v1/cases/{case_id}", json={"archived": True}, headers=auth).status_code == 200
    assert save(client, workspace, auth, key="then-archived").status_code == 201
    assert counts(database)["cases"] == 1


# ---------------------------------------------------------------- revisions


def test_a_revision_key_replays_the_revision_it_created(client, database, workspace, auth):
    case_id = save(client, workspace, auth).json()["case_id"]
    headers = {**auth, "If-Match": current_tag(client, auth, case_id), "Idempotency-Key": "rev-key"}
    url = f"/api/v1/cases/{case_id}/revisions"
    body = {"markdown": MARKDOWN + "\n", "dsl_version": "1.0"}
    first = client.post(url, json=body, headers=headers)
    assert first.status_code == 201, first.text
    second = client.post(url, json=body, headers=headers)
    assert second.status_code == 201, second.text
    assert second.json() == first.json()
    assert counts(database) == {
        "cases": 1,
        "revisions": 2,
        "compiles": 0,
        "executions": 0,
        "outbox": 2,
        "audit": 2,
        "records": 1,
    }


def test_an_archived_case_frees_the_key_it_refused(client, database, workspace, auth):
    """Refusing to write must not consume the key, or the client could never retry anything (§9.3.5)."""
    case_id = save(client, workspace, auth).json()["case_id"]
    assert client.patch(f"/api/v1/cases/{case_id}", json={"archived": True}, headers=auth).status_code == 200
    body = {"markdown": MARKDOWN, "dsl_version": "1.0"}
    refused = client.post(
        f"/api/v1/cases/{case_id}/revisions",
        json=body,
        headers={**auth, "If-Match": current_tag(client, auth, case_id), "Idempotency-Key": "rejected"},
    )
    assert refused.status_code == 409, refused.text
    assert refused.json()["error"]["code"] == "CASE_ARCHIVED"
    assert counts(database)["records"] == 0

    assert client.patch(f"/api/v1/cases/{case_id}", json={"archived": False}, headers=auth).status_code == 200
    accepted = client.post(
        f"/api/v1/cases/{case_id}/revisions",
        json=body,
        headers={**auth, "If-Match": current_tag(client, auth, case_id), "Idempotency-Key": "rejected"},
    )
    assert accepted.status_code == 201, accepted.text
    assert counts(database)["records"] == 1


def test_a_revision_still_requires_if_match(client, database, workspace, auth):
    """The required-header refusal is the platform's existing 400, which this route keeps (§13.1)."""
    case_id = save(client, workspace, auth).json()["case_id"]
    missing = client.post(
        f"/api/v1/cases/{case_id}/revisions",
        json={"markdown": MARKDOWN, "dsl_version": "1.0"},
        headers={**auth, "Idempotency-Key": "no-etag"},
    )
    assert missing.status_code == 400, missing.text
    assert missing.json()["error"]["code"] == "VALIDATION_ERROR"
    # Refusing before the transaction opened means the key is still the client's to use.
    assert counts(database) == {
        "cases": 1,
        "revisions": 1,
        "compiles": 0,
        "executions": 0,
        "outbox": 1,
        "audit": 1,
        "records": 0,
    }


# --------------------------------------------------- compile, run and cancel commands


def _start_run(client: TestClient, database, workspace: dict[str, str], auth: dict[str, str], settings) -> str:
    saved = save(client, workspace, auth).json()
    compile_offline(database, workspace, settings, saved["revision_id"])
    started = run(client, auth, {"case_id": saved["case_id"]})
    assert started.status_code == 202, started.text
    return str(started.json()["id"])


def test_a_compile_key_replays_the_artifact_it_reserved(client, database, workspace, auth):
    """A compile answers with an id before the work is done, so a retry is a read, not a second run (§6.2)."""
    saved = save(client, workspace, auth).json()
    url = f"/api/v1/case-revisions/{saved['revision_id']}/compile"
    headers = {**auth, "Idempotency-Key": "compile-key"}
    body = {"use_ai": False, "force": False}
    first = client.post(url, json=body, headers=headers)
    assert first.status_code == 202, first.text
    assert first.json()["status"] == CompileStatus.PENDING.value
    before = counts(database)
    assert before["compiles"] == 1

    second = client.post(url, json=body, headers=headers)
    assert second.status_code == 202, second.text
    assert second.json() == first.json()
    assert counts(database) == before


def test_forcing_a_recompile_is_a_different_intent_than_the_compile_it_replays(client, database, workspace, auth):
    saved = save(client, workspace, auth).json()
    url = f"/api/v1/case-revisions/{saved['revision_id']}/compile"
    headers = {**auth, "Idempotency-Key": "force-key"}
    assert client.post(url, json={"use_ai": False, "force": False}, headers=headers).status_code == 202
    clash = client.post(url, json={"use_ai": False, "force": True}, headers=headers)
    assert clash.status_code == 409, clash.text
    assert clash.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    assert counts(database)["compiles"] == 1


def test_a_legacy_compile_record_replays_without_reserving_a_second_artifact(client, database, workspace, auth):
    saved = save(client, workspace, auth).json()
    revision_id = saved["revision_id"]
    remember_legacy(
        database,
        workspace,
        route=f"POST /case-revisions/{revision_id}/compile",
        key="compile-before",
        payload={"revision_id": revision_id, "use_ai": False, "force": False, "digest": saved["source_digest"]},
        answer={
            "compile_artifact_id": "artifact-from-the-old-code",
            "status": CompileStatus.PENDING.value,
            "revision_id": revision_id,
            "compiler_version": COMPILER_VERSION,
        },
    )
    replayed = client.post(
        f"/api/v1/case-revisions/{revision_id}/compile",
        json={"use_ai": False, "force": False},
        headers={**auth, "Idempotency-Key": "compile-before"},
    )
    assert replayed.status_code == 202, replayed.text
    assert replayed.json()["compile_artifact_id"] == "artifact-from-the-old-code"
    assert counts(database) == {
        "cases": 1,
        "revisions": 1,
        "compiles": 0,
        "executions": 0,
        "outbox": 1,
        "audit": 1,
        "records": 1,
    }


def test_a_run_key_replays_the_execution_it_created(client, database, workspace, auth, settings):
    """One key, one run: the queue entry answers for the run because they commit together (§9.3.5)."""
    saved = save(client, workspace, auth).json()
    compile_offline(database, workspace, settings, saved["revision_id"])
    body = {"case_id": saved["case_id"]}
    first = run(client, auth, body, key="run-key")
    assert first.status_code == 202, first.text
    assert first.json()["case_id"] == saved["case_id"]
    assert counts(database)["executions"] == 1

    stored = record(database, route="POST /executions", key="run-key")
    # A stream or download link minted for one caller must never be handed to the next one (§9.4).
    assert "report_url" not in stored.response["result"]

    before = counts(database)
    second = run(client, auth, body, key="run-key")
    assert second.status_code == 202, second.text
    assert second.json() == first.json()
    assert second.headers["location"] == first.headers["location"]
    assert counts(database) == before


def test_naming_a_stale_ir_digest_refuses_the_run_and_frees_the_key(client, database, workspace, auth, settings):
    """`expected_ir_digest` guards a review, and a refusal must not spend the key it arrived with (§9.2)."""
    saved = save(client, workspace, auth).json()
    artifact = compile_offline(database, workspace, settings, saved["revision_id"])
    refused = run(
        client,
        auth,
        {"case_id": saved["case_id"], "expected_ir_digest": "sha256:" + "0" * 64},
        key="guarded",
    )
    assert refused.status_code == 409, refused.text
    assert refused.json()["error"]["code"] == "COMPILE_STALE_DIGEST"
    assert counts(database)["executions"] == 0
    assert counts(database)["records"] == 0

    named = run(
        client, auth, {"case_id": saved["case_id"], "expected_ir_digest": artifact.ir_digest}, key="guarded"
    )
    assert named.status_code == 202, named.text
    assert named.json()["id"]
    assert counts(database)["records"] == 1


def test_a_legacy_run_record_replays_although_the_command_now_names_one_more_field(
    client, database, workspace, auth, settings
):
    """An old body could not name an IR digest, so the proof of equivalence drops the new field (§9.2.2)."""
    saved = save(client, workspace, auth).json()
    compile_offline(database, workspace, settings, saved["revision_id"])
    old_body = {
        "compile_artifact_id": None,
        "case_id": saved["case_id"],
        "revision_id": None,
        "environment_id": None,
        "environment_revision_id": None,
        "variables": {},
        "browser": None,
        "evidence_mode": None,
    }
    remember_legacy(
        database,
        workspace,
        route="POST /executions",
        key="run-before",
        payload=old_body,
        answer={
            "id": "run-from-the-old-code",
            "status": ExecutionStatus.QUEUED.value,
            "outcome": None,
            "project_id": workspace["project_id"],
            "case_id": saved["case_id"],
            "trigger": "manual",
        },
    )
    replayed = run(client, auth, old_body, key="run-before")
    assert replayed.status_code == 202, replayed.text
    assert replayed.json()["id"] == "run-from-the-old-code"
    assert replayed.json()["report_url"] == "/api/v1/executions/run-from-the-old-code/report"
    assert counts(database)["executions"] == 0


def test_a_cancel_key_replays_the_stop_it_already_asked_for(client, database, workspace, auth, settings):
    execution_id = _start_run(client, database, workspace, auth, settings)
    headers = {**auth, "Idempotency-Key": "cancel-key"}
    first = client.post(f"/api/v1/executions/{execution_id}/cancel", json={"reason": "smoke"}, headers=headers)
    assert first.status_code == 202, first.text
    assert (first.json()["status"], first.json()["outcome"]) == (
        ExecutionStatus.FINISHED.value,
        Outcome.CANCELLED.value,
    )

    stored = record(database, route=f"POST /executions/{execution_id}/cancel", key="cancel-key")
    # What was accepted is stored next to the id, because §9.4 promises a replay the first call's answer
    # rather than a re-derived one; REST ignores both and re-reads the run, so its body is unchanged.
    assert stored.response == {
        "format": INTERNAL_FORMAT,
        "result": {"id": execution_id, "cancel_requested": True},
    }

    before = counts(database)
    second = client.post(f"/api/v1/executions/{execution_id}/cancel", json={"reason": "smoke"}, headers=headers)
    assert second.status_code == 202, second.text
    # The answer is the run as it is now, not the snapshot the first caller was given (§9.4).
    assert second.json() == first.json()
    assert counts(database) == before


def test_a_cancel_with_a_different_reason_is_a_different_intent(client, database, workspace, auth, settings):
    execution_id = _start_run(client, database, workspace, auth, settings)
    headers = {**auth, "Idempotency-Key": "reason-key"}
    assert client.post(f"/api/v1/executions/{execution_id}/cancel", json={"reason": "first"}, headers=headers)
    clash = client.post(f"/api/v1/executions/{execution_id}/cancel", json={"reason": "second"}, headers=headers)
    assert clash.status_code == 409, clash.text
    assert clash.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"


# ----------------------------------------------------------- what a 200 promises


def test_an_edit_that_answers_200_has_actually_changed_the_row(client, database, workspace, auth):
    """A detached row accepts writes in silence, so only the read-back proves the edit landed (§13.1).

    This is the shape the metadata route had: `name`, `archived` and `tags` were assigned on an object
    a finished transaction had handed back, and the response echoed those values while the row kept its
    own. `expire_on_commit=False` makes that combination look like success from the outside.
    """
    case_id = save(client, workspace, auth).json()["case_id"]
    before = client.get(f"/api/v1/cases/{case_id}", headers=auth).json()["row_version"]

    assert client.patch(f"/api/v1/cases/{case_id}", json={"archived": True}, headers=auth).status_code == 200
    body = client.get(f"/api/v1/cases/{case_id}", headers=auth).json()
    assert body["archived"] is True
    assert body["row_version"] > before

    assert client.patch(f"/api/v1/cases/{case_id}", json={"name": "renamed"}, headers=auth).status_code == 200
    assert client.get(f"/api/v1/cases/{case_id}", headers=auth).json()["name"] == "renamed"


def test_a_project_edit_persists_and_the_version_it_read_no_longer_passes_if_match(
    client, database, workspace, admin_auth
):
    """Comparing `If-Match` against an object from a finished transaction guarded nothing (§13.1)."""
    url = f"/api/v1/projects/{workspace['project_id']}"
    tag = client.get(url, headers=admin_auth).headers["etag"]
    patched = client.patch(
        url, json={"display_name": "Renamed by this test"}, headers={**admin_auth, "If-Match": tag}
    )
    assert patched.status_code == 200, patched.text
    assert client.get(url, headers=admin_auth).json()["display_name"] == "Renamed by this test"

    stale = client.patch(url, json={"display_name": "Renamed twice"}, headers={**admin_auth, "If-Match": tag})
    assert stale.status_code == 409, stale.text
    assert stale.json()["error"]["code"] == "VERSION_CONFLICT"


# ------------------------------------------------------------------ digests


def test_the_digest_describes_the_intent_not_the_dictionary_order():
    a = atomic_request_digest({"name": "x", "tags": ["b", "a"], "title": None})
    b = atomic_request_digest({"title": None, "tags": ["b", "a"], "name": "x"})
    assert a == b
    assert a.startswith("sha256:v2:")
    assert len(a) == 74


def test_an_omitted_value_is_not_the_same_intent_as_an_explicit_null():
    """Otherwise "run the current case" could replay as "run this artifact" (§9.2)."""
    assert atomic_request_digest({"title": None}) != atomic_request_digest({})


def test_the_atomic_digest_differs_from_the_legacy_one_for_identical_input():
    payload = {"name": "x"}
    assert atomic_request_digest(payload) != request_digest(payload)


# ---------------------------------------------------------------- unit of work


def test_a_unit_of_work_is_one_transaction_only(database, session, workspace, settings):
    from backend.app.application.context import CallContext
    from backend.app.domain.rbac import Identity

    session.commit()  # an uncommitted seed writer would already be holding the write lock this test opens
    call = CallContext(
        identity=Identity(user_id="u", tenant_id=workspace["tenant_id"]), request_id="req", settings=settings
    )
    uow = UnitOfWork(database=database, call=call, write=True)
    with uow:
        assert uow.scope is uow.session
    with pytest.raises(RuntimeError, match="one transaction"), uow:
        pass


def test_a_failed_command_reports_a_lock_wait_as_something_to_retry():
    from sqlalchemy.exc import OperationalError

    busy = OperationalError("BEGIN IMMEDIATE", {}, Exception("database is locked"))
    assert _is_busy_wait(busy) is True
    assert _is_busy_wait(OperationalError("INSERT", {}, Exception("no such column: ir"))) is False


def test_the_advisory_lock_key_is_stable_across_processes():
    """A per-process string hash would give two workers two different locks for one command (§9.3.2)."""
    args = ("tenant", "actor", "POST /executions", "key")
    assert _lock_digest(*args) == _lock_digest(*args)
    assert _lock_digest(*args) != _lock_digest(*(*args[:3], "other-key"))
    assert -(1 << 63) <= _lock_digest(*args) < (1 << 63)
