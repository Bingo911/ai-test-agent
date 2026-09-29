from __future__ import annotations

import hashlib
import re
from typing import Any

import yaml
from pydantic import ValidationError


class UniqueKeyLoader(yaml.SafeLoader):
    def construct_mapping(self, node: yaml.MappingNode, deep: bool = False) -> dict[Any, Any]:
        self.flatten_mapping(node)
        mapping: dict[Any, Any] = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            try:
                duplicated = key in mapping
            except TypeError as exc:
                raise yaml.constructor.ConstructorError("mapping", node.start_mark, "unhashable mapping key", key_node.start_mark) from exc
            if duplicated:
                raise yaml.constructor.ConstructorError("mapping", node.start_mark, f"duplicate key {key!r}", key_node.start_mark)
            mapping[key] = self.construct_object(value_node, deep=deep)
        return mapping

from .schemas import TestIR

STEP_HEADING = re.compile(r"^##\s+Step\s+(\d+)\s*$", re.IGNORECASE)
VARIABLE = re.compile(r"^\$\{(env|vars|secrets)\.([A-Za-z_][A-Za-z0-9_.-]*)\}$")


class ParseError(Exception):
    def __init__(self, code: str, message: str, line: int | None = None) -> None:
        self.code, self.message, self.line = code, message, line

    def as_dict(self) -> dict[str, object]:
        result: dict[str, object] = {"code": self.code, "message": self.message}
        if self.line is not None:
            result["source_range"] = {"start_line": self.line, "end_line": self.line}
        return result


def value_spec(value: Any, line: int, *, allow_secret: bool = False) -> dict[str, Any]:
    if not isinstance(value, str):
        return {"kind": "literal", "value": value}
    match = VARIABLE.fullmatch(value)
    if match:
        namespace, key = match.groups()
        if namespace == "secrets" and not allow_secret:
            raise ParseError("SECRET_FIELD_NOT_ALLOWED", "Secrets are supported only as input values.", line)
        return {"kind": "secret", "key": key} if namespace == "secrets" else {
            "kind": "variable", "namespace": namespace, "key": key,
        }
    if "${" in value:
        if "${secrets." in value:
            raise ParseError("SECRET_TEMPLATE_NOT_ALLOWED", "A secret reference must be the whole field.", line)
        tokens = re.findall(r"\$\{(?:env|vars)\.[A-Za-z_][A-Za-z0-9_.-]*\}", value)
        parts = re.split(r"\$\{(?:env|vars)\.[A-Za-z_][A-Za-z0-9_.-]*\}", value)
        if tokens and "".join(parts) == "":
            return {"kind": "template", "template": value}
        raise ParseError("VARIABLE_TEMPLATE_INVALID", "Malformed or unsupported variable placeholder.", line)
    return {"kind": "literal", "value": value}


def target_spec(raw: Any, line: int) -> dict[str, Any]:
    if not isinstance(raw, dict) or not isinstance(raw.get("description"), str) or not raw["description"].strip():
        raise ParseError("TARGET_DESCRIPTION_REQUIRED", "target.description is required.", line)
    unknown = set(raw) - {"description", "type", "css", "role", "name", "text", "xpath", "exact", "allow_vision"}
    if unknown:
        raise ParseError("TARGET_FIELD_UNKNOWN", f"Unsupported target field '{sorted(unknown)[0]}'.", line)
    candidates: list[dict[str, Any]] = []
    if raw.get("css"):
        candidates.append({"strategy": "css", "selector": raw["css"]})
    if raw.get("role"):
        item: dict[str, Any] = {"strategy": "role", "role": raw["role"]}
        if raw.get("name") is not None:
            item.update(name=raw["name"], exact=raw.get("exact", True))
        candidates.append(item)
    if raw.get("text"):
        candidates.append({"strategy": "text", "text": raw["text"], "exact": raw.get("exact", True)})
    if raw.get("xpath"):
        candidates.append({"strategy": "xpath", "selector": raw["xpath"]})
    result = {"description": raw["description"].strip(), "candidates": candidates}
    if raw.get("type") is not None:
        result["type"] = raw["type"]
    if raw.get("allow_vision"):
        result["allow_vision"] = True
    return result


