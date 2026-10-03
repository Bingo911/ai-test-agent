"""The run commands both adapters start and stop, with the inputs frozen at the moment of the call (§9.1).

Two rules shape this module. A command answers with what the caller named, never with what was current
when an earlier attempt ran - so "run this case" resolves to a revision inside the transaction and the
resolved ids are stored in the business result, not in the replay digest (§9.2). And a run is queued in
the same transaction as the reservation that answers for it, so a replay can never report a run whose
queue entry was committed by a different, possibly failed, transaction (§9.3.5).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from sqlalchemy.orm import Session

from ..db.models import TestExecution
from ..domain.enums import ExecutionStatus, Permission
from ..domain.errors import ApiError, ErrorCode
from ..repositories.cases import CaseRepository, CompileRepository
from ..services.executions import TRIGGER_MANUAL, create_execution, execution_result, request_cancellation
from . import authorization
from .context import CallContext
from .idempotency import execute_atomic_command
from .unit_of_work import UnitOfWork


def _target(
    session: Session,
    call: CallContext,
    *,
    compile_artifact_id: str | None,
    case_id: str | None,
    revision_id: str | None,
) -> tuple[str, str, str | None]:
    """(project, case, revision) from whichever ids the caller named, refusing anything it did not name."""
    if compile_artifact_id:
        artifact = CompileRepository(session, call.tenant_id).by_id(compile_artifact_id)
        if artifact is None:
            raise ApiError(ErrorCode.NOT_FOUND, "Compile artifact not found in your tenant")
        if revision_id and revision_id != artifact.revision_id:
            raise ApiError(
                ErrorCode.SEMANTIC_ERROR,
                "The named revision is not the one that artifact was compiled from",
                details={"revision_id": artifact.revision_id},
            )
        revision = CaseRepository(session, call.tenant_id).require_revision(artifact.revision_id)
        return str(artifact.project_id), str(revision.case_id), str(revision.id)
    if case_id:
        case = CaseRepository(session, call.tenant_id).by_id(case_id)
        if case is None:
            raise ApiError(ErrorCode.NOT_FOUND, "Case not found in your tenant")
        return str(case.project_id), str(case.id), revision_id or case.current_revision_id
    raise ApiError(
        ErrorCode.VALIDATION_ERROR,
        "Name a compile_artifact_id or a case_id to run",
        details={"allowed": ["compile_artifact_id", "case_id"]},
    )


def run_execution(
    uow: UnitOfWork,
    *,
    compile_artifact_id: str | None,
    case_id: str | None,
    revision_id: str | None,
    environment_id: str | None,
    environment_revision_id: str | None,
    variables: dict[str, Any] | None,
    browser: str | None,
    evidence_mode: str | None,
    expected_ir_digest: str | None,
    idempotency_key: str | None,
    use_server_ai: bool | None = None,
    ai_policy: Mapping[str, bool] | None = None,
    gate: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """Start one run of the inputs the caller froze, and answer with the run that was created.

    `use_server_ai` is a three-way input on purpose (§6.4): `None` means this entrypoint never named an AI
    intent, which is what a REST run has always meant, while `False` is an explicit instruction to keep the
    model out. Collapsing the two would either give an MCP run the AI it refused to ask for or take the AI
    away from a REST run that has always had it.
    """
    call = uow.call
    session = uow.scope
    named = {
        "compile_artifact_id": compile_artifact_id,
        "case_id": case_id,
        "revision_id": revision_id,
    }
    project_id, _, _ = _target(session, call, **named)
    authorization.project(session, call, project_id, permission=Permission.EXECUTION_RUN)

    command: dict[str, Any] = {
        "compile_artifact_id": compile_artifact_id,
        "case_id": case_id,
        "revision_id": revision_id,
        "environment_id": environment_id,
        "environment_revision_id": environment_revision_id,
        "variables": dict(variables or {}),
        "browser": browser,
        "evidence_mode": evidence_mode,
        "expected_ir_digest": expected_ir_digest,
    }
    if use_server_ai is not None:
        command["use_server_ai"] = bool(use_server_ai)

    def action() -> tuple[str, dict[str, Any]]:
        # Resolved again here, after the lock: the case may have taken a revision while this command
        # waited, and the run has to be of the revision that was current when it was allowed through.
        target_project, target_case, target_revision = _target(session, call, **named)
        execution = create_frozen_execution(
            uow,
            project_id=target_project,
            case_id=target_case,
            revision_id=target_revision,
            environment_id=environment_id,
            environment_revision_id=environment_revision_id,
            compile_artifact_id=compile_artifact_id,
            run_variables=variables,
            evidence_mode=evidence_mode,
            browser=browser,
            expected_ir_digest=expected_ir_digest,
            run_ai=_run_ai(call, use_server_ai=use_server_ai, ai_policy=ai_policy),
        )
        return execution.id, execution_result(execution)

    return execute_atomic_command(
        uow,
        route="POST /executions",
        key=idempotency_key,
        command=command,
        action=action,
        project_id=project_id,
        permission=Permission.EXECUTION_RUN,
        gate=gate,
        legacy_payload={key: value for key, value in command.items() if key != "expected_ir_digest"},
    )


def _run_ai(
    call: CallContext, *, use_server_ai: bool | None, ai_policy: Mapping[str, bool] | None
) -> dict[str, Any] | None:
    """What the worker is allowed to infer about this run's AI, frozen at the moment it was queued (§6.4).

    `origin` is the command's own answer, not a field the adapter hands over, because it is the reason the
    worker must re-read the policy at all: a job that came in through MCP was authorised by a project policy
    that may have tightened since this transaction committed.
    """
    if use_server_ai is None:
        return None
    return {
        "origin": call.entrypoint,
        "use_server_ai": bool(use_server_ai),
        "policy": dict(ai_policy or {}),
    }


def create_frozen_execution(
    uow: UnitOfWork,
    *,
    project_id: str,
    case_id: str,
    revision_id: str | None,
    environment_id: str | None,
    environment_revision_id: str | None,
    compile_artifact_id: str | None,
    run_variables: dict[str, Any] | None,
    evidence_mode: str | None,
    browser: str | None,
    expected_ir_digest: str | None,
    trigger: str = TRIGGER_MANUAL,
    run_ai: dict[str, Any] | None = None,
) -> TestExecution:
    """The core of a run request, in the caller's transaction; the audit row is part of it (§9.3.5)."""
    call = uow.call
    execution = create_execution(
        uow.scope,
        call.settings,
        call.tenant_id,
        project_id=project_id,
        case_id=case_id,
        revision_id=revision_id,
        environment_id=environment_id,
        environment_revision_id=environment_revision_id,
        compile_artifact_id=compile_artifact_id,
        requested_by=call.actor_id,
        run_variables=run_variables,
        evidence_mode=evidence_mode,
        browser=browser,
        trigger=trigger,
        expected_ir_digest=expected_ir_digest,
        run_ai=run_ai,
    )
    uow.audit(
        operation="execution.create",
        resource_type="execution",
        resource_id=execution.id,
        project_id=execution.project_id,
        detail={
            "case_id": execution.case_id,
            "revision_id": execution.revision_id,
            "compile_artifact_id": execution.compile_artifact_id,
            "environment_revision_id": execution.environment_revision_id,
            "browser": execution.browser,
            "evidence_mode": execution.evidence_mode,
            "variables": sorted((run_variables or {}).keys()),
        },
    )
    return execution


