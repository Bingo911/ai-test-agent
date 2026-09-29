"""Locator Engine (§8).

Five strategies in strict priority order — CSS, accessibility role, text, XPath, AI visual
fallback — and every candidate, explicit *or* remembered, is re-validated against the live page
before an action is allowed to use it.

The engine never guesses: `first()` is forbidden, an ambiguous match is recorded and skipped, and
two validated candidates that point at different elements are a conflict that fails the step rather
than silently preferring the higher-priority one.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from ..evidence.collector import EvidenceCollector
from ..ir.models import LocatorCandidate, Target
from ..observability import get_logger
from .contracts import (
    LocatorAttempt,
    LocatorResolution,
    MemoryCandidateSource,
    RunContext,
    VisionCandidate,
    VisionRequest,
    VisionResolver,
)

log = get_logger(__name__)

STRATEGY_ORDER: tuple[str, ...] = ("css", "role", "text", "xpath")
PROBE_ATTRIBUTE = "data-aita-probe"
VISION_ATTRIBUTE = "data-aita-vision"

#: Control types an `input`/`clear` step may write into.
EDITABLE_TYPES = {"input", "textarea", "select"}
CLICKABLE_TYPES = {"input", "button", "link", "checkbox", "radio", "select", "textarea", "file", "other", None}


class LocatorFailure(Exception):
    """Terminal locator outcome for a step: not found, ambiguous, or not interactable."""

    def __init__(
        self, kind: str, message: str, *, attempts: Sequence[LocatorAttempt], detail: dict[str, Any] | None = None
    ) -> None:
        self.kind = kind
        self.message = message
        self.attempts = list(attempts)
        self.detail = detail or {}
        super().__init__(message)


@dataclass(frozen=True)
class LocatorBudget:
    """One shared deadline for the whole step, sliced by §8.1 for the visual fallback."""

    step_ms: int
    deterministic_ms: int
    vision_ms: int
    reserve_action_ms: int

    @classmethod
    def for_step(
        cls,
        *,
        step_timeout_ms: int,
        vision_enabled: bool,
        vision_step_budget_ms: int = 20_000,
        deterministic_ms: int = 5_000,
        vision_call_ms: int = 10_000,
        reserve_action_ms: int = 5_000,
    ) -> LocatorBudget:
        if not vision_enabled:
            return cls(step_ms=step_timeout_ms, deterministic_ms=step_timeout_ms, vision_ms=0, reserve_action_ms=0)
        # An explicitly shorter step timeout is never quietly lengthened (§8.1).
        step_ms = (
            min(step_timeout_ms, vision_step_budget_ms) if step_timeout_ms >= vision_step_budget_ms else step_timeout_ms
        )
        deterministic = min(deterministic_ms, step_ms)
        reserve = min(reserve_action_ms, max(0, step_ms - deterministic))
        vision = min(vision_call_ms, max(0, step_ms - deterministic - reserve))
        return cls(step_ms=step_ms, deterministic_ms=deterministic, vision_ms=vision, reserve_action_ms=reserve)

    @property
    def vision_affordable(self) -> bool:
        return self.vision_ms > 0


def target_fingerprint(target: Target) -> str:
    """Stable identity of *what was asked for*, independent of how it was located (§8.3)."""
    payload = {
        "description": target.description,
        "type": target.type,
        "candidates": sorted(
            json.dumps(
                candidate.model_dump(exclude_none=True), ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
            for candidate in target.candidates
        ),
    }
    return (
        "sha256:" + hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
    )


@dataclass
class _Validated:
    candidate: LocatorCandidate | dict[str, Any]
    locator: Any
    strategy: str
    source: str
    probe: str
    signature: dict[str, Any]
    attempt: LocatorAttempt


@dataclass
class PlaywrightLocatorEngine:
    """Resolves an IR `Target` into exactly one actionable Playwright locator."""

    memory: MemoryCandidateSource | None = None
    vision: VisionResolver | None = None
    vision_allowed: bool = False
    browser_family: str = "chromium"

    async def resolve(
        self,
        page: Any,
        target: Target,
        *,
        action: str,
        collector: EvidenceCollector,
        context: RunContext,
        budget: LocatorBudget,
        deadline_monotonic_ms: float,
        step_id: str | None = None,
    ) -> LocatorResolution:
        attempts: list[LocatorAttempt] = []
        validated: list[_Validated] = []
        failures: list[tuple[str, dict[str, Any]]] = []
        deterministic_deadline = min(deadline_monotonic_ms, _now_ms() + budget.deterministic_ms)

        try:
            for strategy in STRATEGY_ORDER:
                if _now_ms() >= deterministic_deadline:
                    attempts.append(
                        LocatorAttempt(
                            strategy=strategy,
                            source="explicit",
                            outcome="skipped",
                            reason="deterministic_budget_exhausted",
                        )
                    )
                    continue
                for candidate, source in self._candidates_for(strategy, target, context):
                    outcome = await self._try_candidate(
                        page,
                        candidate,
                        strategy=strategy,
                        source=source,
                        action=action,
                        target=target,
                        attempts=attempts,
                        failures=failures,
                        probe_seed=f"{strategy}-{len(attempts)}",
                    )
                    if isinstance(outcome, _Validated):
                        validated.append(outcome)
                        break
                    if _now_ms() >= deterministic_deadline:
                        break
        except LocatorFailure:
            await self._cleanup_probes(page)
            self._flush_memory(context, target, winner=None, failures=failures)
            raise

        resolution: LocatorResolution | None = None
        if validated:
            resolution = await self._settle(page, validated, attempts)

        if resolution is None and self.vision is not None and self.vision_allowed and target.allow_vision:
            remaining = deadline_monotonic_ms - _now_ms()
            if not budget.vision_affordable or remaining <= budget.reserve_action_ms:
                attempts.append(
                    LocatorAttempt(
                        strategy="vision",
                        source="vision",
                        outcome="skipped",
                        reason="the remaining budget cannot cover a visual call plus the reserved action time",
                    )
                )
            elif not collector.vision_capture_allowed():
                attempts.append(
                    LocatorAttempt(
                        strategy="vision",
                        source="vision",
                        outcome="skipped",
                        reason="secret fields could not be masked, so no screenshot may leave the platform",
                    )
                )
            else:
                vision_deadline = min(deadline_monotonic_ms, _now_ms() + min(budget.vision_ms, remaining))
                resolution, vision_attempt = await self._resolve_with_vision(
                    page,
                    target,
                    action=action,
                    collector=collector,
                    context=context,
                    deadline_monotonic_ms=vision_deadline,
                    step_id=step_id,
                )
                attempts.append(vision_attempt)

        await self._cleanup_probes(page)
        if resolution is None:
            self._flush_memory(context, target, winner=None, failures=failures)
            raise self._failure(validated, attempts)
        resolution.attempts = attempts
        # A visual hit owns no reusable selector, so it earns no credit even though the step resolved.
        winner = next((item for item in validated if item.attempt.outcome == "resolved"), None)
        self._flush_memory(context, target, winner=winner, failures=failures)
        return resolution

    # ------------------------------------------------------------------ candidates

    def _candidates_for(self, strategy: str, target: Target, context: RunContext) -> list[tuple[Any, str]]:
        """Explicit candidates first, then approved memory for the same strategy (§8.1)."""
        explicit = [(candidate, "explicit") for candidate in target.candidates if candidate.strategy == strategy]
        remembered = [(candidate, "memory") for candidate in self._memory_candidates(strategy, target, context)]
        return explicit + remembered

    def _memory_candidates(self, strategy: str, target: Target, context: RunContext) -> list[dict[str, Any]]:
        source = self._memory_source(context)
        if source is None or not context.origin:
            return []
        try:
            pairs = source.candidates(
                origin=context.origin,
                route_pattern=context.route_pattern or "*",
                target_fingerprint=target_fingerprint(target),
            )
        except Exception as exc:  # pragma: no cover - memory is an optimisation, never a blocker
            log.warning("element memory read failed", extra={"context": {"error": str(exc)}})
            return []
        decoded: list[dict[str, Any]] = []
        for remembered_strategy, stored in pairs:
            if remembered_strategy != strategy:
                continue
            fields = decode_memory_selector(strategy, stored)
            if fields is not None:
                decoded.append(fields)
        return decoded

    # ------------------------------------------------------------------ validation

    async def _try_candidate(
        self,
        page: Any,
        candidate: LocatorCandidate | dict[str, Any],
        *,
        strategy: str,
        source: str,
        action: str,
        target: Target,
        attempts: list[LocatorAttempt],
        failures: list[tuple[str, dict[str, Any]]],
        probe_seed: str,
    ) -> _Validated | LocatorAttempt:
        field_values = _candidate_fields(candidate)
        attempt = LocatorAttempt(
            strategy=strategy,
            source=source,
            selector=field_values.get("selector"),
            role=field_values.get("role"),
            name=field_values.get("name"),
            text=field_values.get("text"),
        )
        started = time.monotonic()
        try:
            locator = _build_locator(page, strategy, field_values)
            matched = await locator.count()
        except Exception as exc:
            attempt.outcome = "rejected"
            attempt.reason = f"{type(exc).__name__}: {exc}"
            attempt.elapsed_ms = _elapsed(started)
            attempts.append(attempt)
            return attempt
        attempt.matched = int(matched)
        attempt.elapsed_ms = _elapsed(started)

        if matched == 0:
            attempt.outcome = "no_match"
            attempts.append(attempt)
            failures.append((strategy, field_values))
            return attempt
        if matched > 1:
            # Ambiguity is recorded and skipped: taking first() would be a guess (§8.1).
            attempt.outcome = "ambiguous"
            attempt.reason = f"{matched} elements matched"
            attempts.append(attempt)
            failures.append((strategy, field_values))
            return attempt

        try:
            element = locator.first
            if not await element.is_visible():
                attempt.outcome = "not_visible"
                attempts.append(attempt)
                failures.append((strategy, field_values))
                return attempt
            signature = await _describe(element)
            if signature.get("in_frame"):
                # V1 targets the main document only; say so instead of quietly clicking the wrong frame (§2.2).
                attempt.outcome = "rejected"
                attempt.reason = "the match is inside an iframe"
                attempts.append(attempt)
                raise LocatorFailure(
                    "unsupported_scope",
                    "the target resolved inside an iframe, which this version does not target",
                    attempts=attempts,
                )
            mismatch = _type_mismatch(signature, target, action)
            if mismatch:
                attempt.outcome = "wrong_type"
                attempt.reason = mismatch
                attempts.append(attempt)
                failures.append((strategy, field_values))
                return attempt
            probe = hashlib.sha1(
                f"{probe_seed}:{json.dumps(signature, sort_keys=True)}".encode(),
                usedforsecurity=False,
            ).hexdigest()[:16]
            await element.evaluate(
                "(el, [attribute, token]) => el.setAttribute(attribute, token)", [PROBE_ATTRIBUTE, probe]
            )
        except LocatorFailure:
            raise
        except Exception as exc:
            attempt.outcome = "rejected"
            attempt.reason = f"{type(exc).__name__}: {exc}"
            attempts.append(attempt)
            failures.append((strategy, field_values))
            return attempt

        attempt.outcome = "resolved"
        attempt.elapsed_ms = _elapsed(started)
        attempts.append(attempt)
        return _Validated(
            candidate=candidate,
            locator=locator,
            strategy=strategy,
            source=source,
            probe=probe,
            signature=signature,
            attempt=attempt,
        )

    async def _settle(
        self, page: Any, validated: list[_Validated], attempts: list[LocatorAttempt]
    ) -> LocatorResolution | None:
        """Return the winner, or None when two candidates conflict and the step must fail (§8.1)."""
        if len(validated) == 1:
            return self._resolution(validated[0], attempts)
        winner = validated[0]
        conflicts: list[_Validated] = []
        for other in validated[1:]:
            same = await other.locator.first.evaluate(
                "(el, [attribute, token]) => el.getAttribute(attribute) === token", [PROBE_ATTRIBUTE, winner.probe]
            )
            if same or _same_element(winner.signature, other.signature):
                continue
            conflicts.append(other)
        if conflicts:
            for conflicting in conflicts:
                conflicting.attempt.outcome = "rejected"
                conflicting.attempt.reason = "conflicts with a higher-priority validated candidate"
                conflicting.attempt.matched = 1
            winner.attempt.outcome = "ambiguous"
            winner.attempt.reason = (
                f"{len(conflicts) + 1} candidates resolved unique elements that disagree; "
                "add a more specific target instead of relying on strategy order"
            )
            return None
        return self._resolution(winner, attempts)

    def _resolution(self, winner: _Validated, attempts: list[LocatorAttempt]) -> LocatorResolution:
        box = winner.signature.get("rect") or None
        return LocatorResolution(
            strategy=winner.strategy,
            source=winner.source,
            signature=json.dumps(winner.signature, sort_keys=True, ensure_ascii=False),
            attempts=attempts,
            description=_describe_text(winner.signature),
            box=tuple(box) if isinstance(box, list) and len(box) == 4 else None,
            matched_text=winner.signature.get("text"),
            degraded=winner.source == "memory",
            locator=winner.locator,
        )

    def _memory_source(self, context: RunContext) -> MemoryCandidateSource | None:
        return self.memory or context.memory

    def _flush_memory(
        self,
        context: RunContext,
        target: Target,
        *,
        winner: _Validated | None,
        failures: list[tuple[str, dict[str, Any]]],
    ) -> None:
        """Count selector outcomes once, when the step settles (§8.3).

        Deferring the writes means a candidate that lost a conflict is charged as a failure of that
        candidate only, the winner is credited once per step, and no memory row is touched twice.
        """
        source = self._memory_source(context)
        if source is None or not context.origin:
            return
        fingerprint = target_fingerprint(target)
        origin = context.origin
        route = context.route_pattern or "*"
        try:
            if winner is not None:
                encoded = encode_memory_selector(winner.strategy, _candidate_fields(winner.candidate))
                if encoded:
                    source.note_success(
                        origin=origin,
                        route_pattern=route,
                        target_fingerprint=fingerprint,
                        strategy=winner.strategy,
                        selector=encoded,
                    )
            for strategy, fields in failures:
                encoded = encode_memory_selector(strategy, fields)
                if encoded:
                    source.note_failure(
                        origin=origin,
                        route_pattern=route,
                        target_fingerprint=fingerprint,
                        strategy=strategy,
                        selector=encoded,
                    )
        except Exception as exc:  # pragma: no cover - memory bookkeeping must never fail a step
            log.warning("element memory write failed", extra={"context": {"error": str(exc)}})

    # ---------------------------------------------------------------------- vision

    async def _resolve_with_vision(
        self,
        page: Any,
        target: Target,
        *,
        action: str,
        collector: EvidenceCollector,
        context: RunContext,
        deadline_monotonic_ms: float,
        step_id: str | None = None,
    ) -> tuple[LocatorResolution | None, LocatorAttempt]:
        attempt = LocatorAttempt(strategy="vision", source="vision")
        started = time.monotonic()
        version_before = await page_version(page)
        await collector.screenshot(page, name="vision-viewport", step_id=step_id, sensitive=True)
        data = collector.last_screenshot_bytes
        if data is None:
            attempt.outcome = "skipped"
            attempt.reason = "no viewport screenshot available for the visual fallback"
            attempt.elapsed_ms = _elapsed(started)
            return None, attempt
        request = VisionRequest(
            target=target,
            action=action,
            screenshot=data,
            viewport=(int(collector.viewport[0]), int(collector.viewport[1])) if collector.viewport else (0, 0),
            page_url=str(page.url),
            page_version_digest=version_before,
            deadline_ms=int(max(0.0, deadline_monotonic_ms - _now_ms())),
        )
        try:
            candidate = await self.vision.resolve(request)  # type: ignore[union-attr]
        except Exception as exc:
            attempt.outcome = "rejected"
            attempt.reason = f"vision call failed: {type(exc).__name__}: {exc}"
            attempt.elapsed_ms = _elapsed(started)
            return None, attempt
        if candidate is None:
            attempt.outcome = "no_match"
            attempt.reason = "the model returned no candidate box"
            attempt.elapsed_ms = _elapsed(started)
            return None, attempt
        resolution, reason = await self._validate_vision_candidate(
            page, candidate, target, action=action, version_before=version_before
        )
        attempt.elapsed_ms = _elapsed(started)
        if resolution is None:
            attempt.outcome = "rejected"
            attempt.reason = reason
            return None, attempt
        attempt.outcome = "resolved"
        attempt.matched = 1
        attempt.reason = f"model={candidate.model}"
        return resolution, attempt

    async def _validate_vision_candidate(
        self, page: Any, candidate: VisionCandidate, target: Target, *, action: str, version_before: str
    ) -> tuple[LocatorResolution | None, str | None]:
        """Map the model's box onto a real DOM node and verify it before acting (§8.2)."""
        version_after = await page_version(page)
        if version_after != version_before:
            return None, "the page changed between the screenshot and the candidate mapping"
        token = hashlib.sha1(f"vision:{time.monotonic_ns()}".encode(), usedforsecurity=False).hexdigest()[:16]
        mapped = await page.evaluate(_VISION_MAP_JS, [int(candidate.x), int(candidate.y), token, target.type or ""])
        if not isinstance(mapped, dict) or not mapped.get("ok"):
            return None, f"candidate box did not map to a usable element: {mapped}"
        signature = mapped.get("signature") or {}
        mismatch = _type_mismatch(signature, target, action)
        if mismatch:
            return None, f"mapped element failed validation: {mismatch}"
        if signature.get("tag") in {"canvas", "body", "html"}:
            return None, "the mapped element is too generic; V1 requests a human instead of clicking coordinates"
        box = signature.get("rect") or [0, 0, 0, 0]
        # The action runs against the tagged node, never against raw coordinates, so a DOM change
        # between mapping and clicking fails instead of hitting whatever moved into the box.
        return (
            LocatorResolution(
                strategy="vision",
                source="vision",
                signature=json.dumps({"attribute": VISION_ATTRIBUTE, "token": token, **signature}, sort_keys=True),
                attempts=[],
                description=_describe_text(signature),
                box=tuple(box),
                matched_text=signature.get("text"),
                degraded=True,
                locator=page.locator(f'[{VISION_ATTRIBUTE}="{token}"]'),
            ),
            None,
        )

    async def probe(
        self,
        page: Any,
        target: Target,
        *,
        deadline_monotonic_ms: float,
        expect_visible: bool,
    ) -> tuple[str, list[LocatorAttempt], LocatorResolution | None]:
        """Existence check for `element_visible` / `element_hidden` (§5.3).

        Visibility is the *assertion* here rather than a validation gate, so a hidden match counts as
        present-but-hidden instead of being rejected, and the first matching strategy is enough —
        an assertion is read-only, so there is no side effect to guard against a conflict.
        """
        attempts: list[LocatorAttempt] = []
        for strategy in STRATEGY_ORDER:
            for candidate in list(target.candidates):
                if candidate.strategy != strategy:
                    continue
                fields = _candidate_fields(candidate)
                attempt = LocatorAttempt(
                    strategy=strategy,
                    source="explicit",
                    selector=fields.get("selector"),
                    role=fields.get("role"),
                    name=fields.get("name"),
                    text=fields.get("text"),
                )
                started = time.monotonic()
                try:
                    locator = _build_locator(page, strategy, fields)
                    matched = int(await locator.count())
                except Exception as exc:
                    attempt.outcome = "rejected"
                    attempt.reason = f"{type(exc).__name__}: {exc}"
                    attempts.append(attempt)
                    if _now_ms() >= deadline_monotonic_ms:
                        return "absent", attempts, None
                    continue
                attempt.matched = matched
                attempt.elapsed_ms = _elapsed(started)
                if matched == 0:
                    attempt.outcome = "no_match"
                    attempts.append(attempt)
                    continue
                if matched > 1:
                    attempt.outcome = "ambiguous"
                    attempt.reason = f"{matched} elements matched"
                    attempts.append(attempt)
                    return "ambiguous", attempts, None
                signature = await _describe(locator.first)
                if signature.get("in_frame"):
                    attempt.outcome = "rejected"
                    attempt.reason = "the match lives inside an iframe, which V1 does not target"
                    attempts.append(attempt)
                    return "in_frame", attempts, None
                visible = bool(signature.get("visible"))
                attempt.outcome = "resolved" if visible == expect_visible else "no_match"
                attempt.reason = (
                    None if attempt.outcome == "resolved" else f"present but {'hidden' if not visible else 'visible'}"
                )
                attempts.append(attempt)
                if attempt.outcome != "resolved":
                    return ("present_visible" if visible else "present_hidden"), attempts, None
                return (
                    ("present_visible" if visible else "present_hidden"),
                    attempts,
                    LocatorResolution(
                        strategy=strategy,
                        source="explicit",
                        signature=json.dumps(signature, sort_keys=True, ensure_ascii=False),
                        attempts=attempts,
                        description=_describe_text(signature),
                        matched_text=signature.get("text"),
                        locator=locator,
                    ),
                )
        return "absent", attempts, None

    async def read_text(self, resolution: LocatorResolution) -> str:
        """Inner text of a resolved element, used by `text_equals` (§5.3)."""
        return str(await resolution.locator.first.evaluate("el => (el.innerText ?? el.textContent ?? '')"))

    async def read_value(self, resolution: LocatorResolution) -> str:
        """Control value used by `value_equals`; the caller must never log it (§7.2)."""
        return str(await resolution.locator.first.evaluate("el => (el.value ?? '')"))

    async def _cleanup_probes(self, page: Any) -> None:
        """Remove identity probes; the winning vision marker is deliberately left for the action."""
        # pragma: no cover - the page may already be gone; probes never affect the verdict
        with contextlib.suppress(Exception):
            await page.evaluate(
                "(attribute) => document.querySelectorAll(`[${attribute}]`)"
                ".forEach((node) => node.removeAttribute(attribute))",
                PROBE_ATTRIBUTE,
            )

    def _failure(self, validated: list[_Validated], attempts: list[LocatorAttempt]) -> LocatorFailure:
        ambiguous = [attempt for attempt in attempts if attempt.outcome == "ambiguous"]
        if ambiguous and not validated:
            return LocatorFailure(
                "locator_ambiguous",
                "the target matched several elements and none could be verified as the only one",
                attempts=attempts,
                detail={"ambiguous": [attempt.strategy for attempt in ambiguous]},
            )
        return LocatorFailure(
            "locator_not_found",
            "no candidate resolved a single visible, correctly-typed element",
            attempts=attempts,
            detail={"strategies_tried": sorted({attempt.strategy for attempt in attempts})},
        )


