"""The compile command both adapters run, and what a compile answers (§6.2, §13.2).

Compiling is the one command that deliberately does not wait for its own result: the request reserves the
artifact row, queues the worker and returns the id, so a replay must answer with the *same* artifact rather
than start a second compilation of the same source. That is why the reservation is part of this
transaction and not a separate one.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ..domain.enums import CompileStatus, Permission
from ..ir.models import COMPILER_VERSION
from ..services.cases import compile_now
from . import authorization
from .idempotency import execute_atomic_command
from .unit_of_work import UnitOfWork


def compile_revision(
    uow: UnitOfWork,
    *,
    revision_id: str,
    use_ai: bool,
    force: bool,
    idempotency_key: str | None,
    gate: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """Queue a compile of one immutable revision and hand back the artifact that will hold the result."""
    call = uow.call
    session = uow.scope
    revision = authorization.revision(session, call, revision_id, permission=Permission.CASE_COMPILE)
    # A revision's source digest never changes once written, so naming it here describes the caller's
    # intent rather than a snapshot the command could otherwise answer from.
    command: dict[str, Any] = {
        "revision_id": revision_id,
        "use_ai": use_ai,
        "force": force,
        "digest": revision.source_digest,
    }

    def action() -> tuple[str, dict[str, Any]]:
        current = authorization.revision(session, call, revision_id, permission=Permission.CASE_COMPILE)
        artifact_id = compile_now(
            session,
            call.tenant_id,
            project_id=current.project_id,
            revision_id=current.id,
            created_by=call.actor_id,
            use_ai=use_ai,
            force=force,
            origin=call.entrypoint,
        )
        uow.audit(
            operation="compile.request",
            resource_type="compile_artifact",
            resource_id=artifact_id,
            project_id=current.project_id,
            detail={"revision_id": revision_id, "use_ai": use_ai, "force": force},
        )
        return artifact_id, {
            "compile_artifact_id": artifact_id,
            "status": CompileStatus.PENDING.value,
            "revision_id": revision_id,
            "compiler_version": COMPILER_VERSION,
        }

    return execute_atomic_command(
        uow,
        route=f"POST /case-revisions/{revision_id}/compile",
        key=idempotency_key,
        command=command,
        action=action,
        project_id=revision.project_id,
        permission=Permission.CASE_COMPILE,
        gate=gate,
        legacy_payload=command,
    )
