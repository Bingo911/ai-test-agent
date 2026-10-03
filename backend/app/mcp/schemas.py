"""The MCP wire contract: the one result envelope every tool answers with (§6.1).

`Envelope` is the only shape a tool may answer with. Both channels carry the same bytes on purpose:
a client that reads only `content` still sees the full result, and a client that reads
`structuredContent` still fits the same budget, so neither path can smuggle past the size ceiling.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from mcp_types import CallToolResult, TextContent
from pydantic import BaseModel, ConfigDict, Field

from .errors import AdapterCode, NextAction, ToolFailure

SCHEMA_VERSION = "1.0"


class ErrorBody(BaseModel):
    code: str
    message: str
    retryable: bool = False
    retry_after_ms: int | None = None
    details: dict[str, Any] = Field(default_factory=dict)
    next_action: NextAction = NextAction.none


class Envelope(BaseModel):
    """The completion envelope every tool returns, success and failure alike (§6.1)."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = SCHEMA_VERSION
    ok: bool
    request_id: str
    data: dict[str, Any] | None = None
    error: ErrorBody | None = None
    truncated: bool = False
    next_cursor: str | None = None

    @classmethod
    def success(
        cls,
        request_id: str,
        data: BaseModel | dict[str, Any] | None,
        *,
        truncated: bool = False,
        next_cursor: str | None = None,
    ) -> Envelope:
        payload = data.model_dump(mode="json") if isinstance(data, BaseModel) else (data or {})
        return cls(ok=True, request_id=request_id, data=payload, truncated=truncated, next_cursor=next_cursor)

    @classmethod
    def failure(cls, request_id: str, failure: ToolFailure) -> Envelope:
        return cls(
            ok=False,
            request_id=request_id,
            data=None,
            error=ErrorBody(
                code=failure.code,
                message=failure.message,
                retryable=failure.retryable,
                retry_after_ms=failure.retry_after_ms,
                details=failure.details,
                next_action=failure.next_action,
            ),
        )


@dataclass(frozen=True)
class Answer:
    """A tool's successful payload plus the two envelope fields that belong to a page (§6.1, §6.6).

    `next_cursor` lives on the envelope only: repeating it inside `data` would give a client two sources
    of truth for one page. `data` is a `Wire` projection or a plain mapping; a `Wire` is rendered with
    `exclude_unset` here, because a field the policy hid must be *absent*, and dumping every declared
    field instead would present the hidden one as `null` - which reads as "there is none".

    `bulk` is the answer's way of saying it was assembled because the caller explicitly asked for one big
    object - a case's full Markdown, an artifact's IR. Those are bounded by the hard response ceiling
    alone; everything else, list pages included, also has to fit the smaller metadata budget (§11).
    """

    data: Any = None
    next_cursor: str | None = None
    truncated: bool = False
    bulk: bool = False

    def __post_init__(self) -> None:
        payload = self.data
        wire = getattr(payload, "wire", None)
        if callable(wire):
            object.__setattr__(self, "data", wire())

    def envelope(self, request_id: str) -> Envelope:
        return Envelope.success(
            request_id, self.data, truncated=self.truncated, next_cursor=self.next_cursor
        )


def envelope_json(envelope: Envelope) -> str:
    """Compact, key-ordered JSON: the same string goes into both channels and into the size count."""
    return json.dumps(envelope.model_dump(mode="json"), ensure_ascii=False, separators=(",", ":"))


def call_tool_result(envelope: Envelope) -> CallToolResult:
    """Render one envelope as a protocol result, with `ok` driving `isError` (§6.1).

    A test that merely failed is `ok=true` data; only an operation that could not be completed is
    `isError=true`, otherwise a client is taught to treat a red report as a broken tool.
    """
    text = envelope_json(envelope)
    return CallToolResult(
        content=[TextContent(type="text", text=text)],
        structured_content=envelope.model_dump(mode="json"),
        is_error=not envelope.ok,
    )


def serialized_size(result: CallToolResult) -> int:
    """UTF-8 bytes of the whole result, counting both channels, so text cannot double the budget (§11)."""
    return len(result.model_dump_json(by_alias=True).encode("utf-8"))


def over_budget(result: CallToolResult, budget: int) -> ToolFailure:
    """The refusal for an answer that would not fit, carrying the two numbers that say by how much (§11)."""
    return ToolFailure(
        AdapterCode.RESULT_TOO_LARGE,
        "The response would exceed the byte budget for this tool",
        retryable=True,
        details={"limit_bytes": budget, "actual_bytes": serialized_size(result)},
        next_action=NextAction.narrow_request,
    )


def oversized(result: CallToolResult, request_id: str, *, budget: int) -> CallToolResult:
    """Replace an over-budget answer with a bounded refusal rather than truncating runnable content (§11)."""
    if serialized_size(result) <= budget:
        return result
    return call_tool_result(Envelope.failure(request_id, over_budget(result, budget)))


