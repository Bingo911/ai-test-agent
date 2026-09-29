"""The compile worker (§6.1, §6.2, §9.3).

Compilation is a queue task because it can call a model, and a model call must never hold up an HTTP
request or a browser slot. It is idempotent by `dedupe_key`, so a redelivered message re-runs nothing.
"""

from __future__ import annotations

from typing import Any

from ..ai.adapter import AiAdapter
from ..compiler.pipeline import compile_revision
from ..config import Settings, get_settings
from ..db.base import get_database
from ..domain.enums import CompileStatus
from ..domain.errors import ErrorCode
from ..executors.playwright.adapter import PlaywrightExecutor
from ..ir.models import COMPILER_VERSION
from ..observability import get_logger
from ..repositories.cases import CaseRepository, CompileRepository, compile_dedupe_key
from ..repositories.platform import AccessRepository
from ..repositories.resources import AttachmentRepository

log = get_logger(__name__)


class CompileWorker:
    def __init__(self, *, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.db = get_database()
        self.executor = PlaywrightExecutor(self.settings)

    def run(self, payload: dict[str, Any]) -> dict[str, Any]:
        tenant_id = str(payload["tenant_id"])
        project_id = str(payload["project_id"])
        revision_id = str(payload["revision_id"])
        force = bool(payload.get("force"))
        use_ai = bool(payload.get("use_ai", False))

        markdown, _source_digest, artifact_id, already_done = self._prepare(
            tenant_id, project_id, revision_id, force, use_ai
        )
        if already_done is not None:
            return already_done

        capabilities = set(self.executor.capabilities().actions)
        attachments, allow_vision = self._project_inputs(tenant_id, project_id, revision_id)
        adapter = AiAdapter(self.settings, purpose="compiler") if use_ai else None

        try:
            outcome = compile_revision(
                markdown,
                revision_id=revision_id,
                settings=self.settings,
                capabilities=capabilities,
                attachments=attachments,
                allow_vision=allow_vision,
                ai_adapter=adapter,
            )
        except Exception as exc:
            log.exception("compilation crashed", extra={"context": {"revision_id": revision_id, "error": str(exc)}})
            return self._record(
                tenant_id,
                artifact_id,
                status=CompileStatus.FAILED.value,
                ir=None,
                ir_digest=None,
                diagnostics=[
                    {
                        "code": ErrorCode.COMPILER_CAPABILITY_MISSING.value,
                        "message": str(exc)[:500],
                        "severity": "ERROR",
                    }
                ],
                review_items=[],
                usage={},
                model=None,
                prompt_version=self.settings.ai_prompt_version,
                compiler_mode="deterministic",
                error_code=ErrorCode.COMPILER_CAPABILITY_MISSING.value,
            )
        return self._record(
            tenant_id,
            artifact_id,
            status=outcome.status,
            ir=outcome.ir,
            ir_digest=outcome.ir_digest,
            diagnostics=outcome.diagnostics,
            review_items=outcome.review_items,
            usage=outcome.usage,
            model=outcome.model,
            prompt_version=outcome.prompt_version,
            compiler_mode=outcome.compiler_mode,
            error_code=None
            if outcome.status != CompileStatus.FAILED.value
            else _first_diagnostic_code(outcome.diagnostics),
        )

    # ------------------------------------------------------------------ staging

    def _prepare(
        self, tenant_id: str, project_id: str, revision_id: str, force: bool, use_ai: bool
    ) -> tuple[str, str, str, dict[str, Any] | None]:
        """Create (or reuse) the pending artifact; a finished one short-circuits the whole task.

        Reuse is keyed on the AI policy as well, so a deterministic request is never answered with an
        AI-assisted artifact and vice versa (§6.2).
        """
        with self.db.session(tenant_id) as session:
            revision = CaseRepository(session, tenant_id).require_revision(revision_id)
            dedupe = compile_dedupe_key(
                tenant_id,
                revision.id,
                revision.source_digest,
                compiler_version=COMPILER_VERSION,
                use_ai=use_ai,
            )
            compiles = CompileRepository(session, tenant_id)
            artifact = compiles.create_pending(
                project_id=project_id,
                revision_id=revision.id,
                source_digest_value=revision.source_digest,
                compiler_version=COMPILER_VERSION,
                dedupe_key=dedupe,
            )
            if (
                artifact.status
                in (CompileStatus.SUCCEEDED.value, CompileStatus.NEEDS_REVIEW.value, CompileStatus.FAILED.value)
                and not force
            ):
                session.commit()
                return (
                    revision.markdown,
                    revision.source_digest,
                    artifact.id,
                    {"artifact_id": artifact.id, "status": artifact.status, "reused": True},
                )
            session.commit()
            return revision.markdown, revision.source_digest, artifact.id, None

    def _project_inputs(self, tenant_id: str, project_id: str, revision_id: str) -> tuple[dict[str, str], bool]:
        with self.db.session(tenant_id) as session:
            ready = AttachmentRepository(session, tenant_id).ready_ids(project_id=project_id)
            project = AccessRepository(session, tenant_id).project(tenant_id, project_id)
            allow_vision = bool((project.settings or {}).get("allow_vision")) if project is not None else False
            return dict.fromkeys(sorted(ready), "CLEAN"), allow_vision

    def _record(self, tenant_id: str, artifact_id: str, **fields: Any) -> dict[str, Any]:
        with self.db.session(tenant_id) as session:
            compiles = CompileRepository(session, tenant_id)
            artifact = compiles.require(artifact_id)
            compiles.record_result(artifact, **fields)
            session.commit()
        return {
            "artifact_id": artifact_id,
            "status": fields["status"],
            "compiler_mode": fields.get("compiler_mode"),
            "ir_digest": fields.get("ir_digest"),
            "diagnostics": len(fields.get("diagnostics") or []),
            "review_items": len(fields.get("review_items") or []),
        }


def _first_diagnostic_code(diagnostics: list[dict[str, Any]]) -> str | None:
    for item in diagnostics:
        if str(item.get("severity", "ERROR")).upper() == "ERROR":
            return str(item.get("code") or ErrorCode.COMPILER_CAPABILITY_MISSING.value)
    return None


_worker: CompileWorker | None = None


def default_worker() -> CompileWorker:
    """The process's compile worker, rebound if the process-wide database was replaced."""
    global _worker
    if _worker is None or _worker.db is not get_database():
        _worker = CompileWorker()
    return _worker


def compile_task(payload: dict[str, Any]) -> dict[str, Any]:
    """Queue handler registered for `case.compile` (§9.3)."""
    return default_worker().run(payload)