_VISION_MAP_JS = """
([x, y, token, expectedType]) => {
  const el = document.elementFromPoint(x, y);
  if (!el) return { ok: false, reason: 'no element at the candidate point' };
  if (el === document.body || el === document.documentElement)
    return { ok: false, reason: 'candidate resolved to the page itself' };
  const rect = el.getBoundingClientRect();
  if (rect.width <= 0 || rect.height <= 0) return { ok: false, reason: 'mapped element is not visible' };
  const hit = document.elementsFromPoint(x, y);
  if (hit.length && hit[0] !== el) return { ok: false, reason: 'mapped element is covered by another element' };
  el.setAttribute('data-aita-vision', token);
  return { ok: true, signature: {
    tag: el.tagName.toLowerCase(),
    type: el.getAttribute('type'),
    role: el.getAttribute('role'),
    text: (el.innerText || el.value || '').slice(0, 120),
    rect: [Math.round(rect.x), Math.round(rect.y), Math.round(rect.width), Math.round(rect.height)],
    disabled: el.disabled === true,
    test_id: el.getAttribute('data-testid') || el.id || null,
  } };
}
"""


async def page_version(page: Any) -> str:
    """Cheap fingerprint used to detect a navigation between screenshot and action (§8.2)."""
    try:
        digest = await page.evaluate(
            "() => [location.href, document.readyState, document.querySelectorAll('*').length,"
            " (document.body ? document.body.innerText.length : 0)].join('|')"
        )
    except Exception:
        return "unavailable"
    return "sha256:" + hashlib.sha256(str(digest).encode("utf-8")).hexdigest()


