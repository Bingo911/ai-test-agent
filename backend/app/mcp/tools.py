"""The tool adapters: what a caller asks for, in one bounded transaction (§6.2, §8, §9.3, §11).

Every read here follows the same pipeline, which is why the pipeline is written once and the tools are
short: the executor slot is already held by the time the body runs, one transaction is opened for the
page, the project and the policy are re-read *inside* it, the SQL is bounded by the page size, and only
then is anything projected or serialised. A write shares the slot, the transaction-per-call and the
envelope, and adds two things of its own: it is the command transaction with one committer (§9.3), and
the receipt only ever names what was created. The order is the contract:

* authorisation before paging, and paging before projection - a page trimmed afterwards would leak both
  a short page and the rows that were removed from it (§6.2);
* policy projection before size clipping, because a clipped sensitive field is still sensitive (§11);
* for an answer that carries gated content, the access audit is part of the same transaction and the
  content is only returned after that transaction committed; an audit that cannot be written means the
  content does not leave (§11);
* for a command, nothing leaves until the commit returned - not a receipt, and not the log line that
  says whether this call executed anything or merely replayed an earlier one (§9.3, §15);
* a name the project keeps to itself is *absent* from the object, and a value that is genuinely missing
  is `null`. Those are different answers, and only the adapter knows which one it produced (§6.6).

Nothing here reaches for `get_database()` or `get_settings()`: the services object this app instance was
built with is the only source of connections and limits (§4.4).
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Annotated, Any

from mcp.server.mcpserver import Context
from mcp_types import CallToolResult, ToolAnnotations
from pydantic import Field, StringConstraints
from sqlalchemy.exc import SQLAlchemyError

from ..ai.adapter import AiAdapter
from ..application import case_queries, discovery, report_queries
from ..application import cases as case_commands
from ..application import compilations as compile_commands
from ..application import executions as execution_commands
from ..application.authorization import project as authorised_project
from ..application.context import CallContext
from ..application.unit_of_work import UnitOfWork, read_transaction, write_transaction
from ..config import Settings
from ..domain.enums import CompileStatus, Permission, Sensitivity
from ..domain.errors import ApiError, ErrorCode
from ..domain.mcp_policy import McpPolicy, read_policy
from ..observability import get_logger
from ..reporting.report import locator_attempt_view
from ..repositories.cases import CaseRepository, CompileRepository, is_executable
from ..repositories.reservations import WorkerLeaseRepository
from ..repositories.resources import EnvironmentRepository
from . import cursors
from .auth import SCOPE_CONNECT, SCOPE_READ, SCOPE_RUN, SCOPE_WRITE
from .callcontext import McpCall
from .errors import AdapterCode, NextAction, ToolFailure
from .projections import (
    DIAGNOSTICS_LIMIT,
    FLAG_CASE_CONTENT,
    FLAG_ENABLED,
    FLAG_REPORT_DETAILS,
    FLAG_SERVER_AI,
    LOOKUP_FOUND,
    LOOKUP_NOT_CREATED,
    SETTLED_COMPILE_STATUSES,
    clip,
    compile_poll,
    console_links,
    content_open,
    diagnostic_view,
    execution_poll,
    execution_terminal,
    human_task_view,
    limits_view,
    page_size,
    require_content,
    require_mcp_enabled,
    review_page,
    stamp,
)
from .schemas import (
    Answer,
    CancelReceipt,
    CasePage,
    CaseRef,
    CaseView,
    CaseWriteReceipt,
    CompilationView,
    CompileReceipt,
    ContextView,
    EnvironmentPage,
    EnvironmentRef,
    ExecutionView,
    ProjectAuthority,
    ProjectPage,
    ProjectRef,
    ReportView,
    RunReceipt,
    StepPage,
    StepRef,
    TenantRef,
    call_tool_result,
    over_budget,
    render,
    serialized_size,
)
from .server import McpServices, ToolSpec, register_tool, run_tool

log = get_logger(__name__)

#: §6.5 - every query tool says the same four things about itself.
QUERY_ANNOTATIONS = ToolAnnotations(
    read_only_hint=True,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=False,
)

#: §6.5 - a command that only creates platform rows. `idempotent_hint` is true because the tool demands a
#: key, not because writing a case is naturally repeatable, and `open_world_hint` is false because nothing
#: outside this database is touched. A tool that reaches a target site or a model says otherwise (§6.5).
WRITE_ANNOTATIONS = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=False,
)

#: §6.5 - a compile is the one command that may reach outside this database: `use_ai=true` puts the case in
#: front of the platform's own model. The rest of the row is the write default, and `idempotent_hint` is
#: true for the same reason - the same key queues one attempt, not one attempt per retry.
COMPILE_ANNOTATIONS = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=True,
)

#: §6.5 - a run drives a real browser against a real site, so it is the one tool that says `destructive`
#: out loud. Its idempotency covers the platform's own queue entry, never the target site: the description
#: has to say that, because a client that reads "same key, one execution" as "same key, one checkout" would
#: be promising something this platform cannot deliver.
RUN_ANNOTATIONS = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=True,
    idempotent_hint=True,
    open_world_hint=True,
)

#: §6.5 - stopping a run changes what this platform does with it and nothing outside, so it is destructive
#: without being open-world; the browser it stops is already the one the run above opened.
CANCEL_ANNOTATIONS = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=True,
    idempotent_hint=True,
    open_world_hint=False,
)

#: The shared argument shapes. A resource id is the platform's own id, so it keeps the platform's bound
#: (§6.6); `limit` carries its range here rather than in the body so the published schema states it.
IdArg = Annotated[str, Field(min_length=1, max_length=36)]
OptionalIdArg = Annotated[str | None, Field(min_length=1, max_length=36)]
LimitArg = Annotated[int, Field(ge=1, le=100)]
CursorArg = Annotated[str | None, Field(max_length=1024)]
DigestArg = Annotated[str, Field(min_length=1, max_length=80)]
BooleanArg = Annotated[bool, Field()]
#: §6.6 - a search term and a tag filter are bounded by what the columns hold, not by a page.
SearchArg = Annotated[str | None, Field(max_length=120)]
TagsArg = Annotated[list[Annotated[str, Field(min_length=1, max_length=60)]], Field(max_length=20)]
#: §6.6 - K is one to a hundred and twenty printable ASCII characters. It lands in a column, in an advisory
#: lock key and in a log field, so a newline or a control character is not a style problem but a bug.
IdempotencyArg = Annotated[str, StringConstraints(pattern=r"^[\x20-\x7E]{1,120}$")]
#: The write bounds are the REST ones on purpose (§6.6): an assistant must not be able to author a case, a
#: title or a tag the console itself could not have created. The UTF-8 byte check that also applies to
#: Markdown is the compiler's (`CASE_TOO_LARGE`), so it is not duplicated here.
CaseNameArg = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
OptionalCaseNameArg = Annotated[str | None, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
CaseTagArg = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,59}$")]
CaseTagsArg = list[CaseTagArg]
MarkdownArg = Annotated[str, Field(min_length=1, max_length=262_144)]
DslVersionArg = Annotated[str, StringConstraints(pattern=r"^\d+\.\d+$")]
#: §6.6 - a positive integer, and required: a revision written without the version it was based on is the
#: lost-update bug the column exists to prevent.
ExpectedRowVersionArg = Annotated[int, Field(ge=1)]
#: §6.6 - run variables keep the stored JSON dictionary and its value types: the shared command runs the
#: same declared/required checks a console run does, and the digest must not flatten `1` into `"1"` (§6.4).
#: The default is in the annotation rather than the signature because §6.4's request table says `variables={}`:
#: an omitted dictionary means "no run variables", the same intent as an explicit empty one.
VariablesArg = Annotated[dict[str, Any], Field(default_factory=dict)]
#: The REST bound for a browser name, and the two modes the platform can actually produce. A caller may
#: narrow what a run keeps, never invent a third mode (§6.6).
BrowserArg = Annotated[str | None, StringConstraints(strip_whitespace=True, min_length=1, max_length=24)]
EvidenceModeArg = Annotated[str | None, StringConstraints(pattern=r"^(NORMAL|SENSITIVE)$")]
CancelReasonArg = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=300)]


@dataclass(frozen=True)
class Audit:
    """The audit row a content-bearing answer must have written before its bytes were sent (§11).

    Only identifiers and the *names* of the flags that were honoured go in: a trail that copied the case
    text it authorised would be a second place for that text to leak from.
    """

    resource_type: str
    resource_id: str | None = None
    project_id: str | None = None
    detail: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Read:
    """What a read handler produced: the answer, plus the audit it owes for that answer (§11).

    `audit` is `None` for a minimal metadata read, which leaves an observation log line and nothing else.
    That is a deliberate difference rather than an omission: a row per project-list page would bury the
    trail commands depend on, and §11 asks for a log line for exactly these reads.
    """

    answer: Answer
    audit: Audit | None = None


@dataclass(frozen=True)
class Page:
    """The cursor arithmetic every list tool shares (§11).

    `filters` is part of the cursor's identity rather than an annotation on it: a bookmark from a search
    page must not continue an unfiltered one, because "page 3" only means something relative to the
    question that produced pages 1 and 2.
    """

    kind: str
    tenant_id: str
    limit: int
    cursor: str | None = None
    project_id: str | None = None
    filters: Mapping[str, Any] = field(default_factory=dict)

    def _decode(self) -> cursors.Position | None:
        if not self.cursor:
            return None
        return cursors.decode(
            self.cursor,
            kind=self.kind,
            tenant_id=self.tenant_id,
            filters=cursors.filter_digest(**self.filters),
            project_id=self.project_id,
        )

    def after_name(self) -> tuple[str | None, str | None]:
        """The `(sort key, id)` pair for an `ORDER BY name ASC, id ASC` page."""
        position = self._decode()
        return (None, None) if position is None else (position.key, position.row_id)

    def after_moment(self) -> tuple[datetime | None, str | None]:
        """The pair for a `created_at DESC, id DESC` page, which is what projects and environments use."""
        position = self._decode()
        return (None, None) if position is None else (position.as_timestamp(), position.row_id)

    def after_index(self) -> tuple[int | None, str | None]:
        """The pair for an ascending numeric page: step order, and the diagnostic index."""
        position = self._decode()
        return (None, None) if position is None else (position.as_integer(), position.row_id)

    def kept(self, rows: Sequence[Any]) -> Sequence[Any]:
        """The rows to answer with, dropping the one extra row that only proved there is a next page."""
        return rows[: self.limit]

    def next_cursor(self, rows: Sequence[Any]) -> str | None:
        """The marker for the page after `rows`, where `rows` is the deliberate over-fetch of `limit + 1`.

        A full page with nothing behind it is the last page, and the honest answer for that is no cursor
        at all rather than one that produces an empty page.
        """
        if len(rows) <= self.limit:
            return None
        key, row_id = rows[self.limit - 1].position
        return self._mint(key=key, row_id=row_id)

    def mint(self, *, key: str, row_id: str, more: bool) -> str | None:
        """The marker after a page taken from something that is not a row set, such as a JSON array (§6.6).

        A diagnostic list lives inside one artifact, so there is no extra row to over-fetch: the caller says
        whether more remains, and nothing is offered when it does not.
        """
        return None if not more else self._mint(key=key, row_id=row_id)

    def fit(
        self,
        rows: Sequence[Any],
        items: Sequence[dict[str, Any]],
        build: Callable[[list[dict[str, Any]]], Any],
        *,
        settings: Settings,
        request_id: str,
    ) -> Answer:
        """Render this page inside the metadata budget by dropping whole rows from its tail (§11).

        Shortening is honest only because the cursor is re-minted for the row the answer now ends on, so
        the rows this page moved off are still reachable from the next call. A page that simply stopped
        without one would read as a project having fewer cases than it has, which is a wrong answer rather
        than a short one - and a single row that still does not fit is the case where `RESULT_TOO_LARGE`
        is the truthful response.
        """
        kept = list(items)
        while True:
            answer = Answer(data=build(kept), next_cursor=self._cursor_for(rows, len(kept), len(items)))
            if len(kept) <= 1 or serialized_size(call_tool_result(answer.envelope(request_id))) <= (
                settings.mcp_max_metadata_response_bytes
            ):
                return answer
            kept = kept[:-1]

    def _cursor_for(self, rows: Sequence[Any], kept_count: int, offered: int) -> str | None:
        if kept_count == offered:
            return self.next_cursor(rows)
        key, row_id = rows[kept_count - 1].position
        return self._mint(key=key, row_id=row_id)

    def _mint(self, *, key: str, row_id: str) -> str:
        return cursors.encode(
            kind=self.kind,
            tenant_id=self.tenant_id,
            filters=cursors.filter_digest(**self.filters),
            position=cursors.Position(key=key, row_id=row_id),
            project_id=self.project_id,
        )


def _database(services: McpServices):
    database = services.database
    if database is None:
        # Only a services object built without one reaches here, and that is a wiring fault. Refusing it
        # as an unavailable dependency keeps the answer the same one a real outage gives.
        raise ToolFailure(
            AdapterCode.DEPENDENCY_UNAVAILABLE,
            "MCP is not connected to the platform database",
            retryable=True,
            retry_after_ms=1000,
            next_action=NextAction.retry_same_key_or_query,
        )
    return database


@contextmanager
def _transaction(services: McpServices, call: McpCall, *, tenant: str | None, audited: bool) -> Any:
    """One bounded read transaction for this call, or the refusal that says it could not be had.

    A database failure on this path always answers `DEPENDENCY_UNAVAILABLE`, and that deliberately
    includes the commit of an access audit: the content that audit covers has not been sent, and it will
    not be sent on this call (§11). Nothing here distinguishes "the query failed" from "the audit
    failed", because the caller's next step is the same either way and the distinction belongs in a log.
    """
    selected = tenant or call.tenant_hint
    try:
        with read_transaction(
            _database(services),
            services.settings,
            issuer=call.issuer,
            subject=call.subject,
            request_id=call.request_id,
            tenant_hint=selected,
            scopes=call.credential_scopes,
            deadline=call.deadline,
            select_tenant=bool(selected),
            record_audit=audited,
        ) as uow:
            yield uow
    except SQLAlchemyError as exc:
        raise _unavailable(exc) from exc
    except ApiError as exc:
        if exc.code in _LOST_AUTHORITY:
            # The caller has to be told the truth: retrying will not help, its access is gone.
            raise
        raise _unavailable(exc) from exc


#: Domain refusals that mean "you may not", never "we could not look" (§5.1, §14.1).
_LOST_AUTHORITY = frozenset({ErrorCode.UNAUTHENTICATED, ErrorCode.FORBIDDEN, ErrorCode.NOT_FOUND})


def _unavailable(exc: BaseException) -> ToolFailure:
    log.warning("mcp_read_unavailable", extra={"fields": {"reason": type(exc).__name__}})
    return ToolFailure(
        AdapterCode.DEPENDENCY_UNAVAILABLE,
        "The platform could not read the data this tool needs",
        retryable=True,
        retry_after_ms=1000,
        next_action=NextAction.retry_same_key_or_query,
    )


def _observe(call: McpCall, tool: str, arguments: Mapping[str, Any]) -> None:
    """The record a minimal metadata read leaves behind: a log line, not an audit row (§11)."""
    fields = {key: value for key, value in arguments.items() if value is not None and key != "cursor"}
    log.info("mcp_read", extra={"fields": {"tool": tool, "request_id": call.request_id, **fields}})


async def run_read(
    services: McpServices,
    context: Context,
    tool: str,
    work: Callable[[UnitOfWork, dict[str, Any]], Read],
    *,
    arguments: Mapping[str, Any] | None = None,
    tenant_id: str | None = None,
    audited: bool = False,
) -> CallToolResult:
    """Run one read body through the slot, the transaction and the envelope (§6.1, §11).

    The scope floor is the gate's job, not this helper's: the tool's own `ToolSpec.scope` has already
    been held against the credential before the body ran, and checking it a second time here would
    suggest the first check is not the one that counts.
    """

    async def body(call: McpCall, args: dict[str, Any]) -> Answer:
        read = await services.run_blocking(lambda: _execute(services, call, tool, work, args, tenant_id, audited))
        answer = read.answer
        if not answer.bulk:
            _within_metadata_budget(answer, call.request_id, services.settings)
        return answer

    return await run_tool(services, context, body, arguments=arguments, tenant_id=tenant_id)


def _within_metadata_budget(answer: Answer, request_id: str, settings: Settings) -> None:
    """Hold a metadata answer to the smaller budget, which is the point of having two (§11).

    Measured on the envelope this answer is about to become, after projection and paging, because a
    budget checked against pre-projection rows would be a number nobody honoured. Raising here rather
    than letting `run_tool`'s hard ceiling catch it later is what keeps the 4 MiB cap from silently
    becoming the only limit a list page has.
    """
    result = call_tool_result(answer.envelope(request_id))
    budget = settings.mcp_max_metadata_response_bytes
    if serialized_size(result) > budget:
        raise over_budget(result, budget)


def _execute(
    services: McpServices,
    call: McpCall,
    tool: str,
    work: Callable[[UnitOfWork, dict[str, Any]], Read],
    arguments: Mapping[str, Any],
    tenant_id: str | None,
    audited: bool,
) -> Read:
    """The synchronous half of a read: it owns the transaction, and therefore the commit ordering.

    The body is handed the unit of work rather than the transport call, because everything it may act on
    - the resolved identity, the settings, the deadline - is the application's `CallContext` that the
    transaction carries. The transport facts stay outside: a read that could reach `tenant_hint` would
    have two sources of truth for which tenant it was answering for.
    """
    with _transaction(services, call, tenant=tenant_id, audited=audited) as uow:
        read = work(uow, dict(arguments))
        audit = read.audit
        if audit is not None:
            uow.audit(
                operation="mcp.content.read",
                resource_type=audit.resource_type,
                resource_id=audit.resource_id,
                project_id=audit.project_id,
                detail=dict(audit.detail) | {"tool": tool},
            )
    # Reaching this line means the transaction committed, which is what makes an audited answer honest:
    # no content is returned for an audit row that does not exist.
    if audit is None:
        _observe(call, tool, arguments)
    return read


def policy_of(project: Any) -> McpPolicy:
    """The one policy read for a project row already in hand; unusable means all four flags off (§5.5)."""
    return read_policy(project.settings).policy


def open_project(
    uow: UnitOfWork, project_id: str, *, tool: str, permission: Permission | None = None
) -> tuple[Any, McpPolicy]:
    """Authorise a project and hand back its effective policy, refusing a closed one (§6.2, §14.1).

    Every content-bearing tool goes through here rather than checking the gate itself, so "MCP is off for
    this project" cannot be forgotten for one tool and remembered for nine.
    """
    row = authorised_project(uow.scope, uow.call, project_id, permission=permission)
    return row, require_enabled(project_id, row, tool=tool)


def require_enabled(project_id: str, row: Any, *, tool: str) -> McpPolicy:
    policy = policy_of(row)
    require_mcp_enabled(project_id, policy, tool=tool)
    return policy


def text(value: str | None) -> str | None:
    """One free-text field after the §11 clip: bounded, and never padded out to the limit."""
    if value is None:
        return None
    clipped, _ = clip(str(value))
    return clipped


def role_name(role: Any) -> str | None:
    return None if role is None else str(getattr(role, "value", role))


def cursor_of(value: str | None) -> str | None:
    """An empty string from a client that has no bookmark yet means the first page (§6.6)."""
    return value or None


def capabilities(call: CallContext, *, tenant_selected: bool) -> dict[str, Any]:
    """What this credential and this caller can actually do, which is what the tool is for (§5.4)."""
    identity = call.identity
    return {
        "entrypoint": call.entrypoint,
        "credential_scopes": list(call.scopes),
        "tenant_selected": tenant_selected,
        "tenant_role": role_name(identity.role_in(None)),
        "abilities": sorted(permission.value for permission in identity.permissions_in(None)),
        "grants": identity.granted_projects,
    }


def discoverable_limits(settings: Settings) -> dict[str, Any]:
    """The bounds this deployment answers under, plus the two promises that are not configurable (§6.2)."""
    view = limits_view(settings)
    view["mcp_policy_flags"] = [FLAG_ENABLED, FLAG_CASE_CONTENT, FLAG_REPORT_DETAILS, FLAG_SERVER_AI]
    return view


# --------------------------------------------------------------------------------------
# the write pipeline (§6.1, §9.2, §9.3, §11)
# --------------------------------------------------------------------------------------


@contextmanager
def _command_transaction(services: McpServices, call: McpCall, *, tenant: str | None) -> Any:
    """One command transaction for this call, with only the database's own failures generalised.

    A read turns an unexpected domain refusal into `DEPENDENCY_UNAVAILABLE`, because a read that did not
    raise its own `ToolFailure` never meant to say anything. A write must not: `IDEMPOTENCY_CONFLICT`,
    `VERSION_CONFLICT` and `COMMAND_BUSY` are answers §10 promises the client, each with its own advice
    about whether the key may change. So only what the database itself failed at is rewritten here, and
    it is rewritten as *possibly committed* - a connection lost while committing is not a rollback, and
    telling the caller to change its key would duplicate the resource (§10).

    A command always names its tenant. §6.2 refuses the "caller's first tenant" default for a write: a row
    created in a tenant the caller did not choose is one no member can read, so an adapter that forgot the
    argument is a wiring fault and is answered as one.
    """
    selected = tenant or call.tenant_hint
    if not selected:
        raise ToolFailure(
            AdapterCode.VALIDATION_ERROR,
            "This tool requires tenant_id",
            next_action=NextAction.fix_input,
        )
    try:
        with write_transaction(
            _database(services),
            services.settings,
            issuer=call.issuer,
            subject=call.subject,
            request_id=call.request_id,
            tenant_id=selected,
            scopes=call.credential_scopes,
            deadline=call.deadline,
        ) as uow:
            yield uow
    except SQLAlchemyError as exc:
        raise _command_unavailable(exc) from exc


def _command_unavailable(exc: BaseException) -> ToolFailure:
    log.warning("mcp_write_unavailable", extra={"fields": {"reason": type(exc).__name__}})
    return ToolFailure(
        AdapterCode.DEPENDENCY_UNAVAILABLE,
        "The platform could not complete this command, which may already have been recorded",
        retryable=True,
        retry_after_ms=1000,
        next_action=NextAction.retry_same_key_or_query,
    )


#: §15 - the only fields a write may leave in the log. Markdown, names, titles, tags and run variables are
#: content, and a run's variables are exactly the values a case was pointed at; none of them belong in a
#: log line, which has no policy gate and no size budget of its own.
_WRITE_FACT_FIELDS = ("tenant_id", "project_id", "case_id", "revision_id", "compile_artifact_id", "execution_id")


def _write_facts(arguments: Mapping[str, Any], answer: Answer | None = None) -> dict[str, Any]:
    """The identifiers this call is about, taken from the receipt first and from the arguments otherwise.

    A refused call has no receipt, so its line is built from the ids the caller named - which is also the
    honest ordering for a replay: the stored answer says what the earlier call created.
    """
    data = answer.data if isinstance(answer, Answer) and isinstance(answer.data, Mapping) else {}
    facts: dict[str, Any] = {}
    for field_name in _WRITE_FACT_FIELDS:
        value = data.get(field_name)
        if value is None:
            value = arguments.get(field_name)
        if value is not None:
            facts[field_name] = value
    return facts


def _observe_write(
    call: McpCall, tool: str, facts: Mapping[str, Any], *, result_code: str, replayed: bool, duration_ms: int
) -> None:
    """The line a write leaves behind, whether it committed, replayed or was refused (§15).

    `replayed` is the reason this exists: a receipt looks identical whether this call created the resource
    or read back the answer an earlier one stored, and an assistant that cannot tell the two apart cannot
    reason about whether its own retry worked. The audit row for a replay says the same thing in a way a
    reviewer can check, and it is written by the command that did not run.

    `result_code` is a stable code, never a message: a refusal is the one call an operator most needs to
    find, and the codes are the half of a refusal that carries no content.
    """
    log.info(
        "mcp_write",
        extra={
            "fields": {
                "tool": tool,
                "entrypoint": "mcp",
                "request_id": call.request_id,
                "result_code": result_code,
                "replayed": replayed,
                "duration_ms": duration_ms,
                **facts,
            }
        },
    )


def _elapsed(started: float) -> int:
    return round((time.monotonic() - started) * 1000)


def _refusal_code(exc: BaseException) -> str:
    """A refusal's stable code, and nothing of its message (§15).

    `.value` is read explicitly because these codes are str-mixin enums, where `str()` gives `Class.MEMBER`
    on this Python and a log field that changes shape with the runtime is a field nobody can query.
    """
    code = getattr(exc, "code", None)
    return str(getattr(code, "value", code) or "INTERNAL_ERROR")


def _execute_write(
    services: McpServices,
    call: McpCall,
    tool: str,
    work: Callable[[UnitOfWork, dict[str, Any]], Answer],
    arguments: Mapping[str, Any],
    tenant_id: str | None,
) -> Answer:
    """The synchronous half of a write: it owns the transaction, and therefore the commit ordering.

    The body is handed the unit of work and nothing else, exactly as a read is: what it may act on is the
    resolved identity, the settings and the deadline that transaction carries. It returns the receipt it
    projected *inside* that transaction, because a receipt for a row the transaction then failed to commit
    would be a lie the client cannot detect.
    """
    started = time.monotonic()
    try:
        with _command_transaction(services, call, tenant=tenant_id) as uow:
            answer = work(uow, dict(arguments))
            replayed = uow.replayed
            facts = {"actor_id": uow.call.actor_id, **_write_facts(arguments, answer)}
    except (ToolFailure, ApiError) as exc:
        # No `actor_id` here: a refusal may arrive before the identity resolved, and putting the token
        # subject in that slot would offer a claimed identity to reviewers who read the field as verified.
        _observe_write(
            call,
            tool,
            _write_facts(arguments),
            result_code=_refusal_code(exc),
            replayed=False,
            duration_ms=_elapsed(started),
        )
        raise
    # Reaching this line means the command committed, so the receipt and the log line describe a state the
    # database actually holds.
    _observe_write(
        call,
        tool,
        facts,
        result_code="REPLAYED" if replayed else "OK",
        replayed=replayed,
        duration_ms=_elapsed(started),
    )
    return answer


async def run_write(
    services: McpServices,
    context: Context,
    tool: str,
    work: Callable[[UnitOfWork, dict[str, Any]], Answer],
    *,
    arguments: Mapping[str, Any] | None = None,
    tenant_id: str | None = None,
) -> CallToolResult:
    """Run one write body through the slot, the command transaction and the envelope (§6.1, §9.3).

    A receipt is not measured against the metadata budget the way a page is, and that is deliberate: the
    budget exists to trim answers whose size the caller did not ask for, while a receipt's fields are fixed
    and bounded by the id column limits. `run_tool`'s hard ceiling still covers it, as it covers everything.
    """

    async def body(call: McpCall, args: dict[str, Any]) -> Answer:
        return await services.run_blocking(lambda: _execute_write(services, call, tool, work, args, tenant_id))

    return await run_tool(services, context, body, arguments=arguments, tenant_id=tenant_id)


def write_gate(
    uow: UnitOfWork,
    project_id: str,
    *,
    tool: str,
    checks: Callable[[McpPolicy], None] | None = None,
) -> Callable[[], None]:
    """A refusal that applies only to a *new* intent, for the command to run after its replay decision (§6.2).

    The split is the point. Authority is not this helper's business - the shared command checks the project
    and the permission before it reserves the key, and checks them again after any lock wait (§9.3.1) -
    because a revoked member must lose a replay too. The MCP gate is different: §6.2 promises a closed
    project still answers a successful same-key intent with its receipt, while a new intent is refused.
    Only the idempotency layer knows which of the two this call is, so the gate is handed to it.

    `checks` is for a second policy question the same read can answer - a compile has to know whether this
    project agreed to let the platform's own model read the case - and it runs under the same rule: it may
    refuse a new intent, never a replay of one that already succeeded.

    The policy is read when the gate runs rather than closed over from now, because "now" is before the
    advisory-lock wait: a project that switched off while this call queued must be refused, not let through
    on a snapshot of a policy that no longer exists (§9.3.1).
    """

    def refuse_if_closed() -> None:
        row = authorised_project(uow.scope, uow.call, project_id)
        policy = require_enabled(project_id, row, tool=tool)
        if checks is not None:
            checks(policy)

    return refuse_if_closed


def require_server_ai(
    settings: Settings,
    policy: McpPolicy,
    *,
    project_id: str,
    tool: str,
    purpose: str = "compiler",
    fallback: str = "the deterministic compile",
) -> None:
    """Refuse an explicit request for the platform's own model, naming which half said no (§6.4).

    Two conditions, two codes, because the fix belongs to a different party each time: an unconfigured
    deployment is an operator's, and a project that never agreed is an administrator's. Answering
    `DATA_POLICY_DENIED` when the model is simply absent would tell the caller that widening a policy
    could work, and the reverse would tell an operator to go and tick a box that changes nothing.

    Neither is inferred from `allow_case_content` (§5.5): sending text to a model the platform has never
    heard of, and sending it to this platform's own model, are different decisions an administrator made
    separately.

    `purpose` and `fallback` keep one helper honest for both of the tools that can ask for a model: a
    compile names the deterministic compiler as what remains, and a run names the model-free run.
    """
    if not AiAdapter(settings, purpose=purpose).enabled:
        raise ToolFailure(
            AdapterCode.AI_DISABLED,
            f"This deployment has no internal model configured, so {fallback} is the only path",
            details={"project_id": project_id, "tool": tool, "ai_configured": False},
            next_action=NextAction.narrow_request,
        )
    require_content(policy, tool=tool, flag=FLAG_SERVER_AI, project_id=project_id)


def _case_receipt(uow: UnitOfWork, result: Mapping[str, Any]) -> CaseWriteReceipt:
    """The receipt for a case write: identifiers and a digest, and no echo of the text (§6.6).

    `row_version` comes from the case row rather than from the stored command result, because a replay has
    to name the version to write against *now* - the case may have taken another revision since the write
    this key committed, and a stale number would guarantee the caller's next command loses.
    """
    case_id = str(result["case_id"])
    revision_id = str(result["revision_id"])
    row = CaseRepository(uow.scope, uow.call.tenant_id).require(case_id)
    return CaseWriteReceipt(
        case_id=case_id,
        revision_id=revision_id,
        revision_no=int(result["revision_no"]),
        row_version=int(row.row_version),
        source_digest=str(result["source_digest"]),
        compilation_lookup={"revision_id": revision_id},
    )


# --------------------------------------------------------------------------------------
# aita_get_context
# --------------------------------------------------------------------------------------


@register_tool("aita_get_context")
def _get_context_tool(services: McpServices) -> ToolSpec:
    """Where an assistant starts: who I am, what I belong to, and what this deployment allows."""

    async def handler(
        context: Context,
        tenant_id: OptionalIdArg = None,
        limit: LimitArg = 20,
        cursor: CursorArg = None,
    ) -> CallToolResult:
        arguments = {"tenant_id": tenant_id, "limit": limit, "cursor": cursor}
        return await run_read(
            services,
            context,
            "aita_get_context",
            _get_context,
            arguments=arguments,
            tenant_id=tenant_id,
        )

    return ToolSpec(
        name="aita_get_context",
        description=(
            "Discover the caller's own access: the tenants it belongs to when no tenant_id is given, or "
            "the projects and permissions inside the selected tenant. Always answers, even for a project "
            "whose MCP access is switched off, and never returns case or report content."
        ),
        scope=SCOPE_CONNECT,
        handler=handler,
        annotations=QUERY_ANNOTATIONS,
    )


def _get_context(uow: UnitOfWork, arguments: dict[str, Any]) -> Read:
    call = uow.call
    settings = call.settings
    limit = page_size(arguments.get("limit"))
    cursor = cursor_of(arguments.get("cursor"))
    if not call.tenant_id:
        return _tenant_context(uow, call, limit=limit, cursor=cursor, settings=settings)
    return _project_context(uow, call, limit=limit, cursor=cursor, settings=settings)


def _tenant_context(
    uow: UnitOfWork, call: CallContext, *, limit: int, cursor: str | None, settings: Settings
) -> Read:
    """The tenant list, which is the caller's own membership rows and nothing else (§5.1).

    This is the one page readable without a selected tenant because it is the read that answers "which
    tenant should I select"; `enabled` is a project-scoped gate, so there is no policy to apply above it.
    """
    page = Page(kind=cursors.KIND_TENANTS, tenant_id="", limit=limit, cursor=cursor)
    after_name, after_id = page.after_name()
    rows = discovery.tenant_page(
        uow.scope,
        issuer=call.identity.issuer,
        subject=call.identity.subject,
        limit=limit + 1,
        after_name=after_name,
        after_id=after_id,
    )
    items = render(
        [
            TenantRef(
                tenant_id=row.tenant_id,
                role=role_name(row.role),
                name=text(row.name),
                display_name=text(row.display_name),
            )
            for row in page.kept(rows)
        ]
    )
    def build_view(kept: list[dict[str, Any]]) -> ContextView:
        return ContextView(
            actor_id=call.actor_id,
            tenant_id=None,
            items=kept,
            capabilities=capabilities(call, tenant_selected=False),
            limits=discoverable_limits(settings),
        )

    return Read(page.fit(rows, items, build_view, settings=settings, request_id=call.request_id))


def _project_context(
    uow: UnitOfWork, call: CallContext, *, limit: int, cursor: str | None, settings: Settings
) -> Read:
    page = Page(kind=cursors.KIND_PROJECTS, tenant_id=call.tenant_id, limit=limit, cursor=cursor)
    after_moment, after_id = page.after_moment()
    rows = discovery.project_page(
        uow.scope,
        tenant_id=call.tenant_id,
        user_id=call.actor_id,
        tenant_role=call.identity.role_in(None),
        limit=limit + 1,
        after_created_at=after_moment,
        after_id=after_id,
    )
    items = render([_authority(call, row) for row in page.kept(rows)])

    def build_view(kept: list[dict[str, Any]]) -> ContextView:
        return ContextView(
            actor_id=call.actor_id,
            tenant_id=call.tenant_id,
            items=kept,
            capabilities=capabilities(call, tenant_selected=True),
            limits=discoverable_limits(settings),
        )

    return Read(page.fit(rows, items, build_view, settings=settings, request_id=call.request_id))


def _authority(call: CallContext, row: discovery.ProjectRow) -> ProjectAuthority:
    """One project as its member sees it; a closed project shows its id and the gate, and nothing else.

    The permissions are read from the identity this call resolved, which is the caller's own authority -
    `get_context` is the tool an assistant uses to find out what it may ask for next, not a way to read
    another user's role (§5.4).
    """
    named = {"name": text(row.name), "display_name": text(row.display_name)} if row.mcp_enabled else {}
    identity = call.identity
    return ProjectAuthority(
        project_id=row.project_id,
        mcp_enabled=row.mcp_enabled,
        permissions=sorted(permission.value for permission in identity.permissions_in(row.project_id)),
        role=role_name(identity.role_in(row.project_id)),
        **named,
    )


# --------------------------------------------------------------------------------------
# aita_list_projects
# --------------------------------------------------------------------------------------


@register_tool("aita_list_projects")
def _list_projects_tool(services: McpServices) -> ToolSpec:
    async def handler(
        context: Context,
        tenant_id: IdArg,
        limit: LimitArg = 20,
        cursor: CursorArg = None,
    ) -> CallToolResult:
        return await run_read(
            services,
            context,
            "aita_list_projects",
            _list_projects,
            arguments={"limit": limit, "cursor": cursor},
            tenant_id=tenant_id,
        )

    return ToolSpec(
        name="aita_list_projects",
        description=(
            "List the projects the caller can see in one tenant, newest first, each with its own MCP gate "
            "state. A project whose MCP access is off is listed by id alone, so an assistant can tell "
            "'not enabled' from 'not visible to me'."
        ),
        scope=SCOPE_READ,
        handler=handler,
        annotations=QUERY_ANNOTATIONS,
    )


def _list_projects(uow: UnitOfWork, arguments: dict[str, Any]) -> Read:
    """The same page `get_context` answers for a selected tenant, minus the permissions (§6.2).

    One keyset and one filter set, so a cursor from either tool continues the other honestly: they read
    the same rows in the same order and differ only in what they project.
    """
    call = uow.call
    limit = page_size(arguments.get("limit"))
    page = Page(
        kind=cursors.KIND_PROJECTS,
        tenant_id=call.tenant_id,
        limit=limit,
        cursor=cursor_of(arguments.get("cursor")),
    )
    after_moment, after_id = page.after_moment()
    rows = discovery.project_page(
        uow.scope,
        tenant_id=call.tenant_id,
        user_id=call.actor_id,
        tenant_role=call.identity.role_in(None),
        limit=limit + 1,
        after_created_at=after_moment,
        after_id=after_id,
    )
    items = render(
        [
            ProjectRef(
                project_id=row.project_id,
                mcp_enabled=row.mcp_enabled,
                **({"name": text(row.name), "display_name": text(row.display_name)} if row.mcp_enabled else {}),
            )
            for row in page.kept(rows)
        ]
    )
    return Read(
        page.fit(rows, items, lambda kept: ProjectPage(items=kept), settings=call.settings, request_id=call.request_id)
    )


# --------------------------------------------------------------------------------------
# aita_list_environments
# --------------------------------------------------------------------------------------


@register_tool("aita_list_environments")
def _list_environments_tool(services: McpServices) -> ToolSpec:
    async def handler(
        context: Context,
        tenant_id: IdArg,
        project_id: IdArg,
        limit: LimitArg = 20,
        cursor: CursorArg = None,
    ) -> CallToolResult:
        return await run_read(
            services,
            context,
            "aita_list_environments",
            _list_environments,
            arguments={"project_id": project_id, "limit": limit, "cursor": cursor},
            tenant_id=tenant_id,
        )

    return ToolSpec(
        name="aita_list_environments",
        description=(
            "List a project's environments with the published revision to run against. Environment "
            "configuration - host names, variable values and secret bindings - is never returned, and "
            "`current_revision_id` is null when none has been published."
        ),
        scope=SCOPE_READ,
        handler=handler,
        annotations=QUERY_ANNOTATIONS,
    )


def _list_environments(uow: UnitOfWork, arguments: dict[str, Any]) -> Read:
    project_id = str(arguments["project_id"])
    open_project(uow, project_id, tool="aita_list_environments")
    call = uow.call
    limit = page_size(arguments.get("limit"))
    page = Page(
        kind=cursors.KIND_ENVIRONMENTS,
        tenant_id=call.tenant_id,
        limit=limit,
        cursor=cursor_of(arguments.get("cursor")),
        project_id=project_id,
        filters={"project_id": project_id},
    )
    after_moment, after_id = page.after_moment()
    rows = discovery.environment_page(
        uow.scope,
        tenant_id=call.tenant_id,
        project_id=project_id,
        limit=limit + 1,
        after_created_at=after_moment,
        after_id=after_id,
    )
    items = render(
        [
            EnvironmentRef(
                environment_id=row.environment_id,
                current_revision_id=row.current_revision_id,
                row_version=row.row_version,
                name=text(row.name),
                revision_version=row.revision_version,
            )
            for row in page.kept(rows)
        ]
    )
    return Read(
        page.fit(
            rows, items, lambda kept: EnvironmentPage(items=kept), settings=call.settings, request_id=call.request_id
        )
    )


# --------------------------------------------------------------------------------------
# aita_list_cases
# --------------------------------------------------------------------------------------


@register_tool("aita_list_cases")
def _list_cases_tool(services: McpServices) -> ToolSpec:
    async def handler(
        context: Context,
        tenant_id: IdArg,
        project_id: IdArg,
        search: SearchArg = None,
        tags: TagsArg = (),
        limit: LimitArg = 20,
        cursor: CursorArg = None,
    ) -> CallToolResult:
        return await run_read(
            services,
            context,
            "aita_list_cases",
            _list_cases,
            arguments={
                "project_id": project_id,
                "search": search,
                "tags": list(tags),
                "limit": limit,
                "cursor": cursor,
            },
            tenant_id=tenant_id,
            audited=True,
        )

    return ToolSpec(
        name="aita_list_cases",
        description=(
            "List a project's cases newest first, filtered by a literal name substring and by tag names, "
            "with the filters applied before the page is taken. Each item carries the current revision id, "
            "its digest, the row version and the newest compile status. A case's own words - name, title "
            "and tags - are only sent when the project's policy allows case content."
        ),
        scope=SCOPE_READ,
        handler=handler,
        annotations=QUERY_ANNOTATIONS,
    )


def _list_cases(uow: UnitOfWork, arguments: dict[str, Any]) -> Read:
    call = uow.call
    project_id = str(arguments["project_id"])
    _, policy = open_project(uow, project_id, tool="aita_list_cases", permission=Permission.CASE_READ)
    search = _search_of(arguments.get("search"))
    tag_names = [str(name) for name in arguments.get("tags") or ()]
    tag_ids, unknown = case_queries.tag_ids_for_names(
        uow.scope, tenant_id=call.tenant_id, project_id=project_id, names=tag_names
    )
    if unknown:
        # Refusing rather than filtering on nothing: an assistant that asked for `login` and was quietly
        # not filtered would conclude the project holds no such case (§10 - a wrong answer, not a short one).
        raise ToolFailure(
            AdapterCode.VALIDATION_ERROR,
            "This project has no tag with one of the requested names",
            details={"unknown_tags": unknown, "project_id": project_id},
            next_action=NextAction.fix_input,
        )
    limit = page_size(arguments.get("limit"))
    page = Page(
        kind=cursors.KIND_CASES,
        tenant_id=call.tenant_id,
        limit=limit,
        cursor=cursor_of(arguments.get("cursor")),
        project_id=project_id,
        filters={"project_id": project_id, "search": search, "tags": _wanted_tags(tag_names)},
    )
    after_moment, after_id = page.after_moment()
    rows = case_queries.case_page(
        uow.scope,
        tenant_id=call.tenant_id,
        project_id=project_id,
        limit=limit + 1,
        search=search,
        tag_ids=tag_ids,
        after_created_at=after_moment,
        after_id=after_id,
    )
    allowed = content_open(policy, FLAG_CASE_CONTENT)
    items = render([_case_ref(row, allowed=allowed) for row in page.kept(rows)])
    answer = page.fit(
        rows, items, lambda kept: CasePage(items=kept), settings=call.settings, request_id=call.request_id
    )
    audit = _page_audit(project_id, allowed=allowed, count=len(answer.data["items"]))
    return Read(answer, audit=audit)


def _case_ref(row: case_queries.CaseRow, *, allowed: bool) -> CaseRef:
    """One case as far as this project's policy lets it be seen: identifiers always, prose by flag (§6.6)."""
    named: dict[str, Any] = {}
    if allowed:
        named = {"name": text(row.name), "title": text(row.title), "tags": list(row.tags)}
    return CaseRef(
        case_id=row.case_id,
        row_version=row.row_version,
        compile_status=row.compile_status,
        revision_id=row.current_revision_id,
        source_digest=row.source_digest,
        archived=row.archived,
        **named,
    )


