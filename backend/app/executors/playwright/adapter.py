"""Playwright implementation of the executor contract (§7.1).

`execute_step` follows the §7.3 order exactly: interrupt check, human gate, variable resolution,
locate, commit, verify, capture. Where the two are separable, a locator failure is reported *before*
any side effect is attempted.
"""

from __future__ import annotations

import contextlib
import inspect
import time
from pathlib import Path
from typing import Any

from ...domain.enums import StepStatus
from ...evidence.collector import EvidenceCollector
from ...ir.models import ACTION_NAMES, CONDITION_KINDS, Step
from ...observability import get_logger
from ..contracts import (
    CapabilitySet,
    EvidenceBundle,
    FailureKind,
    HumanRequired,
    RunContext,
    SessionConfig,
    StepExecutionError,
    StepInterrupted,
    StepResult,
)
from ..locator import LocatorBudget, PlaywrightLocatorEngine
from .actions import ActionContext, run_action
from .session import BrowserSession, PlaywrightSessionFactory

log = get_logger(__name__)

#: Runtime failure kind -> the stable error code the report and API expose (§7, §8, §9).
KIND_TO_ERROR: dict[str, str] = {
    "locator_not_found": "LOCATOR_NOT_FOUND",
    "locator_ambiguous": "LOCATOR_AMBIGUOUS",
    "not_interactable": "LOCATOR_NOT_INTERACTABLE",
    "assertion_failed": "ASSERTION_FAILED",
    "navigation_failed": "TARGET_HTTP_ERROR",
    "timeout": "ACTIVE_TIMEOUT",
    "cancelled": "CANCELLED",
    "lease_lost": "LEASE_LOST",
    "browser_error": "BROWSER_CRASHED",
    "outcome_unknown": "ACTION_OUTCOME_UNKNOWN",
    "evidence_error": "EVIDENCE_CAPTURE_FAILED",
    "unsupported_scope": "UNSUPPORTED_TARGET_SCOPE",
    "human_required": "HUMAN_WAIT_TIMEOUT",
}