# --------------------------------------------------------------------------------------
# the `data` shapes, one per tool (§6.6)
#
# Every field is declared, and the two ways a field can be missing stay two different things:
# a *nullable* field is passed explicitly as `None` and serialises as `null`, a *policy-hidden*
# field is never passed at all, so `exclude_unset` leaves it out of the object. Declaring them both
# as plain optional fields is what makes the difference a property of how the adapter builds the
# object rather than of a comment - and it is why nothing here may post-process the dumped dict.
# --------------------------------------------------------------------------------------


class Wire(BaseModel):
    """Base for the answer DTOs: fixed field names, no extras, and unset means "not present"."""

    model_config = ConfigDict(extra="forbid")

    def wire(self) -> dict[str, Any]:
        """`exclude_unset` is the whole point, and it applies to the nested items too."""
        return self.model_dump(mode="json", exclude_unset=True)


def render(models: Iterable[Wire]) -> list[dict[str, Any]]:
    """A page of items as projections, so a hidden field is absent from each item, not null in it."""
    return [model.wire() for model in models]


class TenantRef(Wire):
    """One tenant the caller belongs to - the answer to "which tenant should I select" (§6.2)."""

    tenant_id: str
    role: str | None = None
    name: str | None = None
    display_name: str | None = None


class ProjectAuthority(Wire):
    """What the caller may do in one project, as the caller's own view of it (§5.4)."""

    project_id: str
    mcp_enabled: bool
    permissions: list[str] = Field(default_factory=list)
    role: str | None = None
    name: str | None = None
    display_name: str | None = None


class ContextView(Wire):
    """`aita_get_context`: the caller, the tenants or projects it asked about, and the bounds (§6.6)."""

    actor_id: str
    items: list[dict[str, Any]] = Field(default_factory=list)
    capabilities: dict[str, Any] = Field(default_factory=dict)
    limits: dict[str, Any] = Field(default_factory=dict)
    tenant_id: str | None = None


class ProjectRef(Wire):
    """`aita_list_projects`: the id and the gate are always there; the names are a policy decision."""

    project_id: str
    mcp_enabled: bool
    name: str | None = None
    display_name: str | None = None


class ProjectPage(Wire):
    items: list[dict[str, Any]] = Field(default_factory=list)


class EnvironmentRef(Wire):
    """`aita_list_environments`: never the config, which can carry host names and secret bindings (§6.2)."""

    environment_id: str
    current_revision_id: str | None
    row_version: int
    name: str | None = None
    revision_version: int | None = None


class EnvironmentPage(Wire):
    items: list[dict[str, Any]] = Field(default_factory=list)


class CaseRef(Wire):
    """`aita_list_cases`: enough to pick a case and reason about its compilation state (§6.2)."""

    case_id: str
    row_version: int
    compile_status: str | None
    revision_id: str | None = None
    source_digest: str | None = None
    name: str | None = None
    title: str | None = None
    tags: list[str] | None = None
    archived: bool | None = None


class CasePage(Wire):
    items: list[dict[str, Any]] = Field(default_factory=list)


class CaseView(Wire):
    """`aita_get_case`: `content_available` explains readability; an empty string never does (§6.6)."""

    case_id: str
    project_id: str
    row_version: int
    content_available: bool
    compilation_lookup: dict[str, Any] = Field(default_factory=dict)
    current_revision_id: str | None
    source_digest: str | None = None
    name: str | None = None
    title: str | None = None
    description: str | None = None
    tags: list[str] | None = None
    dsl_version: str | None = None
    revision_version: int | None = None
    markdown: str | None = None
    markdown_bytes: int | None = None


class CaseWriteReceipt(Wire):
    """`aita_create_case` / `aita_add_case_revision`: ids, a digest and the version to write against next.

    There is no `markdown` field and no name, title or tag (§6.6): the caller supplied that text seconds
    ago, and echoing it back would put case content on the wire through a channel the content policy was
    never consulted about.
    """

    case_id: str
    revision_id: str
    revision_no: int
    row_version: int
    source_digest: str
    compilation_lookup: dict[str, Any] = Field(default_factory=dict)


class CompileReceipt(Wire):
    """`aita_compile_case_revision`: the artifact that will hold the result, and when to ask about it.

    `compiler_mode` is the mode this request was *accepted* under, which is what lets a caller notice a
    downgrade: `use_ai=true` answers `ai_assisted` or it answers with a refusal, never with a quiet
    `deterministic` (§6.4). What the compile actually ran as is `aita_get_compilation`'s business, since
    the worker is the one that reaches a model.
    """

    compile_artifact_id: str
    revision_id: str
    compile_status: str
    compiler_mode: str
    compiler_version: str
    recommended_poll_after_ms: int | None = None


class RunReceipt(Wire):
    """`aita_run_test`: the run that now exists, the inputs it was frozen with, and where to watch it (§6.6).

    Nothing here is a status the caller has to take on trust: the run's own state is read from the row this
    transaction created, so a replay an hour later reports the run as it is rather than as queued. The two
    fields that describe the *request* rather than the row are `effective_server_ai`, which is the AI intent
    this call was accepted under (§6.4), and `worker_available`, which is only a heartbeat - the queue is the
    promise, the browser is not (§13.5).
    """

    execution_id: str
    tenant_id: str
    project_id: str
    status: str
    outcome: str | None
    compile_artifact_id: str
    ir_digest: str | None
    environment_revision_id: str | None
    evidence_mode: str
    effective_server_ai: bool
    worker_available: bool
    recommended_poll_after_ms: int | None = None
    links: dict[str, Any] = Field(default_factory=dict)


