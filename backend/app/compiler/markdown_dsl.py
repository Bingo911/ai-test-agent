"""Markdown DSL parsing (§4). Standard fenced-YAML mode plus the `legacy-prd-v1` compatibility mode."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any

import yaml

from ..ir.models import DSL_VERSION, LEGACY_DSL_VERSION
from .diagnostics import Diagnostic, DiagnosticList, DslError

STEP_HEADING = re.compile(r"^##\s+Step\s+(\d+)\s*$", re.IGNORECASE)
OTHER_HEADING = re.compile(r"^#{1,6}\s+")
TOP_LEVEL_TITLE = re.compile(r"^#\s+(\S.*)$")
FENCE = re.compile(r"^\s*```([A-Za-z0-9_-]*)\s*$")
LEGACY_FIELDS = ("action", "url", "target", "value", "condition", "attachment_ids", "timeout_ms", "human_policy")
FRONT_MATTER_FIELDS = {"dsl_version", "tags", "variables", "defaults"}
VARIABLE_REF = re.compile(r"^\$\{(env|vars|secrets)\.([A-Za-z_][A-Za-z0-9_.-]*)\}$")
_LEGACY_LINE = re.compile(r"^([A-Za-z_][A-Za-z0-9_-]*)\s*:\s*(.*)$")
_LEGACY_INDENTED = re.compile(r"^\s+[A-Za-z_][A-Za-z0-9_-]*\s*:")
_URI_LIKE = re.compile(r"^(?:[a-z][a-z0-9+.-]*://|//|www\.)", re.I)


def key_line(stripped: str) -> re.Match[str] | None:
    """A legacy `field: value` line. URLs (https://...) are values, never keys."""
    if _URI_LIKE.match(stripped):
        return None
    return _LEGACY_LINE.match(stripped)


class UniqueKeyLoader(yaml.SafeLoader):
    """Safe loader that additionally rejects duplicate mapping keys."""

    def construct_mapping(self, node: yaml.MappingNode, deep: bool = False) -> dict[Any, Any]:
        self.flatten_mapping(node)
        mapping: dict[Any, Any] = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            try:
                duplicated = key in mapping
            except TypeError as exc:
                raise yaml.constructor.ConstructorError(
                    "mapping", node.start_mark, "unhashable mapping key", key_node.start_mark
                ) from exc
            if duplicated:
                raise yaml.constructor.ConstructorError(
                    "mapping", node.start_mark, f"duplicate key {key!r}", key_node.start_mark
                )
            mapping[key] = self.construct_object(value_node, deep=deep)
        return mapping


@dataclass
class RawStep:
    number: int
    start_line: int  # 1-based heading line
    end_line: int  # 1-based last line of the step
    text: str
    body: list[tuple[int, str]] = field(default_factory=list)  # (0-based line index, raw line)


@dataclass
class ParsedStep:
    number: int
    step_id: str
    start_line: int
    end_line: int
    text: str
    kind: str  # "structured" | "natural"
    payload: dict[str, Any] | None = None
    prose: str | None = None
    review_required: bool = False


@dataclass
class ParsedCase:
    title: str
    dsl_version: str
    mode: str  # "standard" | "legacy-prd-v1"
    tags: list[str] = field(default_factory=list)
    variables: dict[str, dict[str, Any]] = field(default_factory=dict)
    defaults: dict[str, Any] = field(default_factory=lambda: {"timeout_ms": 10_000})
    steps: list[ParsedStep] = field(default_factory=list)
    source_digest: str = ""


def safe_yaml(text: str, *, line: int) -> Any:
    """Restricted YAML: no anchors/aliases, no custom tags, bounded nesting, unique keys."""
    try:
        depth = 0
        for event in yaml.parse(text, Loader=yaml.SafeLoader):
            if isinstance(event, yaml.AliasEvent) or getattr(event, "anchor", None):
                raise DslError("DSL_YAML_ALIAS_FORBIDDEN", "YAML anchors and aliases are not supported.", line=line)
            if (
                isinstance(event, yaml.ScalarEvent)
                and event.tag
                and not str(event.tag).startswith("tag:yaml.org,2002:")
            ):
                raise DslError("DSL_YAML_TAG_FORBIDDEN", f"Custom YAML tag '{event.tag}' is not supported.", line=line)
            if isinstance(event, (yaml.MappingStartEvent, yaml.SequenceStartEvent)):
                depth += 1
                if depth > 32:
                    raise DslError("DSL_YAML_TOO_DEEP", "YAML nesting exceeds 32 levels.", line=line)
            elif isinstance(event, (yaml.MappingEndEvent, yaml.SequenceEndEvent)):
                depth -= 1
        # UniqueKeyLoader subclasses SafeLoader and the walker above refuses unknown tags, so no object is built.
        return yaml.load(text, Loader=UniqueKeyLoader)  # noqa: S506
    except DslError:
        raise
    except yaml.YamlError as exc:
        offset = getattr(getattr(exc, "problem_mark", None), "line", 0) or 0
        raise DslError("DSL_YAML_INVALID", "YAML syntax is invalid.", line=line + offset) from exc


def parse_markdown(
    markdown: str,
    *,
    max_bytes: int = 262_144,
    max_steps: int = 200,
    max_field_bytes: int = 16_384,
) -> tuple[ParsedCase, DiagnosticList]:
    """Structure-level parse. Layout problems that make the document unreadable raise DslError;
    step-level problems are collected as diagnostics so the API can report all of them at once."""
    encoded = markdown.encode("utf-8")
    if len(encoded) > max_bytes:
        raise DslError("CASE_TOO_LARGE", f"Markdown exceeds {max_bytes} bytes.", line=1)
    normalized = markdown.replace("\r\n", "\n")
    lines = normalized.split("\n")
    diagnostics = DiagnosticList()

    front_matter, content_start = _parse_front_matter(lines)
    title, title_line = _parse_title(lines, content_start)

    dsl_version = str(front_matter.get("dsl_version", DSL_VERSION))
    if front_matter and dsl_version not in (DSL_VERSION, LEGACY_DSL_VERSION):
        raise DslError("DSL_VERSION_UNSUPPORTED", f"dsl_version '{dsl_version}' is not supported.", line=2)

    raw_steps = _split_steps(lines, content_start, diagnostics)
    if not raw_steps:
        raise DslError("CASE_STEPS_REQUIRED", "Add at least one '## Step N' heading.", line=max(title_line, 1))
    if len(raw_steps) > max_steps:
        raise DslError("CASE_STEPS_REQUIRED", f"Case has {len(raw_steps)} steps; the limit is {max_steps}.", line=1)

    numbers = [step.number for step in raw_steps]
    duplicates = {number for number in numbers if numbers.count(number) > 1}
    if duplicates:
        offender = min(duplicates)
        line = next(step.start_line for step in raw_steps if step.number == offender)
        raise DslError("STEP_NUMBER_DUPLICATED", f"Step {offender} appears more than once.", line=line)
    if numbers != list(range(1, len(numbers) + 1)):
        raise DslError(
            "STEP_SEQUENCE_INVALID", "Step numbers must start at 1 and increase by one.", line=raw_steps[0].start_line
        )

    mode = _detect_mode(front_matter, raw_steps)
    if mode == "legacy-prd-v1":
        diagnostics.add(
            Diagnostic.build(
                "DSL_VERSION_UNSUPPORTED",
                "Document was parsed with the legacy-prd-v1 compatibility parser",
                severity="WARNING",
                start_line=raw_steps[0].start_line,
                suggestion='Wrap each step body in a ```yaml block and add front matter with dsl_version: "1.0"',
            )
        )

    steps: list[ParsedStep] = []
    for position, raw in enumerate(raw_steps, start=1):
        parsed = _build_step(raw, position, mode, max_field_bytes, diagnostics)
        if parsed is not None:
            steps.append(parsed)

    return (
        ParsedCase(
            title=title,
            dsl_version=dsl_version,
            mode=mode,
            tags=_parse_tags(front_matter),
            variables=_parse_variables(front_matter),
            defaults=_parse_defaults(front_matter),
            steps=steps,
            source_digest="sha256:" + hashlib.sha256(normalized.encode("utf-8")).hexdigest(),
        ),
        diagnostics,
    )


# --------------------------------------------------------------------------------------
# document layout


def _parse_front_matter(lines: list[str]) -> tuple[dict[str, Any], int]:
    if not lines or lines[0].strip() != "---":
        return {}, 0
    closing = next((index for index in range(1, len(lines)) if lines[index].strip() == "---"), None)
    if closing is None:
        raise DslError("FRONT_MATTER_UNCLOSED", "Front matter needs a closing '---'.", line=1)
    payload = safe_yaml("\n".join(lines[1:closing]), line=2)
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        raise DslError("FRONT_MATTER_INVALID", "Front matter must be a YAML mapping.", line=2)
    unknown = set(payload) - FRONT_MATTER_FIELDS
    if unknown:
        raise DslError(
            "FRONT_MATTER_FIELD_UNKNOWN",
            f"Unsupported front-matter field '{sorted(unknown)[0]}'.",
            line=2,
            field_name=sorted(unknown)[0],
        )
    return payload, closing + 1


def _parse_title(lines: list[str], content_start: int) -> tuple[str, int]:
    titles = [(index, match.group(1)) for index, line in enumerate(lines) if (match := TOP_LEVEL_TITLE.match(line))]
    if len(titles) != 1:
        raise DslError(
            "CASE_TITLE_REQUIRED", "Add exactly one non-empty '# Case title' heading.", line=content_start + 1
        )
    index, text = titles[0]
    return text.strip(), index + 1


def _split_steps(lines: list[str], content_start: int, diagnostics: DiagnosticList) -> list[RawStep]:
    steps: list[RawStep] = []
    index = content_start
    total = len(lines)
    while index < total:
        heading = STEP_HEADING.fullmatch(lines[index].strip())
        if not heading:
            if re.match(r"^##\s+Step\b", lines[index], re.IGNORECASE):
                raise DslError("STEP_HEADING_INVALID", "Use a heading such as '## Step 1'.", line=index + 1)
            index += 1
            continue
        start = index
        index += 1
        body: list[tuple[int, str]] = []
        while index < total:
            if STEP_HEADING.fullmatch(lines[index].strip()):
                break
            if OTHER_HEADING.match(lines[index]):
                diagnostics.add(
                    Diagnostic.build(
                        "DSL_STEP_AMBIGUOUS",
                        f"Non-step heading '{lines[index].strip()}' closes the step; its text is context, not a step",
                        severity="INFO",
                        start_line=index + 1,
                    )
                )
                break
            body.append((index, lines[index]))
            index += 1
        while body and not body[-1][1].strip():
            body.pop()
        end_line = body[-1][0] + 1 if body else start + 1
        steps.append(
            RawStep(
                number=int(heading.group(1)),
                start_line=start + 1,
                end_line=end_line,
                text="\n".join(lines[start:end_line]).rstrip(),
                body=body,
            )
        )
    return steps


def _has_yaml_fence(body: list[tuple[int, str]]) -> bool:
    return any((match := FENCE.match(line)) and match.group(1).lower() == "yaml" for _, line in body)


def _detect_mode(front_matter: dict[str, Any], steps: list[RawStep]) -> str:
    declared = front_matter.get("dsl_version")
    if declared == LEGACY_DSL_VERSION:
        return "legacy-prd-v1"
    if declared:
        return "standard"
    if any(_has_yaml_fence(step.body) for step in steps):
        return "standard"
    if any(_legacy_field_line(line) for step in steps for _, line in step.body):
        return "legacy-prd-v1"
    return "standard"


def _build_step(
    raw: RawStep, position: int, mode: str, max_field_bytes: int, diagnostics: DiagnosticList
) -> ParsedStep | None:
    step_id = f"s{position}"
    yaml_blocks: list[tuple[int, list[str]]] = []
    prose: list[str] = []
    open_block: tuple[int, list[str]] | None = None
    for index, line in raw.body:
        match = FENCE.match(line)
        if open_block is not None:
            if line.strip() == "```":
                yaml_blocks.append(open_block)
                open_block = None
                continue
            open_block[1].append(line)
            continue
        if match:
            if match.group(1).lower() != "yaml":
                diagnostics.add(
                    Diagnostic.build(
                        "DSL_FENCE_INVALID",
                        "Steps may contain one ```yaml block.",
                        step_id=step_id,
                        start_line=index + 1,
                        suggestion="Change the fence language to yaml",
                    )
                )
                return None
            open_block = (index + 1, [])
            continue
        if line.strip():
            prose.append(line.strip())
    if open_block is not None:
        diagnostics.add(
            Diagnostic.build("DSL_FENCE_UNCLOSED", "Unclosed YAML block.", step_id=step_id, start_line=raw.start_line)
        )
        return None
    if len(yaml_blocks) > 1 or (yaml_blocks and prose):
        diagnostics.add(
            Diagnostic.build(
                "DSL_STEP_AMBIGUOUS",
                "Use one YAML block or one natural-language description, not both.",
                step_id=step_id,
                start_line=raw.start_line,
                end_line=raw.end_line,
            )
        )
        return None

    if yaml_blocks:
        block_start, block_lines = yaml_blocks[0]
        text = "\n".join(block_lines)
        if len(text.encode("utf-8")) > max_field_bytes:
            diagnostics.add(
                Diagnostic.build(
                    "CASE_TOO_LARGE",
                    f"Step definition exceeds {max_field_bytes} bytes",
                    step_id=step_id,
                    start_line=block_start,
                )
            )
            return None
        try:
            payload = safe_yaml(text, line=block_start)
        except DslError as exc:
            exc.diagnostic.step_id = step_id
            diagnostics.add(exc.diagnostic)
            return None
        if not isinstance(payload, dict):
            diagnostics.add(
                Diagnostic.build(
                    "DSL_AMBIGUOUS_SYNTAX",
                    "Each step's YAML root must be a mapping.",
                    step_id=step_id,
                    start_line=block_start,
                )
            )
            return None
        return ParsedStep(
            number=raw.number,
            step_id=step_id,
            start_line=raw.start_line,
            end_line=raw.end_line,
            text=raw.text,
            kind="structured",
            payload=payload,
        )

    if not prose:
        diagnostics.add(
            Diagnostic.build(
                "STEP_EMPTY",
                "Step has no YAML definition or description.",
                step_id=step_id,
                start_line=raw.start_line,
                end_line=raw.end_line,
            )
        )
        return None
    if mode == "legacy-prd-v1" and any(_legacy_field_line(line) for line in prose):
        folded = legacy_fold(raw, step_id, diagnostics)
        if folded is None:
            return None
        return ParsedStep(
            number=raw.number,
            step_id=step_id,
            start_line=raw.start_line,
            end_line=raw.end_line,
            text=raw.text,
            kind="structured",
            payload=folded,
        )
    return ParsedStep(
        number=raw.number,
        step_id=step_id,
        start_line=raw.start_line,
        end_line=raw.end_line,
        text=raw.text,
        kind="natural",
        prose=" ".join(prose),
    )


def _legacy_field_line(line: str) -> bool:
    stripped = line.strip()
    match = key_line(stripped)
    return bool(match and match.group(1) in LEGACY_FIELDS)


def _collect_legacy_block(entries: list[tuple[int, str]], start: int) -> tuple[list[tuple[int, str]], int]:
    """Consecutive sub-field lines (and their scalar continuations) that belong to one block."""
    block: list[tuple[int, str]] = []
    cursor = start
    while cursor < len(entries):
        raw = entries[cursor][1]
        if not raw.strip():
            cursor += 1
            continue
        if _legacy_field_line(raw):
            break
        match = key_line(raw.strip())
        if match is None:
            block.append(entries[cursor])
            cursor += 1
            continue
        block.append(entries[cursor])
        cursor += 1
        if not match.group(2).strip():
            while cursor < len(entries) and not entries[cursor][1].strip():
                cursor += 1
            if cursor < len(entries) and key_line(entries[cursor][1].strip()) is None:
                block.append(entries[cursor])
                cursor += 1
    return block, cursor


# --------------------------------------------------------------------------------------
# legacy-prd-v1 compatibility (§4.2)


def legacy_fold(raw: RawStep, step_id: str, diagnostics: DiagnosticList) -> dict[str, Any] | None:
    """Fold the original PRD layout into the standard field set, without guessing meaning from indentation."""
    result: dict[str, Any] = {}
    entries = [(index, line) for index, line in raw.body if line.strip()]
    index = 0
    while index < len(entries):
        line_index, line = entries[index]
        stripped = line.strip()
        if stripped.startswith("#"):
            index += 1
            continue
        match = _LEGACY_LINE.match(stripped)
        if not match:
            diagnostics.add(
                Diagnostic.build(
                    "DSL_AMBIGUOUS_SYNTAX",
                    f"Cannot interpret legacy line: {stripped!r}",
                    step_id=step_id,
                    start_line=line_index + 1,
                    suggestion="Rewrite the step as a ```yaml block",
                )
            )
            return None
        field_name, inline = match.group(1), match.group(2).strip()
        if field_name not in LEGACY_FIELDS:
            diagnostics.add(
                Diagnostic.build(
                    "DSL_AMBIGUOUS_SYNTAX",
                    f"Unknown legacy field '{field_name}'",
                    step_id=step_id,
                    start_line=line_index + 1,
                    field_name=field_name,
                )
            )
            return None
        if field_name in result:
            diagnostics.add(
                Diagnostic.build(
                    "DSL_AMBIGUOUS_SYNTAX",
                    f"Field '{field_name}' appears more than once in the step",
                    step_id=step_id,
                    start_line=line_index + 1,
                    field_name=field_name,
                )
            )
            return None
        if inline:
            result[field_name] = inline
            index += 1
            continue
        # The value sits on the next non-empty line, or the field opens a sub-block of its own.
        nxt = index + 1
        while nxt < len(entries) and not entries[nxt][1].strip():
            nxt += 1
        if nxt >= len(entries):
            diagnostics.add(
                Diagnostic.build(
                    "DSL_MISSING_FIELD",
                    f"Legacy field '{field_name}' has no value",
                    step_id=step_id,
                    start_line=line_index + 1,
                    field_name=field_name,
                )
            )
            return None
        candidate = entries[nxt][1].strip()
        candidate_key = key_line(candidate)
        if candidate_key is None:
            if _legacy_field_line(candidate):
                diagnostics.add(
                    Diagnostic.build(
                        "DSL_MISSING_FIELD",
                        f"Legacy field '{field_name}' has no value",
                        step_id=step_id,
                        start_line=line_index + 1,
                        field_name=field_name,
                    )
                )
                return None
            result[field_name] = candidate
            index = nxt + 1
            continue
        block, cursor = _collect_legacy_block(entries, nxt)
        folded_block = _legacy_block(block, step_id, diagnostics)
        if folded_block is None:
            return None
        result[field_name] = folded_block
        index = cursor
        continue
    if "action" not in result:
        diagnostics.add(
            Diagnostic.build(
                "DSL_MISSING_FIELD",
                "Legacy step has no action",
                step_id=step_id,
                start_line=raw.start_line,
                field_name="action",
            )
        )
        return None
    return result


def _legacy_block(block: list[tuple[int, str]], step_id: str, diagnostics: DiagnosticList) -> dict[str, Any] | None:
    """Parse an indented sub-block, collapsing `condition:` + `page_contains:` + value into a standard condition."""
    flat: dict[str, Any] = {}
    for position, (line_index, line) in enumerate(block):
        stripped = line.strip()
        if not stripped:
            continue
        match = key_line(stripped)
        if not match:
            diagnostics.add(
                Diagnostic.build(
                    "DSL_AMBIGUOUS_SYNTAX",
                    f"Cannot interpret legacy sub-line: {stripped!r}",
                    step_id=step_id,
                    start_line=line_index + 1,
                )
            )
            return None
        key, value = match.group(1), match.group(2).strip()
        if key in flat:
            diagnostics.add(
                Diagnostic.build(
                    "DSL_AMBIGUOUS_SYNTAX",
                    f"Duplicate legacy sub-field '{key}'",
                    step_id=step_id,
                    start_line=line_index + 1,
                )
            )
            return None
        if not value and position + 1 < len(block):
            following = block[position + 1][1].strip()
            if following and key_line(following) is None:
                value = following
                block.pop(position + 1)
        flat[key] = value
    if set(flat) == {"page_contains"}:
        return {"kind": "page_contains", "expected": flat["page_contains"] or ""}
    if len(flat) == 1 and set(flat) & {"url_equals", "url_contains", "text_equals", "value_equals"}:
        kind = next(iter(flat))
        return {"kind": kind, "expected": flat[kind] or ""}
    return flat


# --------------------------------------------------------------------------------------
# front matter helpers


def _parse_tags(front_matter: dict[str, Any]) -> list[str]:
    tags = front_matter.get("tags", [])
    if tags is None:
        return []
    if not isinstance(tags, list) or any(not isinstance(item, str) for item in tags):
        raise DslError("FRONT_MATTER_INVALID", "tags must be a list of strings.", line=2, field_name="tags")
    return [item.strip() for item in tags if item.strip()]


def _parse_variables(front_matter: dict[str, Any]) -> dict[str, dict[str, Any]]:
    raw = front_matter.get("variables", {})
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise DslError("VARIABLES_INVALID", "Front-matter variables must be a mapping.", line=2, field_name="variables")
    declared: dict[str, dict[str, Any]] = {}
    for name, definition in raw.items():
        if definition is None:
            definition = {}
        if not isinstance(definition, dict):
            raise DslError(
                "VARIABLE_DEFINITION_INVALID",
                f"Variable '{name}' must be a mapping with a supported type.",
                line=2,
                field_name=f"variables.{name}",
            )
        if set(definition) - {"type", "required"}:
            raise DslError(
                "VARIABLE_DEFINITION_INVALID",
                f"Variable '{name}' has unsupported fields.",
                line=2,
                field_name=f"variables.{name}",
            )
        if definition.get("type", "string") not in {"string", "integer", "number", "boolean"}:
            raise DslError(
                "VARIABLE_DEFINITION_INVALID",
                f"Variable '{name}' has an unsupported type.",
                line=2,
                field_name=f"variables.{name}.type",
            )
        declared[str(name)] = {
            "type": definition.get("type", "string"),
            "required": bool(definition.get("required", False)),
        }
    return declared


def _parse_defaults(front_matter: dict[str, Any]) -> dict[str, Any]:
    raw = front_matter.get("defaults", {})
    if raw is None:
        raw = {}
    if not isinstance(raw, dict) or set(raw) - {"timeout_ms"}:
        raise DslError("DEFAULTS_INVALID", "defaults supports timeout_ms only.", line=2, field_name="defaults")
    timeout = raw.get("timeout_ms", 10_000)
    if not isinstance(timeout, int) or isinstance(timeout, bool) or not 0 < timeout <= 120_000:
        raise DslError(
            "DEFAULT_TIMEOUT_INVALID", "defaults.timeout_ms must be an integer between 1 and 120000.", line=2
        )
    return {"timeout_ms": timeout}