def _wanted_tags(names: Sequence[str]) -> list[str]:
    return sorted({name.strip().lower() for name in names if name and name.strip()})


def _search_of(value: Any) -> str | None:
    """A blank search is no search: it must not become "match the empty string", which matches all."""
    term = str(value).strip() if value is not None else ""
    return term or None


def _page_audit(project_id: str, *, allowed: bool, count: int) -> Audit | None:
    """The access row a list page owes when it sent gated prose, and only then (§11)."""
    if not allowed or count == 0:
        return None
    return Audit(
        resource_type="case",
        project_id=project_id,
        detail={"flag": FLAG_CASE_CONTENT, "items": count},
    )


# --------------------------------------------------------------------------------------
# aita_get_case
# --------------------------------------------------------------------------------------


@register_tool("aita_get_case")
def _get_case_tool(services: McpServices) -> ToolSpec:
    async def handler(
        context: Context,
        tenant_id: IdArg,
        case_id: IdArg,
        include_markdown: BooleanArg = False,
    ) -> CallToolResult:
        return await run_read(
            services,
            context,
            "aita_get_case",
            _get_case,
            arguments={"case_id": case_id, "include_markdown": include_markdown},
            tenant_id=tenant_id,
            audited=True,
        )

    return ToolSpec(
        name="aita_get_case",
        description=(
            "Read one case: its current revision, source digest and row version, plus where to ask about "
            "its compilation. Markdown is only sent when this tool is asked for it explicitly and the "
            "project's policy allows case content; `content_available` says whether it would."
        ),
        scope=SCOPE_READ,
        handler=handler,
        annotations=QUERY_ANNOTATIONS,
    )