def encode_memory_selector(strategy: str, fields: dict[str, Any]) -> str:
    """One string per remembered candidate: CSS/XPath stay raw, the rest need their structure."""
    if strategy in {"css", "xpath"}:
        return str(fields.get("selector") or "")
    payload = {"strategy": strategy}
    for key in ("role", "name", "text", "exact"):
        if fields.get(key) is not None:
            payload[key] = fields[key]
    return json.dumps(payload, ensure_ascii=False, sort_keys=True) if len(payload) > 1 else ""


def decode_memory_selector(strategy: str, stored: str) -> dict[str, Any] | None:
    try:
        parsed = json.loads(stored)
    except (TypeError, ValueError):
        parsed = None
    if isinstance(parsed, dict) and parsed.get("strategy") == strategy:
        return dict(parsed)
    return {"strategy": strategy, "selector": stored, "role": None, "name": None, "text": None, "exact": None}


def _candidate_fields(candidate: Any) -> dict[str, Any]:
    if isinstance(candidate, LocatorCandidate):
        return candidate.model_dump(exclude_none=True)
    return {
        "strategy": candidate.get("strategy"),
        "selector": candidate.get("selector"),
        "role": candidate.get("role"),
        "name": candidate.get("name"),
        "text": candidate.get("text"),
        "exact": candidate.get("exact"),
    }