class PlaywrightExecutor:
    """One executor object per worker process; sessions are per execution."""

    def __init__(self, settings: Any) -> None:
        self.settings = settings
        self.factory = PlaywrightSessionFactory(settings)

    def capabilities(self) -> CapabilitySet:
        return CapabilitySet(
            actions=ACTION_NAMES,
            conditions=CONDITION_KINDS,
            locator_strategies=("css", "role", "text", "xpath"),
            supports_upload=True,
            supports_vision=bool(getattr(self.settings, "ai_vision_enabled", False))
            and bool(getattr(self.settings, "ai_enabled", False)),
            supports_trace=True,
            supports_video=True,
            single_page_only=True,
            iframe_targets=False,
        )

    def new_collector(self, context: RunContext, config: SessionConfig) -> EvidenceCollector:
        settings = self.settings
        return EvidenceCollector(
            context.evidence,
            console_capacity=int(settings.console_ring_max),
            network_capacity=int(settings.network_ring_max),
            dom_max_bytes=int(settings.dom_evidence_max_bytes),
            disk_budget_bytes=int(settings.evidence_disk_budget_bytes),
            screenshot_policy=config_screenshot_policy(settings, config),
            full_page_allowed=not config.sensitive_evidence,
            mask_fields=True,
            known_secrets=tuple(item.value for item in context.values.values() if item.secret),
            viewport=config.viewport,
        )

    async def create_session(self, config: SessionConfig, context: RunContext) -> BrowserSession:
        collector = self.new_collector(context, config)
        scratch = Path(self.settings.scratch_dir) / context.execution_id
        scratch.mkdir(parents=True, exist_ok=True)
        return await self.factory.create(config, collector, scratch_dir=scratch)

    def locator_engine(self, context: RunContext, config: SessionConfig) -> PlaywrightLocatorEngine:
        return PlaywrightLocatorEngine(
            memory=context.memory,
            vision=context.vision,
            vision_allowed=bool(getattr(self.settings, "ai_vision_enabled", False)) and config.allow_vision,
            browser_family=config.browser_type,
        )

    def action_context(self, session: BrowserSession, context: RunContext) -> ActionContext:
        """The same context a real step gets, so a resume-condition check sees the identical world (§10.3)."""
        return ActionContext(
            page=session.page,
            engine=self.locator_engine(context, session.config),
            collector=session.collector,
            context=context,
            scratch_dir=session.scratch_dir,
            allowed_hosts=session.config.allowed_origins,
            wait_duration_max_ms=int(self.settings.wait_duration_max_ms),
        )

    async def execute_step(
        self,
        session: BrowserSession,
        step: Step,
        context: RunContext,
        *,
        deadline_ms: float,
        budget: LocatorBudget | None = None,
    ) -> StepResult:
        started = _now_ms()
        ac = self.action_context(session, context)
        locator_budget = budget or LocatorBudget.for_step(
            step_timeout_ms=int(max(1, deadline_ms - _now_ms())),
            vision_enabled=bool(
                ac.engine.vision_allowed and getattr(getattr(step, "target", None), "allow_vision", False)
            ),
        )
        try:
            # §7.3 puts the human gate before any locator attempt: a paused run must not touch the target.
            # The gate is awaited because a wait for a person must not block the loop that serves the
            # live page; frames for the operator travel over this same event loop (§10.2).
            if context.human_hook is not None:
                verdict = context.human_hook(step)
                if inspect.isawaitable(verdict):
                    await verdict
            detail = await run_action(step.action, step, ac, deadline_monotonic_ms=deadline_ms, budget=locator_budget)
        except StepInterrupted as interruption:
            return self._result(
                step,
                "CANCELLED" if interruption.kind == "cancelled" else "ERROR",
                interruption.kind,
                interruption.message,
                started,
            )
        except HumanRequired as gate:
            result = self._result(step, "WAIT_HUMAN", None, gate.reason, started)
            result.detail = {"human_mode": gate.mode, "resume_condition": gate.resume_condition}
            return result
        except StepExecutionError as failure:
            result = self._result(step, "FAILED", failure.kind, failure.message, started, detail=failure.detail)
            await self._attach_failure_evidence(session, step, result)
            return result
        except Exception as exc:  # an unexpected executor fault is an ERROR, never a pass
            log.warning("unexpected executor fault", extra={"context": {"step": step.id, "error": str(exc)}})
            return self._result(step, "ERROR", "browser_error", f"{type(exc).__name__}: {exc}", started)

        result = self._result(step, StepStatus.PASSED.value, None, None, started, detail=detail, locator=ac.resolution)
        produced = detail.get("artifact_id")
        if isinstance(produced, str):
            # A step whose whole purpose is a capture indexes that file against itself (§12.1).
            result.artifact_ids.append(produced)
        return result

    def _result(
        self,
        step: Step,
        status: str,
        kind: FailureKind | None,
        message: str | None,
        started_monotonic: float,
        *,
        detail: dict[str, Any] | None = None,
        locator: Any = None,
    ) -> StepResult:
        return StepResult(
            step_id=step.id,
            action=step.action,
            status=status,
            failure_kind=kind,
            message=None if message is None else str(message)[:2000],
            locator=locator,
            detail=dict(detail or {}),
            duration_ms=max(0, int(_now_ms() - started_monotonic)),
        )

    async def _attach_failure_evidence(self, session: BrowserSession, step: Step, result: StepResult) -> None:
        """Failure screenshots and DOM snapshots are auxiliary: their own failure is appended only (§12.1)."""
        collector = session.collector
        try:
            artifact_id = await collector.screenshot(session.page, name=f"failure-{step.id}", step_id=step.id)
            if artifact_id:
                result.artifact_ids.append(artifact_id)
        except Exception as exc:  # pragma: no cover - collector already swallows capture faults
            collector.note_capture_error(f"failure screenshot: {exc}")
        try:
            dom_id = await collector.dom_snapshot(session.page, step_id=step.id, name=f"failure-{step.id}")
            if dom_id:
                result.artifact_ids.append(dom_id)
        except Exception as exc:  # pragma: no cover
            collector.note_capture_error(f"failure dom: {exc}")
        attempts = result.detail.get("locator_attempts")
        if attempts:
            diagnostic = collector.put_failure_detail(
                step_id=step.id,
                error_code=KIND_TO_ERROR.get(result.failure_kind or "", "ASSERTION_FAILED"),
                message=result.message or "",
                locator_attempts=list(attempts),
            )
            if diagnostic:
                result.artifact_ids.append(diagnostic)
        if collector.bundle()["partial"]:
            result.detail["artifact_status"] = "PARTIAL"

    async def collect_evidence(self, session: BrowserSession, context: RunContext) -> EvidenceBundle:
        return await self._drain(session)

    async def close_session(self, session: BrowserSession, context: RunContext) -> EvidenceBundle:
        bundle = await self._drain(session)
        collector = session.collector
        if session.trace_started:
            trace_path = Path(session.scratch_dir) / "trace.zip"
            try:
                await session.context.tracing.stop(path=str(trace_path))
                if trace_path.is_file():
                    artifact_id = collector.put_file(
                        kind="trace",
                        name="trace.zip",
                        path=trace_path,
                        media_type="application/zip",
                        sensitive=True,
                        size_hint=trace_path.stat().st_size,
                    )
                    if artifact_id:
                        bundle.artifact_ids.append(artifact_id)
            except Exception as exc:
                collector.note_capture_error(f"trace stop: {exc}")
        if session.config.record_video:
            await self._capture_video(session, collector, bundle)
        try:
            await session.context.close()
        except Exception as exc:  # pragma: no cover - teardown is best effort
            collector.note_capture_error(f"context close: {exc}")
        with contextlib.suppress(Exception):  # pragma: no cover - teardown is best effort
            await session.browser.close()
        try:
            await session.playwright.stop()
        except Exception as exc:  # pragma: no cover
            collector.note_capture_error(f"playwright stop: {exc}")
        bundle.errors = list(collector.capture_errors)
        return bundle

    async def _capture_video(self, session: BrowserSession, collector: Any, bundle: EvidenceBundle) -> None:
        video = session.page.video
        if video is None:
            return
        try:
            # the recorder writes the file only once the page it belongs to has closed
            if not session.page.is_closed():
                await session.page.close()
            source = Path(await video.path())
        except Exception as exc:
            collector.note_capture_error(f"video collection: {exc}")
            return
        artifact_id = collector.put_file(
            kind="video",
            name="execution.webm",
            path=source,
            media_type="video/webm",
            sensitive=session.config.sensitive_evidence,
            size_hint=source.stat().st_size if source.exists() else 0,
        )
        if artifact_id:
            bundle.artifact_ids.append(artifact_id)

    async def _drain(self, session: BrowserSession) -> EvidenceBundle:
        collector = session.collector
        artifact_ids = collector.flush_console_and_network()
        return EvidenceBundle(
            console=collector.console.items(),
            network=collector.network.items(),
            dialogs=list(collector.dialogs),
            console_truncated=collector.console.truncated,
            network_truncated=collector.network.truncated,
            errors=list(collector.capture_errors),
            artifact_ids=[*collector.artifact_ids, *artifact_ids],
        )


def _now_ms() -> float:
    return time.monotonic() * 1000


def config_screenshot_policy(settings: Any, config: SessionConfig) -> str:
    """A secret-bearing run never captures automatically, whatever the tenant default says (§12.1)."""
    policy = str(getattr(settings, "screenshot_policy", "on_failure"))
    if config.sensitive_evidence and policy == "always":
        return "on_failure"
    return policy