def _get_case(uow: UnitOfWork, arguments: dict[str, Any]) -> Read:
    call = uow.call
    case_id = str(arguments["case_id"])
    row = case_queries.case_by_id(uow.scope, tenant_id=call.tenant_id, case_id=case_id)
    if row is None:
        raise ToolFailure(
            AdapterCode.NOT_FOUND,
            f"Case {case_id} not found in this tenant",
            details={"case_id": case_id},
            next_action=NextAction.fix_input,
        )
    _, policy = open_project(uow, row.project_id, tool="aita_get_case", permission=Permission.CASE_READ)
    allowed = content_open(policy, FLAG_CASE_CONTENT)
    if bool(arguments.get("include_markdown")):
        require_content(policy, tool="aita_get_case", flag=FLAG_CASE_CONTENT, project_id=row.project_id)
    markdown, markdown_bytes = (
        _markdown_of(uow, row) if allowed and arguments.get("include_markdown") else (None, None)
    )
    view = CaseView(
        case_id=row.case_id,
        project_id=row.project_id,
        row_version=row.row_version,
        content_available=allowed,
        compilation_lookup=_compilation_lookup(row),
        current_revision_id=row.current_revision_id,
        **_case_fields(row, allowed=allowed, markdown=markdown, markdown_bytes=markdown_bytes),
    )
    audit = (
        Audit(
            resource_type="case",
            resource_id=row.case_id,
            project_id=row.project_id,
            detail={"flag": FLAG_CASE_CONTENT, "markdown_sent": markdown is not None},
        )
        if allowed
        else None
    )
    return Read(Answer(data=view, bulk=markdown is not None), audit=audit)