class CancelReceipt(Wire):
    """`aita_cancel_execution`: whether this stop was accepted, and what the run says now (§6.6).

    `cancel_requested` is the decision of the call that first used this key and a replay keeps it, while
    `status`/`outcome`/`terminal` are read from the row: asking to stop a run that had already finished does
    not cancel anything, and cancelling one that was running does not make it cancelled until the worker that
    holds the lease says so.
    """

    execution_id: str
    cancel_requested: bool
    status: str
    terminal: bool
    outcome: str | None
    recommended_poll_after_ms: int | None = None


class CompilationView(Wire):
    """`aita_get_compilation`: the query result, whether or not an artifact exists yet (§6.3)."""

    lookup_status: str
    revision_id: str
    executable: bool
    content_available: bool
    review_required: bool = False
    compile_artifact_id: str | None
    compile_status: str | None
    ir_digest: str | None
    diagnostics: list[dict[str, Any]] = Field(default_factory=list)
    review_items: list[dict[str, Any]] = Field(default_factory=list)
    review_url: str | None = None
    compiler_mode: str | None = None
    source_digest: str | None = None
    recommended_poll_after_ms: int | None = None
    #: §6.6 - `ir` is only ever present for an explicit `include_ir` the project's policy agreed to, so it
    #: is left unset otherwise rather than answered as `null`: absent means "not in this answer".
    ir: dict[str, Any] | None = None


class StepRef(Wire):
    """One step. The seven fields a closed project may still show are the first six plus `artifact_refs` (§6.2)."""

    step_id: str
    step_no: int
    action: str
    status: str
    duration_ms: int | None
    error_code: str | None
    artifact_refs: list[dict[str, Any]] = Field(default_factory=list)
    description: str | None = None
    locator_attempts: list[dict[str, Any]] | None = None
    expected: str | None = None
    actual: str | None = None
    resume_phase: str | None = None


class StepPage(Wire):
    """`aita_get_execution_steps`: ordered step metadata, with the poll suggestion on the page (§6.6)."""

    execution_id: str
    terminal: bool
    items: list[dict[str, Any]] = Field(default_factory=list)
    recommended_poll_after_ms: int | None = None
    #: §6.6 - a step whose description is *absent* has to say whether that is policy or an empty field, and
    #: this is the page's one chance to say it: there is no per-item flag, and a caller that read the
    #: absence as "this step had no description" would rewrite the case from a wrong premise.
    details_available: bool


class ExecutionView(Wire):
    """`aita_get_execution`: status, counts and readiness, never the run's variables or IR (§6.6)."""

    execution_id: str
    status: str
    terminal: bool
    state_version: int
    step_counts: dict[str, int] = Field(default_factory=dict)
    base_report_ready: bool
    analysis_status: str
    artifact_status: str
    human_required: bool
    project_id: str
    tenant_id: str
    outcome: str | None
    #: §14.1 - this answer has no gated field of its own, so the flag is about the run's *other* reads: it
    #: says whether the step page and the report for this execution will carry details, which is what a
    #: caller uses to decide whether asking them is worth another round trip.
    details_available: bool
    recommended_poll_after_ms: int | None = None
    cleanup_status: str | None = None
    error_code: str | None = None
    evidence_mode: str | None = None
    browser: str | None = None
    compile_artifact_id: str | None = None
    ir_digest: str | None = None
    environment_revision_id: str | None = None
    human_task: dict[str, Any] | None = None
    started_at: str | None = None
    ended_at: str | None = None
    created_at: str | None = None
    active_ms: int | None = None
    human_ms: int | None = None
    links: dict[str, Any] | None = None


class ReportView(Wire):
    """`aita_get_report`: the aggregate counts SQL produced, plus the console entry (§6.6, §8.2)."""

    execution_id: str
    status: str
    terminal: bool
    base_report_ready: bool
    analysis_status: str
    artifact_status: str
    step_counts: dict[str, int] = Field(default_factory=dict)
    failure_step_ids: list[str] = Field(default_factory=list)
    details_available: bool
    project_id: str
    tenant_id: str
    outcome: str | None
    failure_type: str | None
    #: §8.2 - a report's own stable code. It is a code rather than prose, so §14.1 lets it through a closed
    #: project; the step's prose that explains it is the part that needs the policy.
    error_code: str | None = None
    recommended_poll_after_ms: int | None = None
    cleanup_status: str | None = None
    evidence_mode: str | None = None
    links: dict[str, Any] = Field(default_factory=dict)
    artifact_summary: dict[str, Any] | None = None
    #: §8.2 - at most five failure summaries, and only when details are allowed: absent otherwise rather
    #: than empty, because "nothing failed" and "you may not see what failed" are different answers.
    failure_summaries: list[dict[str, Any]] = Field(default_factory=list)