def cancel_execution(
    uow: UnitOfWork, *, execution_id: str, reason: str, idempotency_key: str | None
) -> dict[str, Any]:
    """Ask for a stop; the worker that holds the lease is the one that acts on it (§9.1).

    The stored answer keeps whether *that call* was accepted, because §9.4 promises a replay the original
    decision rather than today's: a run that finished on its own after a successful cancel must not have the
    cancellation rewritten out of the record, and one that was already finished must not gain a cancel that
    never happened.
    """
    call = uow.call
    session = uow.scope
    authorization.cancellable_execution(session, call, execution_id)
    command: dict[str, Any] = {"execution_id": execution_id, "reason": reason}

    def action() -> tuple[str, dict[str, Any]]:
        # Read again under the lock: `FINISHED` is the one state this command cannot act on, and the run may
        # have reached it while this call waited.
        live = authorization.cancellable_execution(session, call, execution_id)
        open_to_cancel = str(live.status) != ExecutionStatus.FINISHED.value
        stopped = request_cancellation(
            session, call.tenant_id, execution_id, requested_by=call.actor_id, reason=reason
        )
        uow.audit(
            operation="execution.cancel",
            resource_type="execution",
            resource_id=stopped.id,
            project_id=stopped.project_id,
            detail={"reason": reason[:300]},
        )
        return stopped.id, {"id": stopped.id, "cancel_requested": open_to_cancel}

    return execute_atomic_command(
        uow,
        route=f"POST /executions/{execution_id}/cancel",
        key=idempotency_key,
        command=command,
        action=action,
        recheck=lambda: authorization.cancellable_execution(session, call, execution_id),
        legacy_payload={"reason": reason},
    )