def _markdown_of(uow: UnitOfWork, row: case_queries.CaseRow) -> tuple[str | None, int | None]:
    """The revision's full text and its byte count, read only on the path that will send them.

    Not clipped: §6.6 promises the complete Markdown on an agreed request, and §11 forbids passing a
    truncated document off as whole. The response budget, not this function, is what bounds it - and when
    the text does not fit, the answer is `RESULT_TOO_LARGE`, never a shorter case.
    """
    if row.current_revision_id is None:
        return None, None
    fetched = case_queries.revision_markdown(
        uow.scope, tenant_id=uow.call.tenant_id, revision_id=row.current_revision_id
    )
    return fetched if fetched is not None else (None, None)


def _compilation_lookup(row: case_queries.CaseRow) -> dict[str, Any]:
    """Where to ask about this case's compilation, pinned to an artifact once one exists (§6.3)."""
    lookup: dict[str, Any] = {"revision_id": row.current_revision_id}
    if row.compile_artifact_id:
        lookup["compile_artifact_id"] = row.compile_artifact_id
    return lookup


def _case_fields(
    row: case_queries.CaseRow, *, allowed: bool, markdown: str | None, markdown_bytes: int | None
) -> dict[str, Any]:
    """The fields that are not identifiers, split by whether this project sends its prose to a model."""
    fields: dict[str, Any] = {
        "source_digest": row.source_digest,
        "dsl_version": row.dsl_version,
        "revision_version": row.revision_version,
    }
    if allowed:
        fields |= {
            "name": text(row.name),
            "title": text(row.title),
            "description": text(row.description),
            "tags": list(row.tags),
        }
    if markdown is not None:
        fields |= {"markdown": markdown, "markdown_bytes": markdown_bytes}
    return fields


