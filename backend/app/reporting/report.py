"""The run report (§12.2).

Built from database state and the evidence index only — it never opens a browser, and it never has to
wait for the analysis worker. The base part is ready as soon as the execution is FINISHED; the AI
section appears when a model answer has been recorded, which is the two-phase readiness the design asks
for. Nothing here decides a conclusion; it presents the one `finalize` already wrote.
"""

from __future__ import annotations

from typing import Any

from ..config import Settings, get_settings
from ..db.base import coerce_utc, utcnow
from ..db.models import Artifact, StepExecution, TestExecution
from ..domain.enums import AnalysisStatus, ArtifactKind, ArtifactStatus, Sensitivity, StepStatus
from ..repositories.artifacts import ArtifactRepository, FailureAnalysisRepository
from ..repositories.executions import ExecutionRepository
from ..repositories.human import CommandRepository, HumanTaskRepository

#: Evidence a reader may never see inline, whatever the report consumer (§12.1, §10.4).
RESTRICTED_KINDS = (ArtifactKind.TRACE.value, ArtifactKind.VIDEO.value)

LOCATOR_ATTEMPTS_IN_REPORT = 5


def build_report(
    session,
    execution: TestExecution,
    *,
    settings: Settings | None = None,
    tenant_id: str | None = None,
) -> dict[str, Any]:
    settings = settings or get_settings()
    tenant_id = tenant_id or execution.tenant_id
    steps = ExecutionRepository(session, tenant_id).steps(execution.id)
    artifacts = ArtifactRepository(session, tenant_id).for_execution(execution.id)
    tasks = HumanTaskRepository(session, tenant_id).for_execution(execution.id)
    analyses = FailureAnalysisRepository(session, tenant_id).for_execution(execution.id)
    by_step = _artifacts_by_step(artifacts)
    sensitive = execution.evidence_mode == Sensitivity.SENSITIVE.value

    return {
        "execution": _execution_section(execution, settings=settings),
        "case": _case_section(execution, session=session, tenant_id=tenant_id),
        "environment": _environment_section(execution),
        "steps": [_step_section(step, by_step.get(step.step_id, []), sensitive=sensitive) for step in steps],
        "evidence": _evidence_section(artifacts, execution=execution, sensitive=sensitive),
        "human": [_human_section(task, session=session, tenant_id=tenant_id) for task in tasks],
        "analysis": _analysis_section(analyses),
        "analysis_ready": _analysis_ready(analyses, execution=execution),
        "report_phase": "complete" if _analysis_ready(analyses, execution=execution) else "base",
        "generated_at": utcnow().isoformat(),
        "warnings": _warnings(execution, artifacts=artifacts, steps=steps, sensitive=sensitive),
    }


# --------------------------------------------------------------------------- sections


def _execution_section(execution: TestExecution, *, settings: Settings) -> dict[str, Any]:
    queued = coerce_utc(execution.queued_at)
    started = coerce_utc(execution.started_at)
    ended = coerce_utc(execution.ended_at)
    return {
        "id": execution.id,
        "status": execution.status,
        "outcome": execution.outcome,
        "error_code": execution.error_code,
        "error_detail": _scrub(execution.error_detail or {}, settings=settings),
        "trigger": execution.trigger,
        "requested_by": execution.requested_by,
        "retry_of": execution.retry_of_execution_id,
        "browser": execution.browser,
        "browser_version": execution.browser_version,
        "evidence_mode": execution.evidence_mode,
        "artifact_status": execution.artifact_status,
        "analysis_status": execution.analysis_status,
        "cleanup_status": execution.cleanup_status,
        "cancelled": execution.cancel_requested_at is not None,
        "queued_at": _iso(execution.queued_at),
        "started_at": _iso(execution.started_at),
        "ended_at": _iso(execution.ended_at),
        "durations": {
            "queued_ms": _between_ms(queued, started or ended),
            "active_ms": int(execution.active_ms or 0),
            "human_ms": int(execution.human_ms or 0),
            "total_ms": _between_ms(queued or started, ended),
        },
        "human_tasks_used": int(execution.human_tasks_used or 0),
    }


