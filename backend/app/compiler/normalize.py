"""Deterministic normalization of parsed DSL steps into IR payloads (§5.2, §6.1).

Nothing here calls a model or the target site; AI-derived candidates are produced by
`ai_compile` and are always marked for review.
"""

from __future__ import annotations

import re
from typing import Any

from ..ir.models import CONDITION_KINDS, STEP_CLASSES
from .diagnostics import Diagnostic, DiagnosticList

VARIABLE_FULL = re.compile(r"^\$\{(env|vars|secrets)\.([A-Za-z_][A-Za-z0-9_.-]*)\}$")
VARIABLE_ANY = re.compile(r"\$\{(?:env|vars|secrets)\.[A-Za-z_][A-Za-z0-9_.-]*\}")
VARIABLE_TEMPLATE_SAFE = re.compile(r"\$\{(?:env|vars)\.[A-Za-z_][A-Za-z0-9_.-]*\}")

TARGET_FIELDS = {"description", "type", "css", "role", "name", "text", "xpath", "exact", "allow_vision"}
ACTION_FIELDS: dict[str, set[str]] = {
    "open": {"url", "wait_until"},
    "click": {"target"},
    "clear": {"target"},
    "input": {"target", "value"},
    "upload": {"target", "attachment_ids"},
    "wait": {"condition", "duration_ms"},
    "assert": {"condition"},
    "screenshot": {"name", "full_page"},
}
COMMON_STEP_FIELDS = {"action", "timeout_ms", "human_policy"}
CONTROL_ROLE_SUFFIXES: dict[str, tuple[str, ...]] = {
    "button": ("按钮", "按键", "button"),
    "link": ("链接", "link"),
    "input": ("输入框", "文本框", "文本输入", "输入", "textbox", "input"),
    "textarea": ("文本域", "多行输入框"),
    "checkbox": ("复选框", "勾选框", "checkbox"),
    "radio": ("单选框", "单选按钮", "radio"),
    "select": ("下拉框", "下拉列表", "select"),
    "file": ("上传控件", "文件上传", "上传"),
}
TYPE_TO_ROLE = {
    "button": "button",
    "link": "link",
    "input": "textbox",
    "textarea": "textbox",
    "checkbox": "checkbox",
    "radio": "radio",
    "select": "combobox",
    "file": "button",
}
ROLE_TO_TYPE = {
    "button": "button",
    "link": "link",
    "textbox": "input",
    "checkbox": "checkbox",
    "radio": "radio",
    "combobox": "select",
    "searchbox": "input",
}


def _diag(
    diagnostics: DiagnosticList,
    code: str,
    message: str,
    *,
    step_id: str,
    line: int,
    field_name: str | None = None,
    suggestion: str | None = None,
    severity: str = "ERROR",
) -> None:
    diagnostics.add(
        Diagnostic.build(
            code,
            message,
            severity=severity,  # type: ignore[arg-type]
            step_id=step_id,
            start_line=line,
            field_name=field_name,
            suggestion=suggestion,
        )
    )


def value_spec(
    raw: Any,
    *,
    line: int,
    step_id: str,
    diagnostics: DiagnosticList,
    allow_secret: bool = False,
    field_name: str,
) -> dict[str, Any] | None:
    if isinstance(raw, dict) and "kind" in raw:  # already a ValueSpec (AI output)
        return raw
    if not isinstance(raw, (str, int, float, bool)):
        _diag(
            diagnostics,
            "DSL_MISSING_FIELD",
            f"'{field_name}' must be a scalar or a variable reference",
            step_id=step_id,
            line=line,
            field_name=field_name,
        )
        return None
    if not isinstance(raw, str):
        return {"kind": "literal", "value": raw}
    match = VARIABLE_FULL.match(raw.strip())
    if match:
        namespace, key = match.groups()
        if namespace == "secrets":
            if not allow_secret:
                _diag(
                    diagnostics,
                    "SECRET_FIELD_NOT_ALLOWED",
                    "Secrets are allowed only as a whole input value",
                    step_id=step_id,
                    line=line,
                    field_name=field_name,
                )
                return None
            return {"kind": "secret", "key": key}
        return {"kind": "variable", "namespace": namespace, "key": key}
    if "${" in raw:
        if "${secrets." in raw:
            _diag(
                diagnostics,
                "SECRET_TEMPLATE_NOT_ALLOWED",
                "A secret reference must be the whole field",
                step_id=step_id,
                line=line,
                field_name=field_name,
            )
            return None
        tokens = VARIABLE_TEMPLATE_SAFE.findall(raw)
        if not tokens:
            _diag(
                diagnostics,
                "VARIABLE_TEMPLATE_INVALID",
                f"Malformed variable placeholder in '{raw}'",
                step_id=step_id,
                line=line,
                field_name=field_name,
            )
            return None
        if raw.count("${") == len(tokens):
            return {"kind": "template", "template": raw}
        _diag(
            diagnostics,
            "VARIABLE_TEMPLATE_INVALID",
            "Templates may only contain ${{env.*}} or ${{vars.*}} placeholders",
            step_id=step_id,
            line=line,
            field_name=field_name,
        )
        return None
    return {"kind": "literal", "value": raw}