# --------------------------------------------------------------------------------------
# aita_create_case
# --------------------------------------------------------------------------------------


@register_tool("aita_create_case")
def _create_case_tool(services: McpServices) -> ToolSpec:
    async def handler(
        context: Context,
        tenant_id: IdArg,
        project_id: IdArg,
        name: CaseNameArg,
        markdown: MarkdownArg,
        idempotency_key: IdempotencyArg,
        dsl_version: DslVersionArg = "1.0",
        title: OptionalCaseNameArg = None,
        tags: CaseTagsArg = (),
    ) -> CallToolResult:
        return await run_write(
            services,
            context,
            "aita_create_case",
            _create_case,
            arguments={
                "tenant_id": tenant_id,
                "project_id": project_id,
                "name": name,
                "markdown": markdown,
                "idempotency_key": idempotency_key,
                "dsl_version": dsl_version,
                "title": title,
                "tags": list(tags),
            },
            tenant_id=tenant_id,
        )

    return ToolSpec(
        name="aita_create_case",
        description=(
            "Create a case from Markdown and queue its deterministic compile. Needs a fresh "
            "idempotency_key: the same key with the same body returns the same case instead of creating a "
            "second one, and with a different body it is refused. The answer names the case, the revision "
            "and the row version to revise against - it never echoes the Markdown back. Ask "
            "aita_get_compilation with the returned revision_id for the compile result."
        ),
        scope=SCOPE_WRITE,
        handler=handler,
        annotations=WRITE_ANNOTATIONS,
    )


def _create_case(uow: UnitOfWork, arguments: dict[str, Any]) -> Answer:
    project_id = str(arguments["project_id"])
    result = case_commands.create_case(
        uow,
        project_id=project_id,
        name=str(arguments["name"]),
        markdown=str(arguments["markdown"]),
        dsl_version=str(arguments["dsl_version"]),
        title=arguments.get("title"),
        tags=list(arguments.get("tags") or []),
        idempotency_key=str(arguments["idempotency_key"]),
        gate=write_gate(uow, project_id, tool="aita_create_case"),
    )
    return Answer(data=_case_receipt(uow, result))


# --------------------------------------------------------------------------------------
# aita_add_case_revision
# --------------------------------------------------------------------------------------


@register_tool("aita_add_case_revision")
def _add_case_revision_tool(services: McpServices) -> ToolSpec:
    async def handler(
        context: Context,
        tenant_id: IdArg,
        case_id: IdArg,
        markdown: MarkdownArg,
        expected_row_version: ExpectedRowVersionArg,
        idempotency_key: IdempotencyArg,
        dsl_version: DslVersionArg = "1.0",
        title: OptionalCaseNameArg = None,
    ) -> CallToolResult:
        return await run_write(
            services,
            context,
            "aita_add_case_revision",
            _add_case_revision,
            arguments={
                "tenant_id": tenant_id,
                "case_id": case_id,
                "markdown": markdown,
                "expected_row_version": expected_row_version,
                "idempotency_key": idempotency_key,
                "dsl_version": dsl_version,
                "title": title,
            },
            tenant_id=tenant_id,
        )

    return ToolSpec(
        name="aita_add_case_revision",
        description=(
            "Add an immutable new revision to an existing case and queue its compile. "
            "expected_row_version must be the row_version the last read returned, so a revision is never "
            "written over one someone else added in between. Needs a fresh idempotency_key, and answers "
            "with the new revision, its digest and the case's next row version."
        ),
        scope=SCOPE_WRITE,
        handler=handler,
        annotations=WRITE_ANNOTATIONS,
    )


def _add_case_revision(uow: UnitOfWork, arguments: dict[str, Any]) -> Answer:
    case_id = str(arguments["case_id"])
    row = CaseRepository(uow.scope, uow.call.tenant_id).by_id(case_id)
    if row is None:
        raise _missing("case", case_id)
    result = case_commands.add_case_revision(
        uow,
        case_id=case_id,
        markdown=str(arguments["markdown"]),
        dsl_version=str(arguments["dsl_version"]),
        title=arguments.get("title"),
        expected_row_version=int(arguments["expected_row_version"]),
        idempotency_key=str(arguments["idempotency_key"]),
        gate=write_gate(uow, row.project_id, tool="aita_add_case_revision"),
    )
    return Answer(data=_case_receipt(uow, result))


# --------------------------------------------------------------------------------------
# aita_compile_case_revision
# --------------------------------------------------------------------------------------


