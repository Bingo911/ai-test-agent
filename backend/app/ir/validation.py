"""Semantic IR validation (§5.2, §5.3). Schema-valid IR can still be rejected here."""

from __future__ import annotations

import re
from typing import Any

from ..compiler.diagnostics import Diagnostic, DiagnosticList
from .models import Condition, Target, TestIR, ValueSpec

_VARIABLE_REF = re.compile(r"\$\{(env|vars|secrets)\.([A-Za-z_][A-Za-z0-9_.-]*)\}")
_FORBIDDEN_SCHEMES = re.compile(r"^\s*(file|javascript|data|view-source|blob|chrome|chrome-extension):", re.I)
_TEMPLATE_REF = re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_.-]*\}")


class ValidationContext:
    def __init__(
        self,
        *,
        revision_text: str,
        capabilities: set[str] | None = None,
        attachments: dict[str, str] | None = None,
        max_step_timeout_ms: int = 120_000,
        max_wait_duration_ms: int = 10_000,
        allow_vision: bool = True,
        declared_variables: set[str] | None = None,
    ) -> None:
        self.revision_lines = revision_text.splitlines()
        self.capabilities = capabilities or set()
        self.attachments = attachments or {}
        self.max_step_timeout_ms = max_step_timeout_ms
        self.max_wait_duration_ms = max_wait_duration_ms
        self.allow_vision = allow_vision
        self.declared_variables = declared_variables or set()


def validate_ir(ir: TestIR, context: ValidationContext) -> DiagnosticList:
    diagnostics = DiagnosticList()
    for index, step in enumerate(ir.steps, start=1):
        if step.id != f"s{index}":
            diagnostics.add(
                Diagnostic.build(
                    "STEP_SEQUENCE_INVALID",
                    f"Step ids must be s1..s{len(ir.steps)} in order; found '{step.id}' at position {index}",
                    step_id=step.id,
                    start_line=step.source.start_line,
                    end_line=step.source.end_line,
                )
            )
        _check_source(step, context, diagnostics)
        if context.capabilities and step.action not in context.capabilities:
            diagnostics.add(
                Diagnostic.build(
                    "COMPILER_CAPABILITY_MISSING",
                    f"Executor does not support action '{step.action}'",
                    step_id=step.id,
                    start_line=step.source.start_line,
                    suggestion="Remove the step or select an executor that declares this capability",
                )
            )
        if step.timeout_ms is not None and step.timeout_ms > context.max_step_timeout_ms:
            diagnostics.add(
                Diagnostic.build(
                    "STEP_TIMEOUT_INVALID",
                    f"timeout_ms {step.timeout_ms} exceeds the system limit {context.max_step_timeout_ms}",
                    step_id=step.id,
                    start_line=step.source.start_line,
                    field_name="timeout_ms",
                )
            )
        if step.human_policy is not None:
            _check_human_policy(step, context, diagnostics)
        _check_step_payload(step, context, diagnostics)
    return diagnostics


def _check_source(step: Any, context: ValidationContext, diagnostics: DiagnosticList) -> None:
    source = step.source
    if source.end_line > len(context.revision_lines):
        diagnostics.add(
            Diagnostic.build(
                "IR_VALIDATION_FAILED",
                f"source range ends at line {source.end_line} beyond the revision "
                f"({len(context.revision_lines)} lines)",
                step_id=step.id,
                start_line=source.start_line,
                end_line=source.end_line,
                field_name="source",
            )
        )
        return
    slice_text = "\n".join(context.revision_lines[source.start_line - 1 : source.end_line]).rstrip()
    if slice_text != source.text.rstrip():
        diagnostics.add(
            Diagnostic.build(
                "IR_VALIDATION_FAILED",
                "source.text is not an exact slice of the case revision",
                step_id=step.id,
                start_line=source.start_line,
                end_line=source.end_line,
                field_name="source.text",
                suggestion="Recompile after editing; the IR must trace back to the original text",
            )
        )