def _case_section(execution: TestExecution, *, session, tenant_id: str) -> dict[str, Any]:
    from ..repositories.cases import CompileRepository

    snapshot = dict(execution.snapshot or {})
    compile_artifact = CompileRepository(session, tenant_id).by_id(execution.compile_artifact_id)
    section = {
        "case_id": execution.case_id,
        "case_name": snapshot.get("case_name"),
        "revision_id": execution.revision_id,
        "revision_no": snapshot.get("revision_no"),
        "source_digest": snapshot.get("source_digest"),
        "ir_digest": execution.ir_digest,
        "ir_version": (execution.ir or {}).get("ir_version"),
        "step_count": len((execution.ir or {}).get("steps") or []),
        "compile": None,
    }
    if compile_artifact is not None:
        section["compile"] = {
            "artifact_id": compile_artifact.id,
            "status": compile_artifact.status,
            "compiler_mode": compile_artifact.compiler_mode,
            "compiler_version": compile_artifact.compiler_version,
            "model": getattr(compile_artifact, "model", None),
            "prompt_version": getattr(compile_artifact, "prompt_version", None),
            "usage": getattr(compile_artifact, "usage", None) or {},
        }
    return section


def _environment_section(execution: TestExecution) -> dict[str, Any]:
    """The frozen environment summary from the snapshot; live rows are not consulted (§3.2)."""
    snapshot = dict(execution.snapshot or {})
    config = dict(snapshot.get("environment_config") or {})
    return {
        "environment_id": snapshot.get("environment_id"),
        "environment_revision_id": snapshot.get("environment_revision_id"),
        "base_url": config.get("base_url"),
        "allowed_domains": list(config.get("allowed_domains") or []),
        "browsers": list(config.get("browsers") or []),
        "viewport": config.get("viewport") or {},
        "evidence": config.get("evidence") or {},
        "variables": sorted((config.get("variables") or {}).keys()),
        "secret_bindings": sorted((snapshot.get("secret_bindings") or {}).keys()),
        "run_variables": sorted((snapshot.get("run_variables") or {}).keys()),
        "evidence_mode": snapshot.get("evidence_mode"),
    }


def error_detail_view(detail: dict[str, Any] | None) -> dict[str, Any]:
    """The same scrubbing the report applies, for routes that show a failure without the report (§12.1)."""
    return _scrub(dict(detail or {}))


def steps_detail(session, execution: TestExecution, *, tenant_id: str | None = None) -> list[dict[str, Any]]:
    """Per-step rows for `GET /executions/{id}/steps`, redacted exactly as the report is (§12.1)."""
    tenant_id = tenant_id or execution.tenant_id
    steps = ExecutionRepository(session, tenant_id).steps(execution.id)
    artifacts = ArtifactRepository(session, tenant_id).for_execution(execution.id)
    by_step = _artifacts_by_step(artifacts)
    sensitive = execution.evidence_mode == Sensitivity.SENSITIVE.value
    return [_step_section(step, by_step.get(step.step_id, []), sensitive=sensitive) for step in steps]


def _step_section(step: StepExecution, artifacts: list[Artifact], *, sensitive: bool) -> dict[str, Any]:
    detail = dict(step.error_detail or {})
    return {
        "step_id": step.step_id,
        "step_no": step.step_no,
        "action": step.action,
        "description": step.description,
        "status": step.status,
        "duration_ms": int(step.duration_ms or 0),
        "locator_strategy": step.locator_strategy,
        "locator_attempts": [
            {
                key: (str(value)[:200] if key == "reason" else value)
                for key, value in dict(item).items()
                if key
                in ("strategy", "source", "selector", "description", "outcome", "matched", "reason", "elapsed_ms")
            }
            for item in list(step.locator_attempts or [])[:LOCATOR_ATTEMPTS_IN_REPORT]
        ],
        "error": None
        if not step.error_code
        else {
            "code": step.error_code,
            "detail": _scrub(detail, keep=("message", "expected", "actual", "http_status", "polls")),
        },
        "resume_phase": step.resume_phase,
        "passed_by_human": bool(detail.get("passed_by_human"))
        or (step.status == StepStatus.PASSED.value and bool(detail.get("human_completed"))),
        "artifacts": [_artifact_ref(row, sensitive=sensitive) for row in artifacts],
        "started_at": _iso(step.started_at),
        "ended_at": _iso(step.ended_at),
    }