@register_tool("aita_compile_case_revision")
def _compile_case_revision_tool(services: McpServices) -> ToolSpec:
    async def handler(
        context: Context,
        tenant_id: IdArg,
        revision_id: IdArg,
        idempotency_key: IdempotencyArg,
        use_ai: BooleanArg = False,
        force: BooleanArg = False,
    ) -> CallToolResult:
        return await run_write(
            services,
            context,
            "aita_compile_case_revision",
            _compile_case_revision,
            arguments={
                "tenant_id": tenant_id,
                "revision_id": revision_id,
                "idempotency_key": idempotency_key,
                "use_ai": use_ai,
                "force": force,
            },
            tenant_id=tenant_id,
        )

    return ToolSpec(
        name="aita_compile_case_revision",
        description=(
            "Compile one immutable revision and answer at once with the artifact that will hold the result. "
            "The deterministic compiler is the default; use_ai=true asks the platform's own model to read the "
            "case, which needs this deployment to have a model configured and this project to allow it, and is "
            "refused by name rather than quietly compiled the other way. force=true compiles again even when "
            "a finished attempt exists. Needs an idempotency_key: the same key returns the same artifact "
            "instead of queueing a second attempt. Read the outcome back with aita_get_compilation."
        ),
        scope=SCOPE_WRITE,
        handler=handler,
        annotations=COMPILE_ANNOTATIONS,
    )


def _compile_case_revision(uow: UnitOfWork, arguments: dict[str, Any]) -> Answer:
    revision_id = str(arguments["revision_id"])
    use_ai = bool(arguments["use_ai"])
    revision = _revision(uow, revision_id)
    project_id = str(revision.project_id)
    result = compile_commands.compile_revision(
        uow,
        revision_id=revision_id,
        use_ai=use_ai,
        force=bool(arguments["force"]),
        idempotency_key=str(arguments["idempotency_key"]),
        gate=(
            write_gate(
                uow,
                project_id,
                tool="aita_compile_case_revision",
                checks=(
                    None
                    if not use_ai
                    else lambda policy: require_server_ai(
                        uow.call.settings, policy, project_id=project_id, tool="aita_compile_case_revision"
                    )
                ),
            )
        ),
    )
    return Answer(data=_compile_receipt(uow, result, use_ai=use_ai))


def _compile_receipt(uow: UnitOfWork, result: Mapping[str, Any], *, use_ai: bool) -> CompileReceipt:
    """The artifact this call queued, and when to ask about it again (§6.6, §8.1).

    `compiler_mode` reports what was *asked for*, because the artifact row still carries the column default
    until the worker writes the real one, and a receipt that answered `deterministic` to `use_ai=true` would
    hide exactly the downgrade §6.4 forbids. The status and the poll come from the row, though: a replay of a
    key from an hour ago must not claim the attempt is pending when it finished in the meantime.
    """
    artifact_id = str(result["compile_artifact_id"])
    artifact = CompileRepository(uow.scope, uow.call.tenant_id).by_id(artifact_id)
    status = str(artifact.status) if artifact is not None else str(result["status"])
    settled = status in SETTLED_COMPILE_STATUSES
    return CompileReceipt(
        compile_artifact_id=artifact_id,
        revision_id=str(result["revision_id"]),
        compile_status=status,
        compiler_mode="ai_assisted" if use_ai else "deterministic",
        compiler_version=str(result["compiler_version"]),
        recommended_poll_after_ms=(None if settled else compile_poll(artifact.created_at if artifact else None)),
    )


# --------------------------------------------------------------------------------------
# aita_get_compilation
# --------------------------------------------------------------------------------------


@register_tool("aita_get_compilation")
def _get_compilation_tool(services: McpServices) -> ToolSpec:
    async def handler(
        context: Context,
        tenant_id: IdArg,
        compile_artifact_id: OptionalIdArg = None,
        revision_id: OptionalIdArg = None,
        include_ir: BooleanArg = False,
        diagnostics_limit: LimitArg = DIAGNOSTICS_LIMIT,
        cursor: CursorArg = None,
    ) -> CallToolResult:
        return await run_read(
            services,
            context,
            "aita_get_compilation",
            _get_compilation,
            arguments={
                "compile_artifact_id": compile_artifact_id,
                "revision_id": revision_id,
                "include_ir": include_ir,
                "diagnostics_limit": diagnostics_limit,
                "cursor": cursor,
            },
            tenant_id=tenant_id,
            audited=True,
        )

    return ToolSpec(
        name="aita_get_compilation",
        description=(
            "Read one compile attempt: status, whether a run may use it, the IR digest, paged diagnostics "
            "and the review items a person must confirm. Ask by revision id to find the newest attempt, "
            "then by the artifact id it returns. A revision with no attempt yet answers "
            "lookup_status=NOT_CREATED with a poll suggestion rather than an error. IR and the free text of "
            "diagnostics need the project to allow case content."
        ),
        scope=SCOPE_READ,
        handler=handler,
        annotations=QUERY_ANNOTATIONS,
    )


def _get_compilation(uow: UnitOfWork, arguments: dict[str, Any]) -> Read:
    call = uow.call
    artifact_id = _clean_id(arguments.get("compile_artifact_id"))
    revision_arg = _clean_id(arguments.get("revision_id"))
    if bool(artifact_id) == bool(revision_arg):
        raise ToolFailure(
            AdapterCode.VALIDATION_ERROR,
            "Pass exactly one of compile_artifact_id or revision_id",
            details={"compile_artifact_id": artifact_id, "revision_id": revision_arg},
            next_action=NextAction.fix_input,
        )
    compiles = CompileRepository(uow.scope, call.tenant_id)
    artifact = None
    revision = None
    revision_id = revision_arg
    if artifact_id is not None:
        artifact = compiles.by_id(artifact_id)
        if artifact is None:
            raise _missing("compile artifact", artifact_id)
        revision_id = str(artifact.revision_id)
        project_id = str(artifact.project_id)
    else:
        revision = _revision(uow, revision_id)
        project_id = str(revision.project_id)
        # §6.3: the newest attempt, ordered `created_at DESC, id DESC`, or the honest absence of one.
        artifact = compiles.latest(revision_id)
    _, policy = open_project(uow, project_id, tool="aita_get_compilation", permission=Permission.CASE_READ)
    allowed = content_open(policy, FLAG_CASE_CONTENT)
    include_ir = bool(arguments.get("include_ir"))
    if include_ir:
        require_content(policy, tool="aita_get_compilation", flag=FLAG_CASE_CONTENT, project_id=project_id)
    return _compilation_read(
        call,
        artifact,
        revision=revision,
        revision_id=revision_id,
        project_id=project_id,
        allowed=allowed,
        include_ir=include_ir,
        limit=page_size(arguments.get("diagnostics_limit")),
        cursor=cursor_of(arguments.get("cursor")),
    )


def _compilation_read(
    call: CallContext,
    artifact: Any,
    *,
    revision: Any,
    revision_id: str,
    project_id: str,
    allowed: bool,
    include_ir: bool,
    limit: int,
    cursor: str | None,
) -> Read:
    """Project one attempt, or the honest absence of one, and page its diagnostics behind it (§6.3, §6.6)."""
    if artifact is None:
        return Read(
            Answer(
                data=CompilationView(
                    lookup_status=LOOKUP_NOT_CREATED,
                    revision_id=revision_id,
                    executable=False,
                    content_available=allowed,
                    compile_artifact_id=None,
                    compile_status=None,
                    ir_digest=None,
                    # Declared explicitly because the wire model omits unset fields: "no artifact yet" must
                    # still answer with the empty lists a caller indexes into, not with missing keys (§6.3).
                    diagnostics=[],
                    review_items=[],
                    # A save queues the compile, so "not yet" is usually "still coming" rather than "never"
                    # (§6.3) - and the poll suggestion is measured from the revision, which is all there is.
                    recommended_poll_after_ms=compile_poll(revision.created_at if revision else None),
                )
            )
        )
    diagnostics = [item for item in (artifact.diagnostics or []) if isinstance(item, dict)]
    # The artifact id is part of the cursor's filter digest, so a page continued against a revision whose
    # newest attempt has since changed is refused rather than silently carried onto a different artifact (§6.6).
    page = Page(
        kind=cursors.KIND_DIAGNOSTICS,
        tenant_id=call.tenant_id,
        limit=limit,
        cursor=cursor,
        project_id=project_id,
        filters={"compile_artifact_id": str(artifact.id)},
    )
    after, _ = page.after_index()
    start = 0 if after is None else max(0, after) + 1
    window = diagnostics[start : start + limit]
    review_items, over_limit = review_page(artifact.review_items, content_allowed=allowed)
    review_required = over_limit or artifact.status == CompileStatus.NEEDS_REVIEW.value
    view = CompilationView(
        lookup_status=LOOKUP_FOUND,
        revision_id=str(artifact.revision_id),
        executable=is_executable(artifact),
        content_available=allowed,
        review_required=review_required,
        compile_artifact_id=str(artifact.id),
        compile_status=str(artifact.status),
        ir_digest=artifact.ir_digest,
        diagnostics=[diagnostic_view(item, content_allowed=allowed) for item in window],
        review_items=review_items,
        review_url=console_links(call.settings)["cases"] if review_required else None,
        compiler_mode=str(artifact.compiler_mode),
        source_digest=str(artifact.source_digest),
        recommended_poll_after_ms=(
            None
            if artifact.status in SETTLED_COMPILE_STATUSES
            else compile_poll(artifact.created_at)
        ),
        **({"ir": artifact.ir} if include_ir else {}),
    )
    audit = (
        Audit(
            resource_type="compile_artifact",
            resource_id=str(artifact.id),
            project_id=project_id,
            detail={"flag": FLAG_CASE_CONTENT, "ir_sent": include_ir, "diagnostics": len(window)},
        )
        if allowed
        else None
    )
    next_cursor = page.mint(
        key=str(start + len(window) - 1), row_id=str(artifact.id), more=start + len(window) < len(diagnostics)
    )
    return Read(Answer(data=view, next_cursor=next_cursor, bulk=include_ir), audit=audit)


def _revision(uow: UnitOfWork, revision_id: str) -> Any:
    """The revision a compilation was attempted for, or the same refusal for any id that is not ours."""
    revision = CaseRepository(uow.scope, uow.call.tenant_id).revision(revision_id)
    if revision is None:
        raise _missing("case revision", revision_id)
    return revision


def _missing(kind: str, row_id: str) -> ToolFailure:
    return ToolFailure(
        AdapterCode.NOT_FOUND,
        f"{kind.capitalize()} {row_id} not found in this tenant",
        details={"id": row_id},
        next_action=NextAction.fix_input,
    )


def _clean_id(value: Any) -> str | None:
    """A blank or whitespace-only id means "not given".

    The argument schema bounds a length, not a content, so `"  "` reaches here as a string. Treating it as
    a lookup would answer "not found" about an id the caller never meant to pass; treating it as absent
    answers the question the caller actually asked - and `get_compilation`'s exactly-one rule reads off
    what is left.
    """
    if value is None:
        return None
    return str(value).strip() or None