def target_spec(
    raw: Any,
    *,
    line: int,
    step_id: str,
    diagnostics: DiagnosticList,
    field_name: str = "target",
) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        _diag(
            diagnostics,
            "TARGET_DESCRIPTION_REQUIRED",
            "target must be a mapping with a description",
            step_id=step_id,
            line=line,
            field_name=field_name,
        )
        return None
    unknown = set(raw) - TARGET_FIELDS
    if unknown:
        _diag(
            diagnostics,
            "TARGET_FIELD_UNKNOWN",
            f"Unsupported target field '{sorted(unknown)[0]}'",
            step_id=step_id,
            line=line,
            field_name=f"{field_name}.{sorted(unknown)[0]}",
        )
        return None
    description = raw.get("description")
    if not isinstance(description, str) or not description.strip():
        _diag(
            diagnostics,
            "TARGET_DESCRIPTION_REQUIRED",
            "target.description is required",
            step_id=step_id,
            line=line,
            field_name=f"{field_name}.description",
        )
        return None
    control_type = raw.get("type")
    if control_type is not None and control_type not in TYPE_TO_ROLE and control_type not in {"text", "other"}:
        _diag(
            diagnostics,
            "DSL_AMBIGUOUS_SYNTAX",
            f"Unsupported control type '{control_type}'",
            step_id=step_id,
            line=line,
            field_name=f"{field_name}.type",
            suggestion="Use input, button, link, checkbox, radio, select, textarea, file, text or other",
        )
        return None
    candidates: list[dict[str, Any]] = []
    exact = raw.get("exact", True)
    if raw.get("css"):
        candidates.append({"strategy": "css", "selector": str(raw["css"])})
    if raw.get("role"):
        item: dict[str, Any] = {"strategy": "role", "role": str(raw["role"])}
        if raw.get("name") is not None:
            item["name"] = str(raw["name"])
            item["exact"] = bool(exact)
        candidates.append(item)
    if raw.get("text"):
        candidates.append({"strategy": "text", "text": str(raw["text"]), "exact": bool(exact)})
    if raw.get("xpath"):
        candidates.append({"strategy": "xpath", "selector": str(raw["xpath"])})
    result: dict[str, Any] = {"description": description.strip(), "candidates": candidates}
    if control_type is not None:
        result["type"] = control_type
    if raw.get("allow_vision"):
        result["allow_vision"] = True
    return result


