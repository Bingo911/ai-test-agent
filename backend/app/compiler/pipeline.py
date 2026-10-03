"""Compile pipeline: Markdown revision -> validated Test IR artifact (§6.1)."""

from __future__ import annotations

import contextlib
import time
from dataclasses import dataclass, field
from typing import Any

from ..ai.adapter import AiAdapter
from ..config import Settings
from ..domain.enums import CompileStatus
from ..ir.models import COMPILER_VERSION, IR_VERSION, PROMPT_VERSION, TestIR
from ..ir.validation import ValidationContext, validate_ir
from .ai_compile import compile_step
from .diagnostics import Diagnostic, DiagnosticList, DslError
from .markdown_dsl import parse_markdown
from .normalize import normalize_step


@dataclass
class CompileOutcome:
    status: str
    ir: dict[str, Any] | None = None
    ir_digest: str | None = None
    diagnostics: list[dict[str, Any]] = field(default_factory=list)
    compiler_mode: str = "deterministic"
    model: str | None = None
    prompt_version: str = PROMPT_VERSION
    usage: dict[str, Any] = field(default_factory=dict)
    review_items: list[dict[str, Any]] = field(default_factory=list)
    dsl_version: str = IR_VERSION
    case_title: str = ""
    tags: list[str] = field(default_factory=list)
    declared_variables: dict[str, Any] = field(default_factory=dict)
    source_digest: str = ""
    duration_ms: int = 0

    @property
    def executable(self) -> bool:
        return self.status == CompileStatus.SUCCEEDED.value


