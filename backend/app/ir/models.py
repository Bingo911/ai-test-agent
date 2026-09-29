"""Test IR contract (§5). Immutable, versioned, schema- and semantically-validated execution contract."""

from __future__ import annotations

import hashlib
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator

IR_VERSION = "1.0"
DSL_VERSION = "1.0"
LEGACY_DSL_VERSION = "legacy-prd-v1"
COMPILER_VERSION = "1.0.0"
PROMPT_VERSION = "compiler-1.0"

Name = Annotated[str, StringConstraints(min_length=1, max_length=512)]
StepId = Annotated[str, StringConstraints(pattern=r"^s[1-9][0-9]*$")]
PositiveMs = Annotated[int, Field(gt=0, le=120_000)]
Digest = Annotated[str, StringConstraints(pattern=r"^sha256:[0-9a-f]{64}$")]

ACTION_NAMES = (
    "open",
    "click",
    "input",
    "clear",
    "upload",
    "wait",
    "assert",
    "screenshot",
)
CONDITION_KINDS = (
    "page_contains",
    "url_equals",
    "url_contains",
    "element_visible",
    "element_hidden",
    "text_equals",
    "value_equals",
)
LOCATOR_STRATEGIES = ("css", "role", "text", "xpath")

#: Actions that may commit a side effect on the target site (§7.4).
SIDE_EFFECT_ACTIONS = frozenset({"open", "click", "input", "clear", "upload"})


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Source(StrictModel):
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    text: str

    @model_validator(mode="after")
    def _range(self) -> Source:
        if self.end_line < self.start_line:
            raise ValueError("source.end_line must not precede start_line")
        return self


class LocatorCandidate(StrictModel):
    strategy: Literal["css", "role", "text", "xpath"]
    selector: str | None = None
    role: str | None = None
    name: str | None = None
    text: str | None = None
    exact: bool | None = None

    @model_validator(mode="after")
    def _fields_match_strategy(self) -> LocatorCandidate:
        requirements: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
            "css": (("selector",), ("role", "name", "text")),
            "xpath": (("selector",), ("role", "name", "text")),
            "role": (("role", "name"), ("selector", "text")),
            "text": (("text",), ("selector", "role")),
        }
        required, forbidden = requirements[self.strategy]
        if any(getattr(self, key) in (None, "") for key in required):
            raise ValueError(f"{self.strategy} candidate requires {', '.join(required)}")
        if any(getattr(self, key) is not None for key in forbidden):
            raise ValueError(f"{self.strategy} candidate must not carry {', '.join(forbidden)}")
        return self


class Target(StrictModel):
    description: Name
    type: str | None = None
    candidates: list[LocatorCandidate] = Field(default_factory=list, max_length=12)
    allow_vision: bool = False

    @field_validator("type")
    @classmethod
    def _known_type(cls, value: str | None) -> str | None:
        allowed = {"input", "button", "link", "checkbox", "radio", "select", "textarea", "text", "file", "other"}
        if value is not None and value not in allowed:
            raise ValueError(f"unknown control type '{value}'")
        return value


class LiteralValue(StrictModel):
    kind: Literal["literal"]
    value: str | int | float | bool


class VariableValue(StrictModel):
    kind: Literal["variable"]
    namespace: Literal["env", "vars"]
    key: Name


class SecretValue(StrictModel):
    kind: Literal["secret"]
    key: Name


class TemplateValue(StrictModel):
    kind: Literal["template"]
    template: Name


ValueSpec = Annotated[
    LiteralValue | VariableValue | SecretValue | TemplateValue,
    Field(discriminator="kind"),
]


class Condition(StrictModel):
    kind: Literal[
        "page_contains",
        "url_equals",
        "url_contains",
        "element_visible",
        "element_hidden",
        "text_equals",
        "value_equals",
    ]
    expected: ValueSpec | None = None
    target: Target | None = None

    @model_validator(mode="after")
    def _fields_match_kind(self) -> Condition:
        expects_text = self.kind in {"page_contains", "url_equals", "url_contains", "text_equals", "value_equals"}
        expects_target = self.kind in {"element_visible", "element_hidden", "text_equals", "value_equals"}
        if expects_text != (self.expected is not None):
            raise ValueError(f"condition '{self.kind}' expected field mismatch")
        if expects_target != (self.target is not None):
            raise ValueError(f"condition '{self.kind}' target field mismatch")
        return self


