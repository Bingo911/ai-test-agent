"""JSON Schema export for the IR contract (§5.1) plus version helpers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .models import IR_VERSION, TestIR

SCHEMA_ID = f"https://ai-test-agent.local/contracts/ir-{IR_VERSION}.schema.json"


def ir_json_schema() -> dict[str, Any]:
    schema = TestIR.model_json_schema(ref_template="#/components/schemas/{model}")
    schema["$id"] = SCHEMA_ID
    schema["title"] = "AI Test Agent Test IR"
    schema["x-ir-version"] = IR_VERSION
    return schema


def write_contract(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"ir-{IR_VERSION}.schema.json"
    path.write_text(json.dumps(ir_json_schema(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def validate_ir_version(payload: dict[str, Any]) -> str | None:
    """Return an error message when a stored IR cannot be executed by this build."""
    version = payload.get("ir_version")
    if not isinstance(version, str):
        return "ir_version is missing"
    if version.split(".")[0] != IR_VERSION.split(".")[0]:
        return f"ir_version {version} is incompatible with executor contract {IR_VERSION}"
    return None