def _check_human_policy(step: Any, context: ValidationContext, diagnostics: DiagnosticList) -> None:
    policy = step.human_policy
    assert policy is not None  # noqa: S101  # the caller checks the field is set
    if policy.mode == "on_challenge" and policy.resume_condition is not None:
        _check_condition(
            policy.resume_condition, step.id, step.source, context, diagnostics, "human_policy.resume_condition"
        )


def _check_step_payload(step: Any, context: ValidationContext, diagnostics: DiagnosticList) -> None:
    line = step.source.start_line
    end = step.source.end_line

    def location(field_name: str, code: str, message: str, suggestion: str | None = None) -> Diagnostic:
        return Diagnostic.build(
            code, message, step_id=step.id, start_line=line, end_line=end, field_name=field_name, suggestion=suggestion
        )

    if step.action == "open":
        _check_value(step.url, context, diagnostics, step, secret_allowed=False, field_name="url")
        literal = _literal_of(step.url)
        if literal is not None:
            text = str(literal)
            if _FORBIDDEN_SCHEMES.match(text):
                diagnostics.add(
                    location("url", "DOMAIN_NOT_ALLOWED", f"Scheme in '{text}' is not allowed for navigation")
                )
        elif not _is_reference(step.url):
            diagnostics.add(
                location("url", "URL_REQUIRED", "open requires a URL or a ${{env.base_url}} style reference")
            )
    elif step.action in {"click", "clear", "input", "upload"}:
        _check_target(step.target, context, diagnostics, step, action_required=True)
        if step.action == "input":
            _check_value(step.value, context, diagnostics, step, secret_allowed=True, field_name="value")
        if step.action == "upload":
            for attachment_id in step.attachment_ids:
                status = context.attachments.get(attachment_id)
                if status is None:
                    diagnostics.add(
                        location(
                            "attachment_ids",
                            "NOT_FOUND",
                            f"Attachment '{attachment_id}' is not registered in this project",
                            suggestion="Upload the file first, then reference the returned attachment_id",
                        )
                    )
                elif status != "CLEAN":
                    diagnostics.add(
                        location(
                            "attachment_ids",
                            "ATTACHMENT_NOT_READY",
                            f"Attachment '{attachment_id}' has scan status {status}",
                        )
                    )
    elif step.action == "wait":
        if step.duration_ms is not None and step.duration_ms > context.max_wait_duration_ms:
            diagnostics.add(
                location(
                    "duration_ms",
                    "STEP_TIMEOUT_INVALID",
                    f"Fixed waits are limited to {context.max_wait_duration_ms} ms",
                    suggestion="Use a condition instead of a long fixed wait",
                )
            )
        if step.condition is not None:
            _check_condition(step.condition, step.id, step.source, context, diagnostics, "condition")
    elif step.action == "assert":
        _check_condition(step.condition, step.id, step.source, context, diagnostics, "condition")


def _check_condition(
    condition: Condition,
    step_id: str,
    source: Any,
    context: ValidationContext,
    diagnostics: DiagnosticList,
    field_name: str,
) -> None:
    if condition.expected is not None:
        _check_value(
            condition.expected,
            context,
            diagnostics,
            None,
            secret_allowed=False,
            field_name=f"{field_name}.expected",
            step_id=step_id,
            source=source,
        )
        literal = _literal_of(condition.expected)
        if literal is None or str(literal) == "":
            diagnostics.add(
                Diagnostic.build(
                    "CONDITION_EXPECTED_REQUIRED",
                    f"{condition.kind} needs a non-empty expected value",
                    step_id=step_id,
                    start_line=source.start_line,
                    end_line=source.end_line,
                    field_name=f"{field_name}.expected",
                )
            )
    if condition.target is not None:
        _check_target(
            condition.target,
            context,
            diagnostics,
            None,
            action_required=False,
            step_id=step_id,
            source=source,
            field_name=f"{field_name}.target",
        )