class HumanPolicy(StrictModel):
    mode: Literal["disabled", "on_challenge", "before"]
    reason: str | None = None
    resume_condition: Condition | None = None

    @model_validator(mode="after")
    def _challenge_must_be_verifiable(self) -> HumanPolicy:
        if self.mode == "on_challenge" and self.resume_condition is None:
            raise ValueError("on_challenge requires a deterministic resume_condition")
        if self.mode != "on_challenge" and self.resume_condition is not None:
            raise ValueError("only on_challenge may carry a resume_condition")
        return self


class StepBase(StrictModel):
    id: StepId
    source: Source
    timeout_ms: PositiveMs | None = None
    human_policy: HumanPolicy | None = None


class OpenStep(StepBase):
    action: Literal["open"]
    url: ValueSpec
    wait_until: Literal["load", "domcontentloaded", "networkidle", "commit"] = "domcontentloaded"


class ClickStep(StepBase):
    action: Literal["click"]
    target: Target


class ClearStep(StepBase):
    action: Literal["clear"]
    target: Target


class InputStep(StepBase):
    action: Literal["input"]
    target: Target
    value: ValueSpec


class UploadStep(StepBase):
    action: Literal["upload"]
    target: Target
    attachment_ids: list[Name] = Field(min_length=1, max_length=20)


class WaitStep(StepBase):
    action: Literal["wait"]
    condition: Condition | None = None
    duration_ms: PositiveMs | None = None

    @model_validator(mode="after")
    def _exactly_one_mode(self) -> WaitStep:
        if (self.condition is None) == (self.duration_ms is None):
            raise ValueError("wait requires exactly one of condition and duration_ms")
        return self


class AssertStep(StepBase):
    action: Literal["assert"]
    condition: Condition


class ScreenshotStep(StepBase):
    action: Literal["screenshot"]
    name: str | None = None
    full_page: bool = False


Step = Annotated[
    OpenStep | ClickStep | ClearStep | InputStep | UploadStep | WaitStep | AssertStep | ScreenshotStep,
    Field(discriminator="action"),
]

STEP_CLASSES: dict[str, type[StepBase]] = {
    "open": OpenStep,
    "click": ClickStep,
    "clear": ClearStep,
    "input": InputStep,
    "upload": UploadStep,
    "wait": WaitStep,
    "assert": AssertStep,
    "screenshot": ScreenshotStep,
}


class VariableDefinition(StrictModel):
    type: Literal["string", "integer", "number", "boolean"] = "string"
    required: bool = False


class CompilerInfo(StrictModel):
    version: Name
    mode: Literal["deterministic", "ai_assisted"]


class Defaults(StrictModel):
    timeout_ms: PositiveMs = 10_000


class TestIR(StrictModel):
    ir_version: Literal["1.0"]
    case_revision_id: Name
    source_digest: Digest
    compiler: CompilerInfo
    variables: dict[str, VariableDefinition] = Field(default_factory=dict)
    defaults: Defaults = Field(default_factory=Defaults)
    steps: list[Step] = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def _ids_unique_and_ordered(self) -> TestIR:
        ids = [step.id for step in self.steps]
        if len(ids) != len(set(ids)):
            raise ValueError("step ids must be unique")
        numbers = [int(step.id[1:]) for step in self.steps]
        if numbers != sorted(numbers):
            raise ValueError("step ids must stay in document order")
        return self

    def digest(self) -> str:
        return "sha256:" + hashlib.sha256(canonical_json(self.model_dump(mode="json")).encode("utf-8")).hexdigest()

    def step_by_id(self, step_id: str) -> StepBase | None:
        for step in self.steps:
            if step.id == step_id:
                return step
        return None


def canonical_json(payload: Any) -> str:
    import json

    return json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def ir_from_payload(payload: dict[str, Any]) -> TestIR:
    return TestIR.model_validate(payload)