def compile_revision(
    markdown: str,
    *,
    revision_id: str,
    settings: Settings,
    capabilities: set[str] | None = None,
    attachments: dict[str, str] | None = None,
    allow_vision: bool = False,
    ai_adapter: AiAdapter | None = None,
) -> CompileOutcome:
    started = time.monotonic()
    diagnostics = DiagnosticList()
    outcome = CompileOutcome(status=CompileStatus.FAILED.value, prompt_version=settings.ai_prompt_version)
    try:
        case, structure_diagnostics = parse_markdown(
            markdown,
            max_bytes=settings.max_case_bytes,
            max_steps=settings.max_case_steps,
            max_field_bytes=settings.max_field_bytes,
        )
    except DslError as exc:
        outcome.diagnostics = [exc.diagnostic.as_dict()]
        outcome.duration_ms = _elapsed(started)
        return outcome

    diagnostics.extend(structure_diagnostics)

    steps: list[dict[str, Any]] = []
    review_items: list[dict[str, Any]] = []
    used_ai = False

    for parsed in case.steps:
        if parsed.kind == "structured":
            step = normalize_step(
                parsed,
                declared_variables=set(case.variables),
                vision_allowed=allow_vision,
                diagnostics=diagnostics,
            )
            if step is None:
                continue
            if parsed.review_required:
                review_items.append(
                    {
                        "step_id": parsed.step_id,
                        "source_text": parsed.text,
                        "source_range": {"start_line": parsed.start_line, "end_line": parsed.end_line},
                        "generated": step,
                        "reason": "derived_locator_candidate",
                    }
                )
            steps.append(step)
            continue

        if ai_adapter is None or not ai_adapter.enabled:
            diagnostics.add(
                Diagnostic.build(
                    "AI_UNAVAILABLE",
                    f"Step {parsed.step_id} is natural language and AI compilation is unavailable",
                    step_id=parsed.step_id,
                    start_line=parsed.start_line,
                    end_line=parsed.end_line,
                    suggestion="Write the step as a ```yaml block, or enable the AI provider",
                )
            )
            continue
        # From here on, this step's text is handed to the provider. Whether it answers, answers wrongly or the
        # request never comes back, the case left the platform, and a compile that says `deterministic` about
        # that would be the quiet downgrade §6.4 forbids. Counting successful calls instead would understate
        # exactly the failure an operator most needs to see.
        used_ai = True
        result = compile_step(
            ai_adapter,
            case,
            parsed,
            capabilities=capabilities or set(),
            vision_allowed=allow_vision,
        )
        diagnostics.extend(result.diagnostics)
        if result.step is None:
            continue
        if result.review_item:
            review_items.append(result.review_item)
        steps.append(result.step)

    # The mode is decided by the hand-off above, not by whether a step came back, so a compile whose only
    # prose step failed the model is still filed as the assisted run it was.
    mode = "ai_assisted" if used_ai else "deterministic"

    if diagnostics.has_errors or not steps:
        if not diagnostics:
            diagnostics.add(Diagnostic.build("STEP_EMPTY", "No step could be compiled.", start_line=1))
        outcome.diagnostics = diagnostics.as_dicts()
        outcome.compiler_mode = mode
        outcome.case_title = case.title
        outcome.tags = case.tags
        outcome.source_digest = case.source_digest
        outcome.dsl_version = case.dsl_version
        outcome.duration_ms = _elapsed(started)
        if ai_adapter is not None:
            outcome.usage = ai_adapter.usage.as_dict()
            outcome.model = ai_adapter.usage.model
        return outcome

    payload: dict[str, Any] = {
        "ir_version": IR_VERSION,
        "case_revision_id": revision_id,
        "source_digest": case.source_digest,
        "compiler": {"version": COMPILER_VERSION, "mode": mode},
        "variables": case.variables,
        "defaults": {"timeout_ms": case.defaults["timeout_ms"]},
        "steps": steps,
    }
    try:
        ir = TestIR.model_validate(payload)
    except Exception as exc:  # pydantic ValidationError and friends
        diagnostics.add(
            Diagnostic.build(
                "IR_VALIDATION_FAILED",
                f"Generated IR violates the contract: {_first_error(exc)}",
                start_line=case.steps[0].start_line if case.steps else 1,
                suggestion="Simplify the step or provide explicit locators",
            )
        )
        outcome.diagnostics = diagnostics.as_dicts()
        outcome.compiler_mode = mode
        outcome.duration_ms = _elapsed(started)
        return outcome

    context = ValidationContext(
        revision_text=markdown.replace("\r\n", "\n"),
        capabilities=capabilities,
        attachments=attachments,
        max_step_timeout_ms=settings.step_timeout_max_ms,
        max_wait_duration_ms=settings.wait_duration_max_ms,
        allow_vision=allow_vision,
        declared_variables=set(case.variables),
    )
    semantic = validate_ir(ir, context)
    diagnostics.extend(semantic)
    if semantic.has_errors:
        outcome.diagnostics = diagnostics.as_dicts()
        outcome.compiler_mode = mode
        outcome.duration_ms = _elapsed(started)
        if ai_adapter is not None:
            outcome.usage = ai_adapter.usage.as_dict()
        return outcome

    dump = ir.model_dump(mode="json", exclude_none=True)
    outcome.status = CompileStatus.NEEDS_REVIEW.value if review_items else CompileStatus.SUCCEEDED.value
    outcome.ir = dump
    outcome.ir_digest = ir.digest()
    outcome.compiler_mode = mode
    outcome.review_items = review_items
    outcome.diagnostics = diagnostics.as_dicts()
    outcome.case_title = case.title
    outcome.tags = case.tags
    outcome.declared_variables = case.variables
    outcome.source_digest = case.source_digest
    outcome.dsl_version = case.dsl_version
    if ai_adapter is not None:
        outcome.usage = ai_adapter.usage.as_dict()
        outcome.model = ai_adapter.usage.model
    outcome.duration_ms = _elapsed(started)
    return outcome


def _first_error(exc: Exception) -> str:
    errors = getattr(exc, "errors", None)
    if callable(errors):
        with contextlib.suppress(Exception):  # pragma: no cover - the message is cosmetic
            items = errors()
            if items:
                first = items[0]
                path = ".".join(str(part) for part in first.get("loc", ()))
                return f"{path}: {first.get('msg')}" if path else str(first.get("msg"))
    return str(exc)[:300]


def _elapsed(started: float) -> int:
    return int((time.monotonic() - started) * 1000)