def condition_spec(
    raw: Any,
    *,
    line: int,
    step_id: str,
    diagnostics: DiagnosticList,
    field_name: str = "condition",
) -> dict[str, Any] | None:
    if not isinstance(raw, dict) or not isinstance(raw.get("kind"), str):
        _diag(
            diagnostics,
            "CONDITION_INVALID",
            "condition needs a supported kind",
            step_id=step_id,
            line=line,
            field_name=field_name,
        )
        return None
    kind = raw["kind"]
    if kind not in CONDITION_KINDS:
        _diag(
            diagnostics,
            "CONDITION_UNSUPPORTED",
            f"Unsupported condition '{kind}'",
            step_id=step_id,
            line=line,
            field_name=f"{field_name}.kind",
            suggestion=f"Supported kinds: {', '.join(CONDITION_KINDS)}",
        )
        return None
    needs_expected = kind in {"page_contains", "url_equals", "url_contains", "text_equals", "value_equals"}
    needs_target = kind in {"element_visible", "element_hidden", "text_equals", "value_equals"}
    allowed = {"kind"} | ({"expected"} if needs_expected else set()) | ({"target"} if needs_target else set())
    extra = set(raw) - allowed
    if extra:
        _diag(
            diagnostics,
            "CONDITION_FIELD_UNSUPPORTED",
            f"'{kind}' does not accept {sorted(extra)[0]}",
            step_id=step_id,
            line=line,
            field_name=f"{field_name}.{sorted(extra)[0]}",
        )
        return None
    result: dict[str, Any] = {"kind": kind}
    if needs_expected:
        if raw.get("expected") in (None, ""):
            _diag(
                diagnostics,
                "CONDITION_EXPECTED_REQUIRED",
                f"{kind} requires expected",
                step_id=step_id,
                line=line,
                field_name=f"{field_name}.expected",
            )
            return None
        expected = value_spec(
            raw["expected"],
            line=line,
            step_id=step_id,
            diagnostics=diagnostics,
            allow_secret=False,
            field_name=f"{field_name}.expected",
        )
        if expected is None:
            return None
        result["expected"] = expected
    if needs_target:
        target = target_spec(
            raw.get("target"), line=line, step_id=step_id, diagnostics=diagnostics, field_name=f"{field_name}.target"
        )
        if target is None:
            return None
        if not target["candidates"]:
            _diag(
                diagnostics,
                "TARGET_NEEDS_LOCATOR",
                f"{kind} needs a deterministic locator candidate in its target",
                step_id=step_id,
                line=line,
                field_name=f"{field_name}.target.candidates",
                suggestion="Assertions and resume conditions may not rely on vision",
            )
            return None
        result["target"] = target
    return result


def derive_candidates(description: str, control_type: str | None) -> list[dict[str, Any]]:
    """§5.2 target normalization: a clear control type plus name may become a reviewable candidate.

    Only role/text candidates are derived from prose; CSS and XPath are never invented.
    """
    text = description.strip()
    if not text:
        return []
    suffixes = CONTROL_ROLE_SUFFIXES.get(control_type or "", ())
    matched_suffix = next((suffix for suffix in suffixes if text.endswith(suffix)), None)
    if control_type in CONTROL_ROLE_SUFFIXES:
        if matched_suffix is None or len(text) <= len(matched_suffix):
            return []
        name = text[: -len(matched_suffix)].strip()
        if not name:
            return []
        return [{"strategy": "role", "role": TYPE_TO_ROLE[control_type], "name": name, "exact": True}]
    if control_type is not None:
        return []
    # No declared control type: a short phrase that does not end in a control word is treated as visible text.
    control_words = {suffix for group in CONTROL_ROLE_SUFFIXES.values() for suffix in group}
    if len(text) > 60 or any(text.endswith(word) for word in control_words):
        return []
    return [{"strategy": "text", "text": text, "exact": False}]


def human_policy_spec(
    raw: Any,
    *,
    line: int,
    step_id: str,
    diagnostics: DiagnosticList,
) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        _diag(
            diagnostics,
            "HUMAN_POLICY_INVALID",
            "human_policy must be a mapping",
            step_id=step_id,
            line=line,
            field_name="human_policy",
        )
        return None
    if set(raw) - {"mode", "reason", "resume_condition"}:
        _diag(
            diagnostics,
            "HUMAN_POLICY_INVALID",
            f"Unsupported human_policy field '{sorted(set(raw) - {'mode', 'reason', 'resume_condition'})[0]}'",
            step_id=step_id,
            line=line,
            field_name="human_policy",
        )
        return None
    mode = raw.get("mode")
    if mode not in {"disabled", "on_challenge", "before"}:
        _diag(
            diagnostics,
            "HUMAN_POLICY_INVALID",
            "human_policy.mode must be disabled, on_challenge or before",
            step_id=step_id,
            line=line,
            field_name="human_policy.mode",
        )
        return None
    result: dict[str, Any] = {"mode": mode}
    if raw.get("reason") is not None:
        result["reason"] = str(raw["reason"])
    if raw.get("resume_condition") is not None:
        condition = condition_spec(
            raw["resume_condition"],
            line=line,
            step_id=step_id,
            diagnostics=diagnostics,
            field_name="human_policy.resume_condition",
        )
        if condition is None:
            return None
        result["resume_condition"] = condition
    if mode == "on_challenge" and "resume_condition" not in result:
        _diag(
            diagnostics,
            "HUMAN_POLICY_INVALID",
            "on_challenge requires a deterministic resume_condition",
            step_id=step_id,
            line=line,
            field_name="human_policy.resume_condition",
        )
        return None
    return result