def _build_locator(page: Any, strategy: str, fields: dict[str, Any]) -> Any:
    if strategy == "css":
        return page.locator(str(fields["selector"]))
    if strategy == "xpath":
        selector = str(fields["selector"])
        return page.locator(selector if selector.startswith("xpath=") else f"xpath={selector}")
    if strategy == "role":
        exact = fields.get("exact")
        options: dict[str, Any] = {"name": str(fields["name"])}
        if exact is not None:
            options["exact"] = bool(exact)
        return page.get_by_role(str(fields["role"]), **options)
    if strategy == "text":
        options = {}
        if fields.get("exact") is not None:
            options["exact"] = bool(fields["exact"])
        return page.get_by_text(str(fields["text"]), **options)
    raise ValueError(f"unsupported locator strategy '{strategy}'")


async def _describe(element: Any) -> dict[str, Any]:
    return await element.evaluate(
        """el => {
        const rect = el.getBoundingClientRect();
        return {
          tag: el.tagName.toLowerCase(),
          type: el.getAttribute('type'),
          role: el.getAttribute('role'),
          aria: el.getAttribute('aria-label'),
          text: (el.innerText || el.value || '').slice(0, 120),
          rect: [Math.round(rect.x), Math.round(rect.y), Math.round(rect.width), Math.round(rect.height)],
          disabled: el.disabled === true,
          visible: !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length),
          editable: typeof el.disabled === 'boolean' ? el.disabled === false : true,
          in_frame: el.ownerDocument !== document,
          test_id: el.getAttribute('data-testid') || el.id || null,
        };
      }"""
    )