def _check_target(
    target: Target,
    context: ValidationContext,
    diagnostics: DiagnosticList,
    step: Any,
    *,
    action_required: bool,
    step_id: str | None = None,
    source: Any = None,
    field_name: str = "target",
) -> None:
    step_id = step_id or (step.id if step is not None else None)
    source = source or (step.source if step is not None else None)
    start, end = (source.start_line, source.end_line) if source else (1, 1)
    deterministic = [
        candidate for candidate in target.candidates if candidate.strategy in {"css", "role", "text", "xpath"}
    ]
    if not deterministic:
        if action_required and target.allow_vision and context.allow_vision:
            return
        diagnostics.add(
            Diagnostic.build(
                "TARGET_NEEDS_LOCATOR",
                f"Target '{target.description}' has no deterministic locator candidate",
                step_id=step_id,
                start_line=start,
                end_line=end,
                field_name=f"{field_name}.candidates",
                suggestion="Add css/role+name/text/xpath, or enable vision for interaction steps",
            )
        )
        return
    for candidate in deterministic:
        blob = " ".join(filter(None, [candidate.selector, candidate.role, candidate.name, candidate.text]))
        if "${secrets." in blob:
            diagnostics.add(
                Diagnostic.build(
                    "SECRET_FIELD_NOT_ALLOWED",
                    "Secret references cannot be embedded in locator candidates",
                    step_id=step_id,
                    start_line=start,
                    end_line=end,
                    field_name=f"{field_name}.candidates",
                )
            )
        if candidate.strategy in {"css", "xpath"} and not candidate.selector:
            diagnostics.add(
                Diagnostic.build(
                    "DSL_MISSING_FIELD",
                    f"{candidate.strategy} candidate needs a selector",
                    step_id=step_id,
                    start_line=start,
                    end_line=end,
                    field_name=f"{field_name}.candidates.selector",
                )
            )


def _check_value(
    value: ValueSpec,
    context: ValidationContext,
    diagnostics: DiagnosticList,
    step: Any,
    *,
    secret_allowed: bool,
    field_name: str,
    step_id: str | None = None,
    source: Any = None,
) -> None:
    step_id = step_id or (step.id if step is not None else None)
    source = source or (step.source if step is not None else None)
    start, end = (source.start_line, source.end_line) if source else (1, 1)
    if value.kind == "secret" and not secret_allowed:
        diagnostics.add(
            Diagnostic.build(
                "SECRET_FIELD_NOT_ALLOWED",
                f"Secret references are not allowed in '{field_name}'",
                step_id=step_id,
                start_line=start,
                end_line=end,
                field_name=field_name,
                suggestion="Use ${{env.*}} or ${{vars.*}} for URLs, selectors and assertions",
            )
        )
    if value.kind == "variable" and value.namespace == "vars" and value.key not in context.declared_variables:
        diagnostics.add(
            Diagnostic.build(
                "VARIABLE_UNDECLARED",
                f"Variable '{value.key}' is not declared in the case front matter",
                step_id=step_id,
                start_line=start,
                end_line=end,
                field_name=field_name,
            )
        )
    if value.kind == "template":
        if "${secrets." in value.template:
            diagnostics.add(
                Diagnostic.build(
                    "SECRET_TEMPLATE_NOT_ALLOWED",
                    "Secrets may not be interpolated into templates",
                    step_id=step_id,
                    start_line=start,
                    end_line=end,
                    field_name=field_name,
                )
            )
        for namespace, key in _VARIABLE_REF.findall(value.template):
            if namespace == "vars" and key not in context.declared_variables:
                diagnostics.add(
                    Diagnostic.build(
                        "VARIABLE_UNDECLARED",
                        f"Variable '{key}' is not declared in the case front matter",
                        step_id=step_id,
                        start_line=start,
                        end_line=end,
                        field_name=field_name,
                    )
                )
            if namespace == "secrets":
                diagnostics.add(
                    Diagnostic.build(
                        "SECRET_TEMPLATE_NOT_ALLOWED",
                        "Secrets may only be used as a whole input value",
                        step_id=step_id,
                        start_line=start,
                        end_line=end,
                        field_name=field_name,
                    )
                )


def _literal_of(value: ValueSpec | None) -> Any:
    if value is None:
        return None
    if value.kind == "literal":
        return value.value
    if value.kind == "template":
        return value.template
    return None


def _is_reference(value: ValueSpec) -> bool:
    if value.kind in {"variable", "secret"}:
        return True
    if value.kind == "template":
        return bool(_TEMPLATE_REF.search(value.template))
    return False