def normalize_step(
    parsed: Any,
    *,
    declared_variables: set[str],
    vision_allowed: bool,
    diagnostics: DiagnosticList,
) -> dict[str, Any] | None:
    """One structured DSL step to an IR step dict. `parsed.review_required` marks derived targets."""

    payload: dict[str, Any] = dict(parsed.payload or {})
    step_id = parsed.step_id
    line = parsed.start_line
    action = payload.get("action")
    if not isinstance(action, str) or not action.strip():
        _diag(
            diagnostics,
            "DSL_MISSING_FIELD",
            "Each step needs an action field",
            step_id=step_id,
            line=line,
            field_name="action",
        )
        return None
    action = action.strip().lower()
    if action not in ACTION_FIELDS:
        _diag(
            diagnostics,
            "ACTION_UNSUPPORTED",
            f"Unsupported action '{action}'",
            step_id=step_id,
            line=line,
            field_name="action",
            suggestion=f"Supported actions: {', '.join(sorted(ACTION_FIELDS))}",
        )
        return None
    allowed = COMMON_STEP_FIELDS | ACTION_FIELDS[action]
    unknown = set(payload) - allowed
    if unknown:
        _diag(
            diagnostics,
            "DSL_FIELD_UNKNOWN",
            f"Unsupported field '{sorted(unknown)[0]}' for {action}",
            step_id=step_id,
            line=line,
            field_name=sorted(unknown)[0],
        )
        return None

    source = {"start_line": parsed.start_line, "end_line": parsed.end_line, "text": parsed.text}
    step: dict[str, Any] = {"id": step_id, "action": action, "source": source}

    if payload.get("timeout_ms") is not None:
        timeout = payload["timeout_ms"]
        if not isinstance(timeout, int) or isinstance(timeout, bool) or timeout <= 0:
            _diag(
                diagnostics,
                "STEP_TIMEOUT_INVALID",
                "timeout_ms must be a positive integer",
                step_id=step_id,
                line=line,
                field_name="timeout_ms",
            )
            return None
        step["timeout_ms"] = timeout
    if payload.get("human_policy") is not None:
        policy = human_policy_spec(payload["human_policy"], line=line, step_id=step_id, diagnostics=diagnostics)
        if policy is None:
            return None
        step["human_policy"] = policy

    def require_target() -> dict[str, Any] | None:
        target = target_spec(payload.get("target"), line=line, step_id=step_id, diagnostics=diagnostics)
        if target is None:
            return None
        if not target["candidates"]:
            derived = derive_candidates(target["description"], target.get("type"))
            if derived:
                target["candidates"] = derived
                parsed.review_required = True
                _diag(
                    diagnostics,
                    "TARGET_NEEDS_LOCATOR",
                    f"Generated candidate for '{target['description']}' from its description; review required",
                    step_id=step_id,
                    line=line,
                    field_name="target.candidates",
                    severity="WARNING",
                    suggestion="Confirm the generated role/text candidate or add an explicit locator",
                )
            elif target.get("allow_vision") and vision_allowed:
                parsed.review_required = True
                _diag(
                    diagnostics,
                    "TARGET_NEEDS_LOCATOR",
                    f"Target '{target['description']}' relies on AI vision",
                    step_id=step_id,
                    line=line,
                    field_name="target.candidates",
                    severity="WARNING",
                )
            else:
                _diag(
                    diagnostics,
                    "TARGET_NEEDS_LOCATOR",
                    f"Target '{target['description']}' has no locator",
                    step_id=step_id,
                    line=line,
                    field_name="target.candidates",
                    suggestion="Add css, role+name, text or xpath, or set allow_vision: true",
                )
                return None
        return target

    if action == "open":
        url = value_spec(
            payload.get("url"),
            line=line,
            step_id=step_id,
            diagnostics=diagnostics,
            allow_secret=False,
            field_name="url",
        )
        if url is None:
            return None
        step["url"] = url
        if payload.get("wait_until") is not None:
            wait_until = payload["wait_until"]
            if wait_until not in {"load", "domcontentloaded", "networkidle", "commit"}:
                _diag(
                    diagnostics,
                    "DSL_FIELD_UNKNOWN",
                    f"Unsupported wait_until '{wait_until}'",
                    step_id=step_id,
                    line=line,
                    field_name="wait_until",
                )
                return None
            step["wait_until"] = wait_until
    elif action in {"click", "clear"}:
        target = require_target()
        if target is None:
            return None
        step["target"] = target
    elif action == "input":
        target = require_target()
        if target is None:
            return None
        if "value" not in payload:
            _diag(
                diagnostics,
                "DSL_MISSING_FIELD",
                "input requires value",
                step_id=step_id,
                line=line,
                field_name="value",
                suggestion="填写常量、变量或密钥引用",
            )
            return None
        value = value_spec(
            payload["value"], line=line, step_id=step_id, diagnostics=diagnostics, allow_secret=True, field_name="value"
        )
        if value is None:
            return None
        step["target"] = target
        step["value"] = value
    elif action == "upload":
        target = require_target()
        if target is None:
            return None
        ids = payload.get("attachment_ids")
        if not isinstance(ids, list) or not ids or not all(isinstance(item, str) and item.strip() for item in ids):
            _diag(
                diagnostics,
                "ATTACHMENT_REQUIRED",
                "upload requires a non-empty attachment_ids list",
                step_id=step_id,
                line=line,
                field_name="attachment_ids",
            )
            return None
        step["target"] = target
        step["attachment_ids"] = [item.strip() for item in ids]
    elif action == "wait":
        has_condition, has_duration = "condition" in payload, "duration_ms" in payload
        if has_condition == has_duration:
            _diag(
                diagnostics,
                "WAIT_CONDITION_REQUIRED",
                "wait needs exactly one of condition or duration_ms",
                step_id=step_id,
                line=line,
                field_name="condition",
            )
            return None
        if has_condition:
            condition = condition_spec(payload["condition"], line=line, step_id=step_id, diagnostics=diagnostics)
            if condition is None:
                return None
            step["condition"] = condition
        else:
            duration = payload["duration_ms"]
            if not isinstance(duration, int) or isinstance(duration, bool) or duration <= 0:
                _diag(
                    diagnostics,
                    "STEP_TIMEOUT_INVALID",
                    "duration_ms must be a positive integer",
                    step_id=step_id,
                    line=line,
                    field_name="duration_ms",
                )
                return None
            step["duration_ms"] = duration
    elif action == "assert":
        condition = condition_spec(payload.get("condition"), line=line, step_id=step_id, diagnostics=diagnostics)
        if condition is None:
            return None
        step["condition"] = condition
    elif action == "screenshot":
        if payload.get("name") is not None:
            step["name"] = str(payload["name"])
        if payload.get("full_page") is not None:
            if not isinstance(payload["full_page"], bool):
                _diag(
                    diagnostics,
                    "DSL_FIELD_UNKNOWN",
                    "full_page must be a boolean",
                    step_id=step_id,
                    line=line,
                    field_name="full_page",
                )
                return None
            step["full_page"] = payload["full_page"]

    if step["action"] not in STEP_CLASSES:
        return None
    _check_variable_references(step, declared_variables, step_id=step_id, line=line, diagnostics=diagnostics)
    return step


def _check_variable_references(
    payload: Any,
    declared: set[str],
    *,
    step_id: str,
    line: int,
    diagnostics: DiagnosticList,
    path: str = "",
) -> None:
    if isinstance(payload, dict):
        if (
            payload.get("kind") == "variable"
            and payload.get("namespace") == "vars"
            and payload.get("key") not in declared
        ):
            _diag(
                diagnostics,
                "VARIABLE_UNDECLARED",
                f"Variable '{payload.get('key')}' is not declared in front matter",
                step_id=step_id,
                line=line,
                field_name=path or "variables",
                suggestion="Declare it under variables: in the front matter",
            )
        for key, value in payload.items():
            _check_variable_references(
                value,
                declared,
                step_id=step_id,
                line=line,
                diagnostics=diagnostics,
                path=f"{path}.{key}" if path else key,
            )
    elif isinstance(payload, list):
        for item in payload:
            _check_variable_references(item, declared, step_id=step_id, line=line, diagnostics=diagnostics, path=path)