def condition_spec(raw: Any, line: int) -> dict[str, Any]:
    if not isinstance(raw, dict) or not isinstance(raw.get("kind"), str):
        raise ParseError("CONDITION_INVALID", "condition needs a supported kind.", line)
    kind = raw["kind"]
    allowed = {"page_contains", "url_equals", "url_contains", "element_visible", "element_hidden", "text_equals", "value_equals"}
    if kind not in allowed:
        raise ParseError("CONDITION_UNSUPPORTED", f"Unsupported condition '{kind}'.", line)
    result: dict[str, Any] = {"kind": kind}
    needs_expected = kind in {"page_contains", "url_equals", "url_contains", "text_equals", "value_equals"}
    needs_target = kind in {"element_visible", "element_hidden", "text_equals", "value_equals"}
    if needs_expected:
        if "expected" not in raw:
            raise ParseError("CONDITION_EXPECTED_REQUIRED", f"{kind} requires expected.", line)
        result["expected"] = value_spec(raw["expected"], line)
    elif "expected" in raw:
        raise ParseError("CONDITION_FIELD_UNSUPPORTED", f"{kind} does not accept expected.", line)
    if needs_target:
        result["target"] = target_spec(raw.get("target"), line)
    elif "target" in raw:
        raise ParseError("CONDITION_FIELD_UNSUPPORTED", f"{kind} does not accept target.", line)
    return result


def _step(raw: Any, start: int, end: int, source_text: str, number: int) -> tuple[dict[str, Any], bool]:
    if not isinstance(raw, dict) or not isinstance(raw.get("action"), str):
        raise ParseError("STEP_ACTION_REQUIRED", "Each step needs an action field.", start)
    action = raw["action"].strip().lower()
    action_fields = {
        "open": {"url", "wait_until"},
        "click": {"target"},
        "input": {"target", "value"},
        "clear": {"target"},
        "upload": {"target", "attachment_ids"},
        "wait": {"condition", "duration_ms"},
        "assert": {"condition"},
        "screenshot": {"name", "full_page"},
    }
    if action not in action_fields:
        raise ParseError("ACTION_UNSUPPORTED", f"Unsupported action '{action}'.", start)
    unknown = set(raw) - ({"action", "timeout_ms", "human_policy"} | action_fields[action])
    if unknown:
        raise ParseError("STEP_FIELD_UNKNOWN", f"Unsupported field '{sorted(unknown)[0]}' for {action}.", start)
    source = {"start_line": start, "end_line": end, "text": source_text}
    result: dict[str, Any] = {"id": f"s{number}", "action": action, "source": source}
    if raw.get("timeout_ms") is not None:
        result["timeout_ms"] = raw["timeout_ms"]
    if raw.get("human_policy") is not None:
        result["human_policy"] = raw["human_policy"]
    review_required = False
    if action == "open":
        if not isinstance(raw.get("url"), str):
            raise ParseError("URL_REQUIRED", "open requires a URL string.", start)
        result["url"] = value_spec(raw["url"], start)
        result["wait_until"] = raw.get("wait_until", "domcontentloaded")
    elif action in {"click", "input", "clear", "upload"}:
        target = target_spec(raw.get("target"), start)
        if not target["candidates"]:
            phrase = target["description"]
            control = raw["target"].get("type")
            role_suffix = {"button": ("按钮", "button"), "input": ("输入框", "textbox"), "link": ("链接", "link"), "checkbox": ("复选框", "checkbox")}
            if control in role_suffix and phrase.endswith(role_suffix[control][0]) and len(phrase) > len(role_suffix[control][0]):
                suffix, role = role_suffix[control]
                target["candidates"] = [{"strategy": "role", "role": role, "name": phrase[:-len(suffix)], "exact": True}]
            elif target.get("allow_vision"):
                pass
            else:
                raise ParseError("TARGET_NEEDS_LOCATOR", "Add a CSS/role/text/xpath locator or enable vision.", start)
            review_required = True
        result["target"] = target
        if action == "input":
            if "value" not in raw:
                raise ParseError("DSL_MISSING_FIELD", "input requires value.", start)
            result["value"] = value_spec(raw["value"], start, allow_secret=True)
        elif action == "upload":
            ids = raw.get("attachment_ids")
            if not isinstance(ids, list) or not ids:
                raise ParseError("ATTACHMENT_REQUIRED", "upload requires attachment_ids.", start)
            result["attachment_ids"] = ids
    elif action == "wait":
        has_condition, has_duration = "condition" in raw, "duration_ms" in raw
        if has_condition == has_duration:
            raise ParseError("WAIT_CONDITION_REQUIRED", "wait needs exactly one of condition or duration_ms.", start)
        if has_condition:
            result["condition"] = condition_spec(raw["condition"], start)
        else:
            result["duration_ms"] = raw["duration_ms"]
    elif action == "assert":
        result["condition"] = condition_spec(raw.get("condition"), start)
    elif action == "screenshot":
        if raw.get("name") is not None:
            result["name"] = raw["name"]
        if raw.get("full_page") is not None:
            result["full_page"] = raw["full_page"]
    else:
        raise ParseError("ACTION_UNSUPPORTED", f"Unsupported action '{action}'.", start)
    return result, review_required