# --------------------------------------------------------------------------------------
# The three execution reads: one run's state, its steps, its report (§6.2, §8)
# --------------------------------------------------------------------------------------


@register_tool("aita_get_execution")
def _get_execution_tool(services: McpServices) -> ToolSpec:
    async def handler(context: Context, tenant_id: IdArg, execution_id: IdArg) -> CallToolResult:
        return await run_read(
            services,
            context,
            "aita_get_execution",
            _get_execution,
            arguments={"execution_id": execution_id},
            tenant_id=tenant_id,
        )

    return ToolSpec(
        name="aita_get_execution",
        description=(
            "Read one run's state: status, outcome, how far it has got per step status, whether its base "
            "report, analysis and evidence are ready, and whether it is waiting on a person. It carries no "
            "run variables, no IR and no failure prose, which is what makes it cheap enough to poll. Only "
            "FINISHED is terminal; a run parked on a person stays WAIT_HUMAN and is not rerun."
        ),
        scope=SCOPE_READ,
        handler=handler,
        annotations=QUERY_ANNOTATIONS,
    )


def _get_execution(uow: UnitOfWork, arguments: dict[str, Any]) -> Read:
    call = uow.call
    facts = _execution_facts(uow, str(arguments["execution_id"]))
    policy = _run_policy(uow, facts)
    terminal = execution_terminal(facts.status)
    view = ExecutionView(
        execution_id=facts.execution_id,
        tenant_id=facts.tenant_id,
        project_id=facts.project_id,
        status=facts.status,
        outcome=facts.outcome,
        terminal=terminal,
        state_version=facts.state_version,
        step_counts=dict(facts.step_counts),
        base_report_ready=terminal,
        analysis_status=facts.analysis_status,
        artifact_status=facts.artifact_status,
        cleanup_status=facts.cleanup_status,
        human_required=facts.human_task is not None,
        details_available=content_open(policy, FLAG_REPORT_DETAILS),
        error_code=facts.error_code,
        evidence_mode=facts.evidence_mode,
        browser=facts.browser,
        compile_artifact_id=facts.compile_artifact_id,
        ir_digest=facts.ir_digest,
        environment_revision_id=facts.environment_revision_id,
        human_task=_human_task(call, facts),
        created_at=stamp(facts.created_at),
        started_at=stamp(facts.started_at),
        ended_at=stamp(facts.ended_at),
        active_ms=facts.active_ms,
        human_ms=facts.human_ms,
        links=console_links(call.settings, execution_id=facts.execution_id),
        # §8.1 - a non-terminal answer always offers an interval, and a terminal one never does: the two
        # states have to be tellable apart from the payload alone, or every client polls a finished run.
        recommended_poll_after_ms=None if terminal else execution_poll(facts.status),
    )
    return Read(Answer(data=view))


@register_tool("aita_get_execution_steps")
def _get_execution_steps_tool(services: McpServices) -> ToolSpec:
    async def handler(
        context: Context,
        tenant_id: IdArg,
        execution_id: IdArg,
        limit: LimitArg = 20,
        cursor: CursorArg = None,
    ) -> CallToolResult:
        return await run_read(
            services,
            context,
            "aita_get_execution_steps",
            _get_execution_steps,
            arguments={"execution_id": execution_id, "limit": limit, "cursor": cursor},
            tenant_id=tenant_id,
            audited=True,
        )

    return ToolSpec(
        name="aita_get_execution_steps",
        description=(
            "Page through a run's steps in the order the case ran them. Every item carries its step id, "
            "number, action, status, duration, stable error code and evidence references; a step's own "
            "description, locator attempts and an assertion's expected/actual text are only sent when the "
            "project allows report details. Evidence is referenced, never fetched - there is no download "
            "tool, and the console link opens it."
        ),
        scope=SCOPE_READ,
        handler=handler,
        annotations=QUERY_ANNOTATIONS,
    )


def _get_execution_steps(uow: UnitOfWork, arguments: dict[str, Any]) -> Read:
    call = uow.call
    facts = _execution_facts(uow, str(arguments["execution_id"]))
    policy = _run_policy(uow, facts)
    details = content_open(policy, FLAG_REPORT_DETAILS)
    limit = page_size(arguments.get("limit"))
    page = Page(
        kind=cursors.KIND_STEPS,
        tenant_id=call.tenant_id,
        limit=limit,
        cursor=cursor_of(arguments.get("cursor")),
        project_id=facts.project_id,
        # The execution is the whole of the filter, and it belongs in the cursor's digest: a bookmark from
        # one run must not continue another one's steps (§11).
        filters={"execution_id": facts.execution_id},
    )
    after_no, after_id = page.after_index()
    rows = report_queries.step_page(
        uow.scope,
        tenant_id=call.tenant_id,
        execution_id=facts.execution_id,
        limit=limit + 1,
        after_step_no=after_no,
        after_id=after_id,
        details=details,
    )
    terminal = execution_terminal(facts.status)
    poll = None if terminal else execution_poll(facts.status)
    items = render([_step_ref(row, details=details) for row in page.kept(rows)])

    def build(kept: list[dict[str, Any]]) -> StepPage:
        return StepPage(
            execution_id=facts.execution_id,
            terminal=terminal,
            items=kept,
            details_available=details,
            recommended_poll_after_ms=poll,
        )

    answer = page.fit(rows, items, build, settings=call.settings, request_id=call.request_id)
    sent = len(answer.data["items"])
    audit = (
        Audit(
            resource_type="test_execution",
            resource_id=facts.execution_id,
            project_id=facts.project_id,
            detail={"flag": FLAG_REPORT_DETAILS, "steps": sent},
        )
        if details and sent > 0
        else None
    )
    return Read(answer, audit=audit)


@register_tool("aita_get_report")
def _get_report_tool(services: McpServices) -> ToolSpec:
    async def handler(context: Context, tenant_id: IdArg, execution_id: IdArg) -> CallToolResult:
        return await run_read(
            services,
            context,
            "aita_get_report",
            _get_report,
            arguments={"execution_id": execution_id},
            tenant_id=tenant_id,
            audited=True,
        )

    return ToolSpec(
        name="aita_get_report",
        description=(
            "Read a run's report: outcome, per-status step counts, which steps failed and their stable "
            "codes, the failure category, how complete the evidence is and the console links. A run that "
            "has not finished answers with its progress and base_report_ready=false rather than an error. "
            "Up to five failed steps also carry their description and the assertion's expected/actual when "
            "the project allows report details; the rest of them is the step page's."
        ),
        scope=SCOPE_READ,
        handler=handler,
        annotations=QUERY_ANNOTATIONS,
    )


def _get_report(uow: UnitOfWork, arguments: dict[str, Any]) -> Read:
    call = uow.call
    facts = _execution_facts(uow, str(arguments["execution_id"]))
    policy = _run_policy(uow, facts)
    details = content_open(policy, FLAG_REPORT_DETAILS)
    failures = report_queries.failure_steps(
        uow.scope, tenant_id=call.tenant_id, execution_id=facts.execution_id, details=details
    )
    terminal = execution_terminal(facts.status)
    view = ReportView(
        execution_id=facts.execution_id,
        tenant_id=facts.tenant_id,
        project_id=facts.project_id,
        status=facts.status,
        outcome=facts.outcome,
        terminal=terminal,
        base_report_ready=terminal,
        analysis_status=facts.analysis_status,
        artifact_status=facts.artifact_status,
        cleanup_status=facts.cleanup_status,
        error_code=facts.error_code,
        evidence_mode=facts.evidence_mode,
        failure_type=report_queries.latest_failure_type(
            uow.scope, tenant_id=call.tenant_id, execution_id=facts.execution_id
        ),
        step_counts=dict(facts.step_counts),
        failure_step_ids=[row.step_id for row in failures],
        details_available=details,
        artifact_summary=report_queries.artifact_summary(
            uow.scope, tenant_id=call.tenant_id, execution_id=facts.execution_id
        ),
        links=console_links(call.settings, execution_id=facts.execution_id),
        recommended_poll_after_ms=None if terminal else execution_poll(facts.status),
        # Absent, not empty, when the policy says so: an empty list would answer "nothing failed", and a
        # caller that believed it would report a red run as a green one (§6.6).
        **({"failure_summaries": [_failure_summary(row) for row in failures]} if details else {}),
    )
    audit = (
        Audit(
            resource_type="test_execution",
            resource_id=facts.execution_id,
            project_id=facts.project_id,
            detail={"flag": FLAG_REPORT_DETAILS, "failure_summaries": len(failures)},
        )
        if details and failures
        else None
    )
    return Read(Answer(data=view), audit=audit)


# --------------------------------------------------------------------------------------
# The execution reads' shared parts
# --------------------------------------------------------------------------------------


def _execution_facts(uow: UnitOfWork, execution_id: str) -> report_queries.ExecutionFacts:
    """One run's stored state, read by id inside this tenant - or the refusal that says it is not ours.

    Three bounded statements, not a loaded ORM graph: §8.2 forbids reading a run's whole step and
    evidence history to answer a question about its status, and `TestExecution.ir` and `snapshot` are the
    two largest documents on the row.
    """
    facts = report_queries.execution_facts(uow.scope, tenant_id=uow.call.tenant_id, execution_id=execution_id)
    if facts is None:
        raise _missing("execution", execution_id)
    return facts


def _run_policy(uow: UnitOfWork, facts: report_queries.ExecutionFacts) -> McpPolicy:
    """Authorise the run's project and read its policy - without refusing a project that switched MCP off.

    §14.1's 14-tool matrix makes these three reads the exception: a project that has disabled MCP still has
    runs in flight, and an assistant that cannot see their state cannot stop polling them. Nothing about
    that loosens admission - the tenant, resource and membership checks below are the same ones every
    other read runs, and an execution in a project this caller is not in still answers 403 or 404. What
    changes is only how much of the answer is projected, which is why this calls the authoriser directly
    instead of going through `open_project`.
    """
    row = authorised_project(uow.scope, uow.call, facts.project_id)
    return policy_of(row)


def _human_task(call: CallContext, facts: report_queries.ExecutionFacts) -> dict[str, Any] | None:
    """The person this run is waiting on, as far as §6.6 lets an assistant see them.

    Presence rather than `status == WAIT_HUMAN` is what the flag is asked for, and the two agree: a task
    is open exactly while a person is on the critical path, including the moment after they have pressed
    resume and before the worker has taken it. A `RESUME_REQUESTED` task reported as "no human needed"
    would have the assistant conclude the run was moving while it was still parked.
    """
    task = facts.human_task
    if task is None:
        return None
    return human_task_view(
        call.settings,
        task_id=task.task_id,
        step_id=task.step_id,
        reason=task.reason,
        deadline=task.deadline,
        execution_id=facts.execution_id,
    )


