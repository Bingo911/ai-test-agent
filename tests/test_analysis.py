"""Who gets analysed, and what the rule pass says about them (§12.3).

The model is optional here on purpose: every assertion below is what an operator still sees when the
provider is down, which is the case this host is actually in.
"""

from __future__ import annotations

import pytest
from backend.app.analysis.classifier import classify, evidence_refs_for, failing_step
from backend.app.domain.enums import Outcome, StepStatus
from backend.app.orchestrator.finalize import ANALYSABLE_OUTCOMES


def test_a_run_the_platform_cut_short_is_still_diagnosed():
    """TIMEOUT has no test verdict, but "the platform ended it" is a reportable answer (§12.4)."""
    for code in ("QUEUE_TIMEOUT", "ACTIVE_TIMEOUT", "SESSION_LOST", "LEASE_LOST"):
        assert classify(code)["failure_type"] == "SESSION_LOST", code
    assert classify("HUMAN_WAIT_TIMEOUT")["failure_type"] == "HUMAN_TIMEOUT"
    assert classify("HUMAN_WAIT_TIMEOUT")["confidence"] > 0.5


def test_an_unrecognised_code_stays_unknown_and_says_so():
    rules = classify("SOMETHING_NEW")
    assert rules["failure_type"] == "UNKNOWN"
    assert rules["confidence"] <= 0.3
    assert rules["is_hypothesis"] is False
    assert rules["evidence_refs"] == []


def test_only_failures_are_worth_an_asynchronous_analysis_pass():
    assert {Outcome.FAILED.value, Outcome.ERROR.value, Outcome.TIMED_OUT.value} == set(ANALYSABLE_OUTCOMES)
    assert Outcome.PASSED.value not in ANALYSABLE_OUTCOMES
    assert Outcome.CANCELLED.value not in ANALYSABLE_OUTCOMES


def test_the_step_that_decided_the_outcome_is_the_one_explained():
    steps = [
        {"step_id": "s1", "action": "open", "status": StepStatus.PASSED.value, "artifact_ids": []},
        {"step_id": "s2", "action": "click", "status": StepStatus.FAILED.value, "artifact_ids": ["a1"]},
        {"step_id": "s3", "action": "assert", "status": StepStatus.SKIPPED.value, "artifact_ids": []},
    ]
    assert failing_step(steps)["step_id"] == "s2"
    assert evidence_refs_for(steps[1]) == ["artifact:a1"]


def test_a_cancelled_run_points_at_the_step_it_was_in():
    steps = [
        {"step_id": "s1", "action": "open", "status": StepStatus.PASSED.value, "artifact_ids": []},
        {"step_id": "s2", "action": "click", "status": StepStatus.CANCELLED.value, "artifact_ids": []},
    ]
    assert failing_step(steps)["step_id"] == "s2"


def test_locator_attempts_are_citable_evidence_of_this_step():
    step = {
        "step_id": "s3",
        "action": "click",
        "status": StepStatus.ERROR.value,
        "artifact_ids": ["a1", "a2"],
        "error_detail": {"locator_attempts": [{}, {}, {}, {}]},
    }
    refs = evidence_refs_for(step)
    assert refs[:2] == ["artifact:a1", "artifact:a2"]
    # the worker cites at most three attempts, however many the locator tried
    assert refs[2:] == ["step:s3:locator-attempt:1", "step:s3:locator-attempt:2", "step:s3:locator-attempt:3"]


@pytest.mark.parametrize("code", ["ASSERTION_FAILED", "LOCATOR_NOT_FOUND", "TARGET_HTTP_ERROR"])
def test_every_mapped_code_carries_an_actionable_suggestion(code: str):
    rules = classify(code)
    assert rules["failure_type"] != "UNKNOWN"
    assert len(rules["suggestion"]) > 20