def _safe_yaml(text: str, line: int) -> Any:
    try:
        depth = 0
        for event in yaml.parse(text, Loader=yaml.SafeLoader):
            if isinstance(event, yaml.AliasEvent) or getattr(event, "anchor", None):
                raise ParseError("DSL_YAML_ALIAS_FORBIDDEN", "YAML anchors and aliases are not supported.", line)
            if isinstance(event, yaml.ScalarEvent) and event.tag is not None and not event.tag.startswith("tag:yaml.org,2002:"):
                raise ParseError("DSL_YAML_TAG_FORBIDDEN", "Custom YAML tags are not supported.", line)
            if isinstance(event, (yaml.MappingStartEvent, yaml.SequenceStartEvent)):
                depth += 1
                if depth > 32:
                    raise ParseError("DSL_YAML_TOO_DEEP", "YAML nesting exceeds 32 levels.", line)
            elif isinstance(event, (yaml.MappingEndEvent, yaml.SequenceEndEvent)):
                depth -= 1
        return yaml.load(text, Loader=UniqueKeyLoader)
    except ParseError:
        raise
    except yaml.YAMLError as exc:
        raise ParseError("DSL_YAML_INVALID", "YAML syntax is invalid.", line) from exc


def compile_markdown(markdown: str) -> tuple[dict[str, Any], bool]:
    encoded = markdown.encode("utf-8")
    if len(encoded) > 262144:
        raise ParseError("CASE_TOO_LARGE", "Markdown input exceeds 256 KiB.")
    lines = markdown.splitlines()
    titles = [line for line in lines if re.match(r"^#\s+\S", line)]
    if len(titles) != 1 or sum(line.startswith("# ") for line in lines) != 1:
        raise ParseError("CASE_TITLE_REQUIRED", "Add exactly one non-empty '# Case title' heading.")
    front_matter: dict[str, Any] = {}
    content_offset = 0
    if lines and lines[0].strip() == "---":
        closing = next((index for index in range(1, len(lines)) if lines[index].strip() == "---"), None)
        if closing is None:
            raise ParseError("FRONT_MATTER_UNCLOSED", "Front matter needs a closing '---'.", 1)
        parsed_front_matter = _safe_yaml("\n".join(lines[1:closing]), 2)
        if not isinstance(parsed_front_matter, dict):
            raise ParseError("FRONT_MATTER_INVALID", "Front matter must be a YAML mapping.", 2)
        allowed_front_fields = {"dsl_version", "tags", "variables", "defaults"}
        unexpected = set(parsed_front_matter) - allowed_front_fields
        if unexpected:
            raise ParseError("FRONT_MATTER_FIELD_UNKNOWN", f"Unsupported front-matter field '{sorted(unexpected)[0]}'.", 2)
        if parsed_front_matter.get("dsl_version", "1.0") != "1.0":
            raise ParseError("DSL_VERSION_UNSUPPORTED", "Only dsl_version 1.0 is supported by this MVP.", 2)
        front_matter = parsed_front_matter
        content_offset = closing + 1
    variable_definitions = front_matter.get("variables", {})
    if not isinstance(variable_definitions, dict):
        raise ParseError("VARIABLES_INVALID", "Front-matter variables must be a mapping.", 1)
    for variable_name, definition in variable_definitions.items():
        if not isinstance(definition, dict) or definition.get("type") not in {"string", "integer", "number", "boolean"}:
            raise ParseError("VARIABLE_DEFINITION_INVALID", f"Variable '{variable_name}' needs a supported type.", 1)
        if set(definition) - {"type", "required"}:
            raise ParseError("VARIABLE_DEFINITION_INVALID", f"Variable '{variable_name}' has unsupported fields.", 1)
    defaults = front_matter.get("defaults", {})
    if not isinstance(defaults, dict) or set(defaults) - {"timeout_ms"}:
        raise ParseError("DEFAULTS_INVALID", "defaults supports timeout_ms only.", 1)
    effective_defaults = {"timeout_ms": defaults.get("timeout_ms", 10000)}
    if not isinstance(effective_defaults["timeout_ms"], int) or not 0 < effective_defaults["timeout_ms"] <= 120000:
        raise ParseError("DEFAULT_TIMEOUT_INVALID", "defaults.timeout_ms must be between 1 and 120000.", 1)
    number = content_offset
    current: tuple[int, int] | None = None
    steps: list[dict[str, Any]] = []
    review_required = False
    seen: set[int] = set()
    while number < len(lines):
        heading = STEP_HEADING.fullmatch(lines[number].strip())
        if not heading:
            if re.match(r"^##\s+Step\b", lines[number], re.IGNORECASE):
                raise ParseError("STEP_HEADING_INVALID", "Use a heading such as '## Step 1'.", number + 1)
            number += 1
            continue
        step_number = int(heading.group(1))
        if step_number in seen:
            raise ParseError("STEP_NUMBER_DUPLICATED", f"Step {step_number} appears more than once.", number + 1)
        seen.add(step_number)
        start = number
        number += 1
        code_blocks: list[tuple[list[str], int]] = []
        prose: list[str] = []
        while number < len(lines) and not STEP_HEADING.fullmatch(lines[number].strip()):
            line = lines[number].strip()
            if line.startswith("```"):
                match = re.fullmatch(r"```([A-Za-z0-9_-]*)", line)
                if not match or match.group(1).lower() != "yaml":
                    raise ParseError("DSL_FENCE_INVALID", "Steps may contain one ```yaml block.", number + 1)
                block_start = number + 2
                number += 1
                block: list[str] = []
                while number < len(lines) and lines[number].strip() != "```":
                    block.append(lines[number]); number += 1
                if number >= len(lines):
                    raise ParseError("DSL_FENCE_UNCLOSED", "Unclosed YAML block.", start + 1)
                code_blocks.append((block, block_start))
                number += 1
                continue
            if line:
                prose.append(lines[number])
            number += 1
        if code_blocks and (len(code_blocks) != 1 or prose):
            raise ParseError("DSL_STEP_AMBIGUOUS", "Use one YAML block or one natural-language step.", start + 1)
        if not code_blocks:
            if prose:
                raise ParseError("AI_COMPILER_NOT_CONFIGURED", "Natural-language compilation is planned; use structured YAML in this MVP.", start + 1)
            raise ParseError("STEP_EMPTY", "Step has no YAML definition.", start + 1)
        block, block_start = code_blocks[0]
        yaml_text = "\n".join(block)
        raw = _safe_yaml(yaml_text, block_start)
        if not isinstance(raw, dict):
            raise ParseError("STEP_YAML_MAPPING_REQUIRED", "Each step's YAML root must be a mapping.", block_start)
        source_text = "\n".join(lines[start:number])
        parsed, needs_review = _step(raw, start + 1, number, source_text, step_number)
        steps.append(parsed)
        review_required = review_required or needs_review
    actual = [int(step["id"][1:]) for step in steps]
    if not steps:
        raise ParseError("CASE_STEPS_REQUIRED", "Add at least one '## Step N' heading.")
    if actual != list(range(1, len(steps) + 1)):
        raise ParseError("STEP_SEQUENCE_INVALID", "Step numbers must start at 1 and be contiguous.")
    def check_variable_references(value: Any, line: int) -> None:
        if isinstance(value, dict):
            if value.get("kind") == "variable" and value.get("namespace") == "vars":
                if value["key"] not in variable_definitions:
                    raise ParseError("VARIABLE_UNDECLARED", f"Variable '{value['key']}' must be declared in front matter.", line)
            for item in value.values():
                check_variable_references(item, line)
        elif isinstance(value, list):
            for item in value:
                check_variable_references(item, line)

    for step in steps:
        check_variable_references(step, step["source"]["start_line"])
    ir = {
        "ir_version": "1.0", "case_revision_id": None,
        "source_digest": "sha256:" + hashlib.sha256(encoded).hexdigest(),
        "compiler": {"version": "0.1.0", "mode": "deterministic"},
        "variables": variable_definitions, "defaults": effective_defaults, "steps": steps,
    }
    try:
        return TestIR.model_validate(ir).model_dump(mode="json", exclude_none=True), review_required
    except ValidationError as exc:
        error = exc.errors()[0]
        path = ".".join(str(part) for part in error["loc"])
        raise ParseError("IR_VALIDATION_FAILED", f"{path}: {error['msg']}") from exc
