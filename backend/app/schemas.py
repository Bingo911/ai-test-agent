from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

Name = Annotated[str, StringConstraints(min_length=1, max_length=512)]
StepId = Annotated[str, StringConstraints(pattern=r"^s[1-9][0-9]*$")]
PositiveMs = Annotated[int, Field(gt=0, le=120000)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Source(StrictModel):
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    text: str

    @model_validator(mode="after")
    def range_is_valid(self) -> "Source":
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
    def strategy_fields(self) -> "LocatorCandidate":
        valid = {
            "css": self.selector is not None and self.role is None and self.text is None,
            "xpath": self.selector is not None and self.role is None and self.text is None,
            "role": self.role is not None and self.selector is None and self.text is None,
            "text": self.text is not None and self.selector is None and self.role is None,
        }
        if not valid[self.strategy]:
            raise ValueError(f"locator fields do not match '{self.strategy}'")
        return self


class Target(StrictModel):
    description: Name
    type: str | None = None
    candidates: list[LocatorCandidate] = Field(default_factory=list)
    allow_vision: bool = False


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


ValueSpec = Annotated[LiteralValue | VariableValue | SecretValue | TemplateValue, Field(discriminator="kind")]


class Condition(StrictModel):
    kind: Literal[
        "page_contains", "url_equals", "url_contains", "element_visible",
        "element_hidden", "text_equals", "value_equals",
    ]
    expected: ValueSpec | None = None
    target: Target | None = None

    @model_validator(mode="after")
    def fields_match_kind(self) -> "Condition":
        expects_text = self.kind in {"page_contains", "url_equals", "url_contains", "text_equals", "value_equals"}
        expects_target = self.kind in {"element_visible", "element_hidden", "text_equals", "value_equals"}
        if expects_text != (self.expected is not None) or expects_target != (self.target is not None):
            raise ValueError("condition fields must match its kind")
        return self


class HumanPolicy(StrictModel):
    mode: Literal["disabled", "on_challenge", "before"]
    reason: str | None = None
    resume_condition: Condition | None = None

    @model_validator(mode="after")
    def challenge_must_be_verifiable(self) -> "HumanPolicy":
        if self.mode == "on_challenge" and self.resume_condition is None:
            raise ValueError("on_challenge requires a deterministic resume_condition")
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
    action: Literal["click", "clear"]
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
    def exactly_one_mode(self) -> "WaitStep":
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
    OpenStep | ClickStep | InputStep | UploadStep | WaitStep | AssertStep | ScreenshotStep,
    Field(discriminator="action"),
]


class VariableDefinition(StrictModel):
    type: Literal["string", "integer", "number", "boolean"]
    required: bool = False


class Compiler(StrictModel):
    version: Name
    mode: Literal["deterministic", "ai_assisted"]


class TestIR(StrictModel):
    ir_version: Literal["1.0"]
    case_revision_id: str | None = None
    source_digest: Annotated[str, StringConstraints(pattern=r"^sha256:[0-9a-f]{64}$")]
    compiler: Compiler
    variables: dict[str, VariableDefinition] = Field(default_factory=dict)
    defaults: dict[str, PositiveMs] = Field(default_factory=lambda: {"timeout_ms": 10000})
    steps: list[Step] = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def ids_are_unique(self) -> "TestIR":
        ids = [step.id for step in self.steps]
        if len(ids) != len(set(ids)):
            raise ValueError("step ids must be unique")
        return self