def _evidence_section(artifacts: list[Artifact], *, execution: TestExecution, sensitive: bool) -> dict[str, Any]:
    counts: dict[str, int] = {}
    items: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    for row in artifacts:
        counts[row.kind] = counts.get(row.kind, 0) + 1
        ref = _artifact_ref(row, sensitive=sensitive)
        items.append(ref)
        if row.publish_allowed is False or row.upload_status not in ("READY",):
            missing.append(
                {
                    "ref": f"artifact:{row.id}",
                    "kind": row.kind,
                    "state": row.upload_status,
                    "publishable": bool(row.publish_allowed),
                }
            )
    excluded = sorted({row.kind for row in artifacts if row.publish_allowed is False})
    if sensitive:
        excluded = sorted(set(excluded) | set(RESTRICTED_KINDS))
    return {
        "status": execution.artifact_status,
        "total": len(artifacts),
        "by_kind": counts,
        "bytes": sum(int(row.size or 0) for row in artifacts),
        "items": items,
        "missing": missing,
        "privacy_excluded_kinds": excluded,
        "expired": [
            f"artifact:{row.id}"
            for row in artifacts
            if row.retention_until is not None and coerce_utc(row.retention_until) <= utcnow()
        ],
        "access": "authorized download endpoint per artifact",
        "budget_bytes": int(execution.snapshot.get("artifact_budget_bytes") or 0)
        if isinstance(execution.snapshot, dict)
        else 0,
    }


def _human_section(task, *, session, tenant_id: str) -> dict[str, Any]:
    commands = CommandRepository(session, tenant_id).for_task(task.id)
    created = coerce_utc(task.created_at)
    completed = coerce_utc(task.completed_at)
    return {
        "task_id": task.id,
        "step_id": task.step_id,
        "reason": task.reason,
        "detail": task.detail,
        "status": task.status,
        "assignee_id": task.assignee_id,
        "deadline": _iso(task.deadline),
        "window": {
            "started_at": _iso(task.created_at),
            "completed_at": _iso(task.completed_at),
            "waited_ms": _between_ms(created, completed) if completed else 0,
        },
        "resume": {
            "phase": task.resume_phase,
            "requested_at": _iso(task.resume_requested_at),
            "condition": bool(task.resume_condition),
            "verified": task.status == "COMPLETED"
            and (not task.resume_condition or task.resume_requested_at is not None),
        },
        "operator_actions": [
            {
                "command": row.command_type,
                "status": row.status,
                "by": row.requested_by,
                "at": _iso(row.processed_at or row.created_at),
            }
            for row in commands
        ],
        "outcome_note": task.outcome_note,
        # OTP codes and passwords never appear in a report: the operator's keystrokes are not stored (§10.5).
        "redaction": "operator input values are not recorded",
    }


def _analysis_section(analyses: list[Any]) -> dict[str, Any] | None:
    """Null until a row exists: `execution.analysis_status` is what carries the in-flight state."""
    if not analyses:
        return None
    latest = analyses[0]
    return {
        "status": latest.status,
        "revision": latest.revision,
        "failure_type": latest.failure_type,
        # A diagnosis never replaces the verdict; the report keeps both side by side (§12.3).
        "reason": latest.reason,
        "suggestion": latest.suggestion,
        "confidence": latest.confidence,
        "evidence_refs": list(latest.evidence_refs or []),
        "is_hypothesis": bool(latest.is_hypothesis),
        "source": latest.source,
        "model": latest.model,
        "prompt_version": latest.prompt_version,
        "error_code": latest.error_code,
        "history": [
            {
                "revision": row.revision,
                "source": row.source,
                "status": row.status,
                "failure_type": row.failure_type,
                "confidence": row.confidence,
                "is_hypothesis": bool(row.is_hypothesis),
            }
            for row in analyses
        ],
    }


