"""Deterministic failure classification (§12.3).

The rule pass runs first and always: it is what the report shows when the model is unavailable, and
it is the ceiling the model's answer is validated against. `failure_type` is a diagnosis, never a
replacement for `execution.outcome`.
"""

from __future__ import annotations

from typing import Any

from ..domain.enums import FailureType, StepStatus

#: Execution error codes -> the §12.3 diagnosis categories.
FAILURE_TYPE_FOR_CODE: dict[str, str] = {
    "ASSERTION_FAILED": FailureType.ASSERTION_FAILURE.value,
    "LOCATOR_NOT_FOUND": FailureType.LOCATOR_FAILURE.value,
    "LOCATOR_AMBIGUOUS": FailureType.LOCATOR_FAILURE.value,
    "LOCATOR_NOT_INTERACTABLE": FailureType.LOCATOR_FAILURE.value,
    "UNSUPPORTED_TARGET_SCOPE": FailureType.LOCATOR_FAILURE.value,
    "TARGET_HTTP_ERROR": FailureType.TARGET_NETWORK_ERROR.value,
    "EGRESS_BLOCKED": FailureType.NAVIGATION_FAILURE.value,
    "DOMAIN_NOT_ALLOWED": FailureType.NAVIGATION_FAILURE.value,
    "BROWSER_START_FAILED": FailureType.BROWSER_FAILURE.value,
    "BROWSER_CRASHED": FailureType.BROWSER_FAILURE.value,
    "STATE_STORE_UNAVAILABLE": FailureType.BROWSER_FAILURE.value,
    "SESSION_LOST": FailureType.SESSION_LOST.value,
    "LEASE_LOST": FailureType.SESSION_LOST.value,
    "ACTION_OUTCOME_UNKNOWN": FailureType.SESSION_LOST.value,
    # The platform ended the session on its own budget, so the run has no test verdict at all (§12.3).
    "QUEUE_TIMEOUT": FailureType.SESSION_LOST.value,
    "ACTIVE_TIMEOUT": FailureType.SESSION_LOST.value,
    "HUMAN_WAIT_TIMEOUT": FailureType.HUMAN_TIMEOUT.value,
    "HUMAN_SESSION_LOST": FailureType.HUMAN_TIMEOUT.value,
    "QUOTA_EXCEEDED": FailureType.HUMAN_TIMEOUT.value,
}

VALID_FAILURE_TYPES = frozenset(item.value for item in FailureType)

#: What a person can actually do about each category, without asking a model.
SUGGESTION_FOR_TYPE: dict[str, str] = {
    FailureType.ASSERTION_FAILURE.value: (
        "Check whether the expected content still matches the page, then update the assertion or confirm a real defect."
    ),
    FailureType.LOCATOR_FAILURE.value: (
        "Inspect the current page structure and refresh the target's locator candidates; "
        "a healed locator can be approved from the report."
    ),
    FailureType.NAVIGATION_FAILURE.value: (
        "Verify the URL, the environment's allowed domains and that the target is reachable from the worker network."
    ),
    FailureType.TARGET_NETWORK_ERROR.value: (
        "The target answered with an error status; check the service and its logs for this request window."
    ),
    FailureType.BROWSER_FAILURE.value: (
        "The browser could not run here; check the executor image, available memory and the recorded launch error."
    ),
    FailureType.SESSION_LOST.value: (
        "The worker lost its lease; re-run the case and check worker capacity if it repeats."
    ),
    FailureType.HUMAN_TIMEOUT.value: (
        "No operator finished the human task in time; schedule assistance or re-run when a controller is available."
    ),
    FailureType.UNKNOWN.value: (
        "No rule matched; use the collected evidence to decide whether this is a product or an infrastructure problem."
    ),
}


def classify(
    error_code: str | None, *, message: str | None = None, step: dict[str, Any] | None = None
) -> dict[str, Any]:
    """A contract-shaped analysis produced without a model."""
    failure_type = FAILURE_TYPE_FOR_CODE.get(str(error_code or ""), FailureType.UNKNOWN.value)
    reason = _reason(error_code, message, step)
    refs = evidence_refs_for(step) if step else []
    return {
        "failure_type": failure_type,
        "reason": reason,
        "suggestion": SUGGESTION_FOR_TYPE.get(failure_type, SUGGESTION_FOR_TYPE[FailureType.UNKNOWN.value]),
        "confidence": 0.95 if failure_type != FailureType.UNKNOWN.value else 0.2,
        "evidence_refs": refs,
        "is_hypothesis": False,
    }


def _reason(error_code: str | None, message: str | None, step: dict[str, Any] | None) -> str:
    detail = (step or {}).get("error_detail") or {}
    text = message or detail.get("message") or ""
    location = ""
    if step is not None:
        location = f" (step {step.get('step_id')} {step.get('action')})"
    if text:
        return f"{error_code}{location}: {str(text)[:400]}"
    return f"{error_code or 'UNKNOWN'}{location}".strip()


def evidence_refs_for(step: dict[str, Any] | None) -> list[str]:
    """References are opaque ids of *this* execution's evidence (§12.3)."""
    if step is None:
        return []
    refs = [f"artifact:{artifact_id}" for artifact_id in (step.get("artifact_ids") or [])]
    attempts = (step.get("error_detail") or {}).get("locator_attempts") or []
    for index in range(min(len(attempts), 3)):
        refs.append(f"step:{step.get('step_id')}:locator-attempt:{index + 1}")
    return refs


def failing_step(steps: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The first step that decided the outcome, which is the one worth explaining."""
    for row in steps:
        if row.get("status") in (StepStatus.FAILED.value, StepStatus.ERROR.value):
            return row
    for row in steps:
        if row.get("status") == StepStatus.CANCELLED.value:
            return row
    return steps[-1] if steps else None


def neighbour_summary(steps: list[dict[str, Any]], step_id: str | None) -> list[dict[str, Any]]:
    """Bounded context around the failure: what ran just before and after it."""
    index = next((position for position, row in enumerate(steps) if row.get("step_id") == step_id), None)
    if index is None:
        return []
    window = steps[max(0, index - 2) : index] + steps[index + 1 : index + 3]
    return [
        {
            "step_id": row.get("step_id"),
            "action": row.get("action"),
            "status": row.get("status"),
            "description": (row.get("description") or "")[:160],
            "error_code": row.get("error_code"),
        }
        for row in window
    ]