def _same_element(left: dict[str, Any], right: dict[str, Any]) -> bool:
    """Whether two validated candidates describe the one rendered box.

    A page that re-renders between two validations replaces the node, so the probe token written by
    the first candidate is gone from the second even though both point at the same control. Calling
    that an ambiguity would fail a step the author wrote unambiguously. Genuine ambiguity still
    fails: two different controls have a different tag, different text or a non-overlapping box.
    """
    if str(left.get("tag") or "") != str(right.get("tag") or ""):
        return False
    if str(left.get("text") or "") != str(right.get("text") or ""):
        return False
    if left.get("role") != right.get("role") or left.get("aria") != right.get("aria"):
        return False
    overlap = _rect_overlap(left.get("rect"), right.get("rect"))
    return overlap is not None and overlap >= 0.9


def _rect_overlap(left: Any, right: Any) -> float | None:
    """The fraction of the smaller rectangle that the two share."""
    if not (isinstance(left, list) and isinstance(right, list) and len(left) == 4 and len(right) == 4):
        return None
    try:
        ax, ay, aw, ah = (int(float(value)) for value in left)
        bx, by, bw, bh = (int(float(value)) for value in right)
    except (TypeError, ValueError):
        return None
    width = min(ax + aw, bx + bw) - max(ax, bx)
    height = min(ay + ah, by + bh) - max(ay, by)
    if width <= 0 or height <= 0:
        return 0.0
    smaller = max(1, min(aw * ah, bw * bh))
    return (width * height) / smaller


