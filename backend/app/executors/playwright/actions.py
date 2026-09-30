"""The eight IR actions and seven conditions, mapped onto Playwright (§7.2, §5.3).

Every handler is given one deadline shared with the locator phase — no strategy or retry gets a fresh
timeout — and every browser call carries an explicit timeout so a hung target cannot outlive the
step budget (§7.4).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ...ir.models import (
    AssertStep,
    ClearStep,
    ClickStep,
    Condition,
    InputStep,
    OpenStep,
    ScreenshotStep,
    Target,
    UploadStep,
    WaitStep,
)
from ..contracts import FailureKind, LocatorResolution, RunContext, StepExecutionError, StepInterrupted
from ..locator import LocatorBudget, PlaywrightLocatorEngine, page_version


def _now_ms() -> float:
    return time.monotonic() * 1000


@dataclass
class ActionContext:
    """Everything a handler touches for one step, injected rather than looked up (§7.1)."""

    page: Any
    engine: PlaywrightLocatorEngine
    collector: Any
    context: RunContext
    scratch_dir: Any
    allowed_hosts: tuple[str, ...] = ()
    relax_http_errors: bool = False
    wait_duration_max_ms: int = 10_000
    attachment_paths: dict[str, str] = field(default_factory=dict)
    #: The resolution the step's own locator attempt produced, for the §8.1 audit trail.
    resolution: Any = None

    def remaining_ms(self, deadline_monotonic_ms: float) -> float:
        return max(0.0, deadline_monotonic_ms - _now_ms())

    def check_interrupted(self, deadline_monotonic_ms: float) -> None:
        interruption = self.context.interruption
        if interruption.lease_lost:
            raise StepInterrupted("lease_lost", "this worker no longer holds the execution lease")
        if interruption.cancel:
            raise StepInterrupted("cancelled", "cancellation was requested for this execution")
        if self.remaining_ms(deadline_monotonic_ms) <= 0:
            raise StepInterrupted("timeout", "the step deadline elapsed")


async def run_action(
    action: str,
    step: Any,
    ac: ActionContext,
    *,
    deadline_monotonic_ms: float,
    budget: LocatorBudget,
) -> dict[str, Any]:
    """Dispatch to the registered handler; returns detail for the step record (§7.3 order)."""
    ac.check_interrupted(deadline_monotonic_ms)
    handler = HANDLERS.get(action)
    if handler is None:  # the compiler only emits these eight, so this is a build mismatch
        raise StepExecutionError("unsupported_scope", f"action '{action}' is not implemented by this executor")
    return await handler(step, ac, deadline_monotonic_ms=deadline_monotonic_ms, budget=budget)


# ----------------------------------------------------------------------- handlers


async def _open(
    step: OpenStep, ac: ActionContext, *, deadline_monotonic_ms: float, budget: LocatorBudget
) -> dict[str, Any]:
    url = ac.context.resolve(step.url).value
    ac.check_interrupted(deadline_monotonic_ms)
    timeout = int(ac.remaining_ms(deadline_monotonic_ms))
    if not url.lower().startswith(("http://", "https://")):
        raise StepExecutionError("navigation_failed", f"only http(s) navigations are supported, got '{url[:40]}'")
    try:
        response = await ac.page.goto(url, wait_until=step.wait_until, timeout=max(1, timeout))
    except StepInterrupted:
        raise
    except Exception as exc:
        raise StepExecutionError("navigation_failed", str(exc)) from exc
    status = None if response is None else int(response.status)
    detail: dict[str, Any] = {"requested_url": url[:200], "final_url": str(ac.page.url)[:200], "http_status": status}
    if status is not None and status >= 400 and not ac.relax_http_errors:
        # A 4xx/5xx in the main document is a failure unless the environment explicitly relaxes it (§7.2).
        raise StepExecutionError("navigation_failed", f"the target answered HTTP {status}", detail=detail)
    return detail


async def _resolve_target(
    step: Any, ac: ActionContext, target: Target, *, action: str, deadline_monotonic_ms: float, budget: LocatorBudget
) -> LocatorResolution:
    ac.context.interruption.detail.setdefault("locator_target", target.description)
    try:
        resolution = await ac.engine.resolve(
            ac.page,
            target,
            action=action,
            collector=ac.collector,
            context=ac.context,
            budget=budget,
            deadline_monotonic_ms=deadline_monotonic_ms,
            step_id=step.id,
        )
    except StepInterrupted:
        raise
    except Exception as exc:  # `LocatorFailure` and transport errors both surface as step failures
        kind = getattr(exc, "kind", None) or "browser_error"
        raise StepExecutionError(
            kind if kind in _LOCATOR_KINDS else "browser_error",
            str(exc),
            detail={"locator_attempts": [attempt.as_dict() for attempt in getattr(exc, "attempts", [])]},
        ) from exc
    ac.resolution = resolution
    return resolution


_LOCATOR_KINDS = {"locator_not_found", "locator_ambiguous", "not_interactable", "unsupported_scope"}


def _dispatch(ac: ActionContext, step: Any, state: str) -> None:
    hook: Callable[..., None] | None = ac.context.dispatch_hook
    if hook is not None:
        hook(step.id, state)


async def _click(
    step: ClickStep, ac: ActionContext, *, deadline_monotonic_ms: float, budget: LocatorBudget
) -> dict[str, Any]:
    resolution = await _resolve_target(
        step, ac, step.target, action="click", deadline_monotonic_ms=deadline_monotonic_ms, budget=budget
    )
    version_before = await page_version(ac.page)
    ac.check_interrupted(deadline_monotonic_ms)
    _dispatch(ac, step, "INTENT_RECORDED")
    try:
        await resolution.locator.first.click(timeout=int(ac.remaining_ms(deadline_monotonic_ms)))
    except Exception as exc:
        raise StepExecutionError("outcome_unknown", str(exc), detail={"click_committed": False}) from exc
    _dispatch(ac, step, "ACKNOWLEDGED")
    return {
        "locator_strategy": resolution.strategy,
        "locator_source": resolution.source,
        "page_changed": (await page_version(ac.page)) != version_before,
    }


async def _input(
    step: InputStep, ac: ActionContext, *, deadline_monotonic_ms: float, budget: LocatorBudget
) -> dict[str, Any]:
    value = ac.context.resolve(step.value)
    resolution = await _resolve_target(
        step, ac, step.target, action="input", deadline_monotonic_ms=deadline_monotonic_ms, budget=budget
    )
    ac.check_interrupted(deadline_monotonic_ms)
    _dispatch(ac, step, "INTENT_RECORDED")
    try:
        await resolution.locator.first.fill(value.value, timeout=int(ac.remaining_ms(deadline_monotonic_ms)))
    except Exception as exc:
        raise StepExecutionError("outcome_unknown", str(exc)) from exc
    _dispatch(ac, step, "ACKNOWLEDGED")
    written = str(await resolution.locator.first.evaluate("el => el.value ?? ''"))
    if written != value.value:
        # The control rejected or transformed the input; compare only, never echo (§7.2).
        raise StepExecutionError(
            "assertion_failed",
            "the control value does not match what was typed",
            detail={"expected_length": len(value.value), "actual_length": len(written), "secret": value.secret},
        )
    return {"locator_strategy": resolution.strategy, "typed_length": len(value.value), "secret": value.secret}


async def _clear(
    step: ClearStep, ac: ActionContext, *, deadline_monotonic_ms: float, budget: LocatorBudget
) -> dict[str, Any]:
    resolution = await _resolve_target(
        step, ac, step.target, action="clear", deadline_monotonic_ms=deadline_monotonic_ms, budget=budget
    )
    ac.check_interrupted(deadline_monotonic_ms)
    _dispatch(ac, step, "INTENT_RECORDED")
    try:
        await resolution.locator.first.fill("", timeout=int(ac.remaining_ms(deadline_monotonic_ms)))
    except Exception as exc:
        raise StepExecutionError("outcome_unknown", str(exc)) from exc
    _dispatch(ac, step, "ACKNOWLEDGED")
    if str(await resolution.locator.first.evaluate("el => el.value ?? ''")):
        raise StepExecutionError("assertion_failed", "the control is not empty after clearing")
    return {"locator_strategy": resolution.strategy}


async def _upload(
    step: UploadStep, ac: ActionContext, *, deadline_monotonic_ms: float, budget: LocatorBudget
) -> dict[str, Any]:
    paths: list[str] = []
    for attachment_id in step.attachment_ids:
        resolved = ac.attachment_paths.get(attachment_id) or ac.context.attachments.get(attachment_id)
        if not resolved:
            raise StepExecutionError(
                "unsupported_scope", f"attachment '{attachment_id}' is not available for this execution"
            )
        source = ac.scratch_dir / resolved if not str(resolved).startswith("/") else _absolute(resolved)
        # Only files the worker already materialised inside this execution's scratch directory, so a
        # forged attachment id cannot make the browser read arbitrary host paths (§14.3).
        try:
            real = source.resolve()
            real.relative_to(ac.scratch_dir.resolve())
        except (ValueError, OSError):
            raise StepExecutionError(
                "unsupported_scope", f"attachment '{attachment_id}' is outside the execution scratch directory"
            ) from None
        if not real.is_file():
            raise StepExecutionError("unsupported_scope", f"attachment '{attachment_id}' was not materialised in time")
        paths.append(str(real))

    resolution = await _resolve_target(
        step, ac, step.target, action="upload", deadline_monotonic_ms=deadline_monotonic_ms, budget=budget
    )
    ac.check_interrupted(deadline_monotonic_ms)
    _dispatch(ac, step, "INTENT_RECORDED")
    try:
        await resolution.locator.first.set_input_files(paths, timeout=int(ac.remaining_ms(deadline_monotonic_ms)))
    except Exception as exc:
        raise StepExecutionError("outcome_unknown", str(exc)) from exc
    _dispatch(ac, step, "ACKNOWLEDGED")
    accepted = int(await resolution.locator.first.evaluate("el => (el.files ? el.files.length : 0)"))
    if accepted != len(paths):
        raise StepExecutionError("assertion_failed", f"the file control accepted {accepted} of {len(paths)} files")
    return {"locator_strategy": resolution.strategy, "files": len(paths)}


async def _wait(
    step: WaitStep, ac: ActionContext, *, deadline_monotonic_ms: float, budget: LocatorBudget
) -> dict[str, Any]:
    if step.duration_ms is not None:
        duration = min(int(step.duration_ms), int(ac.wait_duration_max_ms))
        # Sleep in slices so cancel/lease signals land inside a fixed wait too (§7.4).
        slice_ms = 200.0
        waited = 0.0
        while waited < duration:
            ac.check_interrupted(deadline_monotonic_ms)
            chunk = min(slice_ms, duration - waited)
            await asyncio.sleep(chunk / 1000.0)
            waited += chunk
        return {"waited_ms": int(waited)}
    # WaitStep's model validator makes exactly one of duration_ms/condition required.
    assert step.condition is not None  # noqa: S101
    return await _await_condition(
        step, ac, step.condition, deadline_monotonic_ms=deadline_monotonic_ms, budget=budget, on_exhausted="timeout"
    )


async def _await_condition(
    step: Any,
    ac: ActionContext,
    condition: Condition,
    *,
    deadline_monotonic_ms: float,
    budget: LocatorBudget,
    on_exhausted: FailureKind,
) -> dict[str, Any]:
    """Bounded polling of a read-only condition inside the single step deadline (§7.4)."""
    polls = 0
    last: tuple[bool, str | None, LocatorResolution | None] = (False, "not evaluated", None)
    while True:
        try:
            ac.check_interrupted(deadline_monotonic_ms)
            last = await evaluate_condition(condition, ac, deadline_monotonic_ms=deadline_monotonic_ms, budget=budget)
        except StepInterrupted as interruption:
            if interruption.kind != "timeout" or polls == 0 or on_exhausted == "timeout":
                raise
            raise _condition_unsatisfied(
                condition, last, polls, on_exhausted, deadline_exhausted=True
            ) from interruption
        polls += 1
        satisfied, _reason, resolution = last
        if satisfied:
            return {
                "condition": condition.kind,
                "polls": polls,
                "locator_strategy": resolution.strategy if resolution else None,
                "locator_attempts": (resolution.attempt_payload() if resolution else []),
            }
        if ac.remaining_ms(deadline_monotonic_ms) <= 25:
            raise _condition_unsatisfied(condition, last, polls, on_exhausted, deadline_exhausted=True)
        await asyncio.sleep(0.05)


def _condition_unsatisfied(
    condition: Condition,
    last: tuple[bool, str | None, LocatorResolution | None],
    polls: int,
    on_exhausted: FailureKind,
    *,
    deadline_exhausted: bool,
) -> StepExecutionError:
    """`assert` reports an unmet condition as a failed assertion; `wait` reports it as a timeout (§5.3)."""
    _, reason, resolution = last
    verb = "was not reached in time" if on_exhausted == "timeout" else "was not satisfied"
    return StepExecutionError(
        on_exhausted,
        f"condition '{condition.kind}' {verb}: {reason}",
        detail={
            "condition": condition.kind,
            "polls": polls,
            "deadline_exhausted": deadline_exhausted,
            "locator_attempts": [attempt.as_dict() for attempt in (resolution.attempts if resolution else [])],
        },
    )


async def _assert(
    step: AssertStep, ac: ActionContext, *, deadline_monotonic_ms: float, budget: LocatorBudget
) -> dict[str, Any]:
    return await _await_condition(
        step,
        ac,
        step.condition,
        deadline_monotonic_ms=deadline_monotonic_ms,
        budget=budget,
        on_exhausted="assertion_failed",
    )


async def _screenshot(
    step: ScreenshotStep, ac: ActionContext, *, deadline_monotonic_ms: float, budget: LocatorBudget
) -> dict[str, Any]:
    ac.check_interrupted(deadline_monotonic_ms)
    name = step.name or f"step-{step.id}"
    artifact_id = await ac.collector.screenshot(
        ac.page,
        name=name,
        step_id=step.id,
        full_page=bool(step.full_page),
        sensitive=ac.context.evidence_mode.upper() == "SENSITIVE",
    )
    if not artifact_id:
        # The screenshot *is* the action here, so its failure fails the step (§7.2).
        raise StepExecutionError("evidence_error", f"the required screenshot '{name}' could not be captured")
    return {
        "artifact_id": artifact_id,
        "name": name,
        "full_page": bool(step.full_page and ac.collector.full_page_allowed),
    }


HANDLERS = {
    "open": _open,
    "click": _click,
    "input": _input,
    "clear": _clear,
    "upload": _upload,
    "wait": _wait,
    "assert": _assert,
    "screenshot": _screenshot,
}


# --------------------------------------------------------------------- conditions


async def evaluate_condition(
    condition: Condition, ac: ActionContext, *, deadline_monotonic_ms: float, budget: LocatorBudget
) -> tuple[bool, str | None, LocatorResolution | None]:
    """Deterministic condition check. Returns (satisfied, why-not, resolution-if-any) (§5.3)."""
    kind = condition.kind
    if kind == "page_contains":
        expected = _expected_text(condition, ac)
        body = ""
        try:
            body = await ac.page.locator("body").inner_text(
                timeout=int(min(5_000.0, ac.remaining_ms(deadline_monotonic_ms)))
            )
        except Exception as exc:
            return False, f"page text could not be read: {exc}", None
        return (expected in body), (None if expected in body else "the expected text is not on the page"), None
    if kind in {"url_equals", "url_contains"}:
        expected = _expected_text(condition, ac)
        current = str(ac.page.url)
        if kind == "url_equals":
            return (current == expected), f"current url is {current[:120]}", None
        return (expected in current), f"current url is {current[:120]}", None
    if kind in {"element_visible", "element_hidden"}:
        # Condition's model validator makes target required for these kinds.
        assert condition.target is not None  # noqa: S101
        outcome, _attempts, resolution = await ac.engine.probe(
            ac.page,
            condition.target,
            deadline_monotonic_ms=deadline_monotonic_ms,
            expect_visible=(kind == "element_visible"),
        )
        if outcome == "in_frame":
            raise StepExecutionError("unsupported_scope", "the condition target is inside an iframe")
        if outcome == "ambiguous":
            return False, "several elements matched the target", resolution
        if kind == "element_visible":
            return (outcome == "present_visible"), f"the target is {outcome}", resolution
        # `element_hidden` holds when there is nothing to see: absent, or present but not rendered (§5.3).
        return (outcome in {"absent", "present_hidden"}), f"the target is {outcome}", resolution
    if kind in {"text_equals", "value_equals"}:
        # Condition's model validator makes target required for these kinds.
        assert condition.target is not None  # noqa: S101
        resolution = await _resolve_target(
            _PseudoStep(condition.target),
            ac,
            condition.target,
            action="probe",
            deadline_monotonic_ms=deadline_monotonic_ms,
            budget=budget,
        )
        actual = (
            await ac.engine.read_value(resolution) if kind == "value_equals" else await ac.engine.read_text(resolution)
        )
        expected = _expected_text(condition, ac)
        compare = (lambda a, b: a.strip() == b.strip()) if kind == "text_equals" else (lambda a, b: a == b)
        satisfied = compare(actual, expected)
        if satisfied:
            return True, None, resolution
        if kind == "value_equals" and _looks_secret(condition):
            # Compare only: an equality failure must not put the secret into the report (§7.2).
            return False, "the control value does not match the expected secret", resolution
        return False, f"actual value was {actual[:160]!r}", resolution
    return False, f"condition '{kind}' is not implemented", None


class _PseudoStep:
    """Assertions read a target without a side effect, so they need no step id of their own."""

    def __init__(self, target: Target) -> None:
        self.target = target
        self.id = "condition"


def _expected_text(condition: Condition, ac: ActionContext) -> str:
    if condition.expected is None:
        raise StepExecutionError("assertion_failed", f"condition '{condition.kind}' needs an expected value")
    return ac.context.resolve(condition.expected).value


def _looks_secret(condition: Condition) -> bool:
    return bool(condition.expected is not None and condition.expected.kind in {"secret", "template"})


def _absolute(path: str) -> Any:
    from pathlib import Path

    return Path(path)