def _step_ref(row: report_queries.StepRow, *, details: bool) -> StepRef:
    """One step as far as this project's policy lets it be read: state always, prose by flag (§6.2)."""
    gated: dict[str, Any] = {}
    if details:
        gated = {
            "description": text(row.description),
            "locator_attempts": locator_attempt_view(row.locator_attempts),
            "expected": text(row.expected),
            "actual": text(row.actual),
            "resume_phase": row.resume_phase,
        }
    return StepRef(
        step_id=row.step_id,
        step_no=row.step_no,
        action=row.action,
        status=row.status,
        duration_ms=row.duration_ms,
        error_code=row.error_code,
        # Passed explicitly because a field the wire model never saw is dropped from the answer, and a
        # step with no evidence has to say so rather than look like it withheld one (§6.6).
        artifact_refs=list(row.artifact_refs),
        **gated,
    )


def _failure_summary(row: report_queries.FailureRow) -> dict[str, Any]:
    """One of a report's at most five failed steps (§8.2) - only ever built for a project that sends details."""
    return {
        "step_id": row.step_id,
        "step_no": row.step_no,
        "action": row.action,
        "status": row.status,
        "duration_ms": row.duration_ms,
        "error_code": row.error_code,
        "description": text(row.description),
        "expected": text(row.expected),
        "actual": text(row.actual),
    }


# --------------------------------------------------------------------------------------
# aita_run_test
# --------------------------------------------------------------------------------------


@register_tool("aita_run_test")
def _run_test_tool(services: McpServices) -> ToolSpec:
    async def handler(
        context: Context,
        tenant_id: IdArg,
        compile_artifact_id: IdArg,
        expected_ir_digest: DigestArg,
        environment_revision_id: IdArg,
        idempotency_key: IdempotencyArg,
        variables: VariablesArg,
        browser: BrowserArg = None,
        evidence_mode: EvidenceModeArg = None,
        use_server_ai: BooleanArg = False,
    ) -> CallToolResult:
        return await run_write(
            services,
            context,
            "aita_run_test",
            _run_test,
            arguments={
                "tenant_id": tenant_id,
                "compile_artifact_id": compile_artifact_id,
                "expected_ir_digest": expected_ir_digest,
                "environment_revision_id": environment_revision_id,
                "idempotency_key": idempotency_key,
                "variables": dict(variables),
                "browser": browser,
                "evidence_mode": evidence_mode,
                "use_server_ai": use_server_ai,
            },
            tenant_id=tenant_id,
        )

    return ToolSpec(
        name="aita_run_test",
        description=(
            "Run one compiled artifact in one environment revision and answer as soon as the run is queued. "
            "Everything the run executes against is named by the call - the artifact, the IR digest reviewed "
            "for it, and the environment revision - so a case edited in between cannot change what starts. "
            "The run drives a real browser against the real target site and can have real side effects there; "
            "the idempotency_key deduplicates the run this platform starts, and nothing about the site. "
            "use_server_ai is off unless asked for, and is refused rather than quietly ignored. Poll with "
            "aita_get_execution; the answer also carries where a person watches the run."
        ),
        scope=SCOPE_RUN,
        handler=handler,
        annotations=RUN_ANNOTATIONS,
    )


def _run_test(uow: UnitOfWork, arguments: dict[str, Any]) -> Answer:
    artifact_id = str(arguments["compile_artifact_id"])
    artifact = CompileRepository(uow.scope, uow.call.tenant_id).by_id(artifact_id)
    if artifact is None:
        raise _missing("compile artifact", artifact_id)
    project_id = str(artifact.project_id)
    environment_revision_id = str(arguments["environment_revision_id"])
    use_ai = bool(arguments["use_server_ai"])
    # The gate records the policy it actually passed and the command stores that in the run's snapshot: what
    # the worker inherits is the agreement this call was let through by, not a flag the adapter asserts after
    # the fact (§6.4).
    honoured: dict[str, bool] = {}

    def refuse_new_intent(policy: McpPolicy) -> None:
        require_live_environment(uow, environment_revision_id)
        # §6.4 asks for the effective policy in the snapshot of every MCP run, not only of the one that named
        # the model: it is the half the worker takes the intersection against when vision comes up later.
        honoured["allow_server_ai"] = bool(policy.allow_server_ai)
        if use_ai:
            require_server_ai(
                uow.call.settings,
                policy,
                project_id=project_id,
                tool="aita_run_test",
                purpose="vision",
                fallback="the model-free run",
            )

    result = execution_commands.run_execution(
        uow,
        compile_artifact_id=artifact_id,
        case_id=None,
        revision_id=None,
        environment_id=None,
        environment_revision_id=environment_revision_id,
        variables=dict(arguments.get("variables") or {}),
        browser=arguments.get("browser"),
        evidence_mode=_evidence_mode_of(arguments.get("evidence_mode")),
        expected_ir_digest=str(arguments["expected_ir_digest"]),
        idempotency_key=str(arguments["idempotency_key"]),
        use_server_ai=use_ai,
        ai_policy=honoured,
        gate=write_gate(uow, project_id, tool="aita_run_test", checks=refuse_new_intent),
    )
    return Answer(data=_run_receipt(uow, result, use_server_ai=use_ai))


def require_live_environment(uow: UnitOfWork, revision_id: str) -> None:
    """§6.4: a new run may not name an environment that has since been archived.

    The platform's own run path takes a named revision at its word, so this is checked here and not inside
    the command: archiving is a precondition of a *new* intent only, and §9.3.1 puts such preconditions after
    the replay decision so a run that started while the environment was open keeps its answer.
    """
    environments = EnvironmentRepository(uow.scope, uow.call.tenant_id)
    revision = environments.require_revision(revision_id)
    environment = environments.by_id(revision.environment_id)
    if environment is not None and environment.archived_at is not None:
        raise ApiError(
            ErrorCode.SEMANTIC_ERROR,
            "This environment is archived, so a new run cannot be started against it",
            details={"environment_id": environment.id, "environment_revision_id": revision.id},
        )


def _evidence_mode_of(value: Any) -> str | None:
    """The one normalisation this adapter does, and it is the one the stored column already makes (§6.6)."""
    if value is None:
        return None
    if isinstance(value, Sensitivity):
        return value.value
    return str(value).upper()


def _worker_available(uow: UnitOfWork) -> bool:
    """The one statement §13.5 lets this answer make: a Worker has been seen recently, nothing more.

    It is a heartbeat read, not a probe of the broker or of the pool, and it never decides whether the run is
    accepted - a deployment with no Worker alive still queues the run and says so.
    """
    ttl = int(uow.call.settings.lease_ttl_seconds)
    return bool(WorkerLeaseRepository(uow.scope, uow.call.tenant_id).live_workers(ttl_seconds=ttl))


def _run_receipt(uow: UnitOfWork, result: Mapping[str, Any], *, use_server_ai: bool) -> RunReceipt:
    """The run as the row holds it now, plus the two things only this call knows (§6.6, §9.4).

    A replay reads the live row, so an hour-old key answers with the run's current state rather than with
    `QUEUED` forever. What is taken from the call instead of from the row is the AI intent, and only because
    the row cannot tell it: the worker decides vision and analysis later, from the snapshot this call wrote.
    """
    facts = _execution_facts(uow, str(result["id"]))
    terminal = execution_terminal(facts.status)
    return RunReceipt(
        execution_id=facts.execution_id,
        tenant_id=facts.tenant_id,
        project_id=facts.project_id,
        status=facts.status,
        outcome=facts.outcome,
        compile_artifact_id=str(facts.compile_artifact_id),
        ir_digest=facts.ir_digest,
        environment_revision_id=facts.environment_revision_id,
        evidence_mode=facts.evidence_mode,
        effective_server_ai=use_server_ai,
        worker_available=_worker_available(uow),
        recommended_poll_after_ms=(None if terminal else execution_poll(facts.status)),
        links=console_links(uow.call.settings, execution_id=facts.execution_id),
    )


# --------------------------------------------------------------------------------------
# aita_cancel_execution
# --------------------------------------------------------------------------------------


@register_tool("aita_cancel_execution")
def _cancel_execution_tool(services: McpServices) -> ToolSpec:
    async def handler(
        context: Context,
        tenant_id: IdArg,
        execution_id: IdArg,
        idempotency_key: IdempotencyArg,
        reason: CancelReasonArg = "requested",
    ) -> CallToolResult:
        return await run_write(
            services,
            context,
            "aita_cancel_execution",
            _cancel_execution,
            arguments={
                "tenant_id": tenant_id,
                "execution_id": execution_id,
                "idempotency_key": idempotency_key,
                "reason": reason,
            },
            tenant_id=tenant_id,
        )

    return ToolSpec(
        name="aita_cancel_execution",
        description=(
            "Ask for a run to stop, and answer with what the run says now. The worker holding the run decides "
            "when it ends, so a stop is requested here and not guaranteed to have happened yet. Needs an "
            "idempotency_key: the same key keeps the original answer, so a retry cannot turn a run that "
            "finished on its own into a cancelled one. Works even after the project closed MCP, because a run "
            "already in flight still has to be stoppable."
        ),
        scope=SCOPE_RUN,
        handler=handler,
        annotations=CANCEL_ANNOTATIONS,
    )


def _cancel_execution(uow: UnitOfWork, arguments: dict[str, Any]) -> Answer:
    execution_id = str(arguments["execution_id"])
    # Read the run first for the tenant answer (§14.1); the command then asks the narrower question this tool
    # is about - whether this caller may stop it - and no MCP policy gate runs here at all (§6.2).
    _execution_facts(uow, execution_id)
    result = execution_commands.cancel_execution(
        uow,
        execution_id=execution_id,
        reason=str(arguments.get("reason") or "requested"),
        idempotency_key=str(arguments["idempotency_key"]),
    )
    return Answer(data=_cancel_receipt(uow, result))


def _cancel_receipt(uow: UnitOfWork, result: Mapping[str, Any]) -> CancelReceipt:
    """This call's decision, kept as the record holds it, next to the run's state read now (§6.6, §9.4).

    `cancel_requested` is deliberately not re-derived: §9.4 promises a replay the answer the first call got.
    Both formats that can be stored here carry the field - the atomic result this command writes, and the
    bare REST summary a pre-upgrade record holds.
    """
    facts = _execution_facts(uow, str(result["id"]))
    terminal = execution_terminal(facts.status)
    return CancelReceipt(
        execution_id=facts.execution_id,
        cancel_requested=bool(result["cancel_requested"]),
        status=facts.status,
        terminal=terminal,
        outcome=facts.outcome,
        recommended_poll_after_ms=(None if terminal else execution_poll(facts.status)),
    )