def _type_mismatch(signature: dict[str, Any], target: Target, action: str) -> str | None:
    """Control-type and actionability check shared by every strategy (§8.1)."""
    tag = str(signature.get("tag") or "")
    input_type = (signature.get("type") or "").lower()
    role = (signature.get("role") or "").lower()
    observed = _control_type(tag, input_type, role)
    if target.type and target.type != observed and not (target.type == "text" and observed == "other"):
        return f"target declared '{target.type}' but the element looks like '{observed}'"
    if action == "probe":
        # Assertions read the element instead of acting on it, so disabled/editable gates do not apply.
        return None
    if signature.get("disabled"):
        return "the element is disabled"
    if action in {"input", "clear"} and observed not in EDITABLE_TYPES:
        return f"'{action}' needs an editable control, the element is '{observed}'"
    if action in {"input", "clear", "upload"} and signature.get("editable") is False:
        return f"'{action}' needs an enabled control"
    if action == "upload" and not (tag == "input" and input_type == "file"):
        return "'upload' needs an <input type=file>"
    if action == "click" and observed not in CLICKABLE_TYPES:
        return f"'click' is not supported on '{observed}'"
    return None


def _control_type(tag: str, input_type: str, role: str) -> str:
    if role:
        return {
            "button": "button",
            "link": "link",
            "textbox": "input",
            "searchbox": "input",
            "checkbox": "checkbox",
            "radio": "radio",
            "combobox": "select",
        }.get(role, "other")
    if tag == "a":
        return "link"
    if tag == "button":
        return "button"
    if tag == "textarea":
        return "textarea"
    if tag == "select":
        return "select"
    if tag == "input":
        if input_type in {"checkbox", "radio", "file"}:
            return input_type
        if input_type in {"submit", "button", "reset", "image"}:
            return "button"
        return "input"
    return "other"


def _describe_text(signature: dict[str, Any]) -> str:
    parts = [str(signature.get("tag") or "?")]
    if signature.get("role"):
        parts.append(f"role={signature['role']}")
    if signature.get("aria"):
        parts.append(f"aria={signature['aria']}")
    if signature.get("text"):
        parts.append(f"text={str(signature['text'])[:40]!r}")
    return " ".join(parts)


def _now_ms() -> float:
    return time.monotonic() * 1000


def _elapsed(started_monotonic: float) -> int:
    """Milliseconds since a `time.monotonic()` reading, which is in seconds."""
    return max(0, int((time.monotonic() - started_monotonic) * 1000))