def _analysis_ready(analyses: list[Any], *, execution: TestExecution) -> bool:
    """The second phase is only ready when an *answer* exists, not merely a failed attempt."""
    if execution.analysis_status == AnalysisStatus.NOT_REQUIRED.value:
        return True
    return any(row.status == AnalysisStatus.SUCCEEDED.value for row in analyses)


def _warnings(
    execution: TestExecution, *, artifacts: list[Artifact], steps: list[StepExecution], sensitive: bool
) -> list[str]:
    notes: list[str] = []
    if execution.artifact_status == ArtifactStatus.PARTIAL.value:
        notes.append("some evidence is missing or was truncated; the conclusion is unchanged (§12.1)")
    if execution.cleanup_status == "QUARANTINED":
        notes.append("the browser session could not be confirmed terminated; its slot is withheld")
    if sensitive:
        notes.append("sensitive evidence mode: trace, video and page content are withheld from this report")
    unverified = [
        step.step_id
        for step in steps
        if step.status == StepStatus.FAILED.value and step.error_code == "ACTION_OUTCOME_UNKNOWN"
    ]
    if unverified:
        notes.append(f"the outcome of step(s) {', '.join(unverified)} could not be verified after a takeover")
    truncated = [row for row in artifacts if (row.artifact_metadata or {}).get("truncated")]
    if truncated:
        notes.append(f"{len(truncated)} evidence file(s) were cut short by a size limit")
    return notes


def _artifact_ref(row: Artifact, *, sensitive: bool) -> dict[str, Any]:
    restricted = row.kind in RESTRICTED_KINDS or row.publish_allowed is False
    return {
        "ref": f"artifact:{row.id}",
        "kind": row.kind,
        "name": row.name,
        "step_id": row.step_id,
        "media_type": row.media_type,
        "size": int(row.size or 0),
        "sha256": row.sha256,
        "sensitivity": row.sensitivity,
        "upload_status": row.upload_status,
        "available": row.upload_status == "READY" and bool(row.publish_allowed) and not sensitive,
        "requires_authorization": restricted,
        "expires_at": _iso(row.retention_until),
        "metadata": {
            key: value
            for key, value in dict(row.artifact_metadata or {}).items()
            if key in ("truncated", "gap", "duration_ms", "publish_revoked_reason", "masking")
        },
    }


def _artifacts_by_step(artifacts: list[Artifact]) -> dict[str, list[Artifact]]:
    grouped: dict[str, list[Artifact]] = {}
    for row in artifacts:
        if row.step_id:
            grouped.setdefault(row.step_id, []).append(row)
    return grouped


def _scrub(detail: dict[str, Any], *, settings: Settings | None = None, keep: tuple[str, ...] = ()) -> dict[str, Any]:
    """Drop anything that could carry a value from the page, keep the diagnostics (§12.1)."""
    allowed = keep or (
        "message",
        "code",
        "attempts",
        "lease_epoch",
        "worker",
        "reason",
        "queued_at",
        "error",
        "http_status",
    )
    out: dict[str, Any] = {}
    for key, value in detail.items():
        if key not in allowed:
            continue
        text = value if isinstance(value, (int, float, bool, list, dict)) or value is None else str(value)
        out[key] = text if not isinstance(text, str) else text[:500]
    return out


def _between_ms(start: Any, end: Any) -> int:
    if start is None or end is None:
        return 0
    return max(0, int((end - start).total_seconds() * 1000))


def _iso(value: Any) -> str | None:
    return value.isoformat() if hasattr(value, "isoformat") else None
