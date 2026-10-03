"""The three static specification resources (§7.1).

These are help documents, not data: they carry no tenant, no project, no case and no report, so they
read nothing from the database, take no execution slot, hold no admission lease and write no audit row.
What they do carry is a promise about the platform's own vocabulary, and a promise copied out of the
parser by hand goes stale the first time the DSL changes. Every list below is taken from the contract
that enforces it - `STEP_CLASSES`, the IR models' own fields, the parser's front-matter and target key
sets, and the two error enums - so a document that names an action the compiler would reject is a build
fault rather than a page of bad advice.

There are no dynamic resource URIs and no prompts (§7.1): one fact about a case would otherwise live
behind two permission systems, and the tools are the ones that answer for it.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Literal, get_args, get_origin

from ..compiler.markdown_dsl import FRONT_MATTER_FIELDS
from ..compiler.normalize import TARGET_FIELDS
from ..domain.errors import ErrorCode
from ..ir.models import STEP_CLASSES, Condition, LocatorCandidate, StepBase, TestIR
from .errors import AdapterCode, NextAction
from .server import available_tool_names

DSL_URI = "aita://docs/dsl/1.0"
ERRORS_URI = "aita://docs/errors/1.0"
WORKFLOW_URI = "aita://docs/workflow/1.0"

DOC_MIME_TYPE = "text/markdown"

#: A static document is served whole, so its size is part of what this build promises. This is a build
#: bound rather than the configured per-response budget: a document that fits here must not become a
#: refusal because one caller asked for a smaller response ceiling.
MAX_DOC_BYTES = 32 * 1024

#: The step fields every case gets from the compiler, so the DSL document does not list them as things
#: an author has to write.
_COMPILER_SUPPLIED = frozenset({"action", "id", "source"})


@dataclass(frozen=True)
class ResourceSpec:
    """One advertised document: its URI, its discovery metadata and how its bytes are produced."""

    uri: str
    name: str
    title: str
    description: str
    render: Callable[[], str]


def literal_values(model: type, field: str) -> tuple[str, ...]:
    """The accepted values of a field the schema declares as a `Literal`.

    Read rather than transcribed: an authoring guide that quietly falls behind `Condition.kind` is worse
    than no guide at all.
    """
    annotation = model.model_fields[field].annotation
    if get_origin(annotation) is not Literal:
        raise AssertionError(f"{model.__name__}.{field} is not a Literal; this document would drift")
    return tuple(str(value) for value in get_args(annotation))


def _step_fields(cls: type[StepBase]) -> tuple[str, ...]:
    return tuple(
        name for name, field in cls.model_fields.items() if name not in _COMPILER_SUPPLIED and field.is_required()
    )


def _step_count_bound(which: str) -> int:
    """The schema's own step-count bound, so the document cannot outstate `TestIR`."""
    attribute = f"{which}_length"
    for constraint in TestIR.model_fields["steps"].metadata:
        value = getattr(constraint, attribute, None)
        if value is not None:
            return int(value)
    raise AssertionError(f"TestIR.steps carries no {which} bound")


def dsl_document() -> str:
    """The authoring contract, line by line from the parser and the IR schema that check it."""
    actions = "\n".join(
        f"- `{name}` - requires "
        f"{', '.join(f'`{field}`' for field in _step_fields(cls)) if _step_fields(cls) else 'no other field'}"
        for name, cls in STEP_CLASSES.items()
    )
    front_matter = ", ".join(f"`{field}`" for field in sorted(FRONT_MATTER_FIELDS))
    target_keys = ", ".join(f"`{key}`" for key in sorted(TARGET_FIELDS))
    condition_kinds = ", ".join(f"`{kind}`" for kind in literal_values(Condition, "kind"))
    strategies = ", ".join(f"`{strategy}`" for strategy in literal_values(LocatorCandidate, "strategy"))
    return f"""# The case authoring DSL

Version 1.0. A case is one Markdown document. The compiler is deterministic: the same bytes always
produce the same IR, and no step is inferred from prose.

## Structure

1. A front-matter block at the very top, delimited by `---` lines. Its only fields are {front_matter}.
2. Exactly one `# Case title` heading. The title is taken from that heading, not from the front matter.
3. One `## Step <n>` heading per step, numbered from 1 in document order with no gaps and no repeats. A
   heading that is not a step heading closes the step above it.
4. Inside each step, one fenced `yaml` block, or the plain `key: value` lines the parser folds into one.

## Actions

{actions}

Each step may also set `timeout_ms`, and `human_policy` where a person has to take over.

## Targets

A target names what to act on. `description` is required and is what a locator is judged against; the
rest says how to find it.

- Recognised keys under `target:` - {target_keys}.
- The four locator strategies are {strategies}. Each takes its own fields and must not carry another
  strategy's; `css` and `xpath` want `selector`, `role` wants `role` plus `name`, `text` wants `text`.
- At most 12 candidates per target, tried in the order written. `allow_vision: true` lets the executor
  fall back to visual targeting for that step.

## Conditions

`assert` takes a condition, and `wait` takes one instead of a duration. The kinds are
{condition_kinds}. A condition carries `expected`, `target`, or both, according to its kind - the
compiler reports `CONDITION_INVALID` for any other combination, and `wait` requires exactly one of
`condition` and `duration_ms`.

## Values and variables

- A literal scalar, or a reference written `${{env.NAME}}`, `${{vars.NAME}}` or `${{secrets.NAME}}`.
- `env` is the environment revision's configuration, `vars` are the case's own declared variables, and
  `secrets` are named secret versions.
- A `${{secrets.*}}` reference may appear in an input value and nowhere else, and its resolved text
  never comes back in a report, a step's actual value or an artifact.
- Declare a variable under `variables:` in the front matter; using one that is not declared is a
  `VARIABLE_UNDECLARED` diagnostic.

## Bounds

- Between {_step_count_bound("min")} and {_step_count_bound("max")} steps per case.
- A step's YAML block is refused rather than truncated once it passes the compiler's per-field size.
- `defaults.timeout_ms` and a step's `timeout_ms` are positive milliseconds; a step inherits the default.

## Example

````markdown
---
dsl_version: "1.0"
tags: [smoke]
variables:
  username:
    type: string
    required: true
defaults:
  timeout_ms: 8000
---
# 登录冒烟测试

## Step 1
```yaml
action: open
url: "${{env.base_url}}/index.html"
```

## Step 2
```yaml
action: input
target:
  description: 用户名输入框
  type: input
  css: 'input[name="username"]'
value: "${{vars.username}}"
```

## Step 3
```yaml
action: assert
condition:
  kind: page_contains
  expected: 欢迎回来
```
````
"""


def errors_document() -> str:
    """Every code this server can answer with, and what the client does next."""
    missing = sorted(code.value for code in AdapterCode if code not in ADVICE)
    if missing:
        raise AssertionError(f"no advice for {', '.join(missing)}; the error document would omit a code")
    adapter = "\n".join(f"| `{code.value}` | {ADVICE[code]} |" for code in sorted(ADVICE, key=lambda item: item.value))
    actions = "\n".join(f"- `{action.value}` - {ACTION_ADVICE[action]}" for action in NextAction)
    runtime = "\n".join(f"- `{code.value}` - {RUNTIME_ADVICE[code]}" for code in RUNTIME_CODES)
    return f"""# Error codes and recovery

## The envelope

Every tool answers with one JSON document, carried identically in `content[0].text` and in
`structuredContent`:

```json
{{"schema_version": "1.0", "ok": false, "request_id": "req_...", "data": null,
 "error": {{"code": "...", "message": "...", "retryable": true, "retry_after_ms": 1000,
 "next_action": "retry_same_key_or_query", "details": {{}}}},
 "truncated": false, "next_cursor": null}}
```

A test that ended `FAILED` or `ERROR` is ordinary data inside a successful answer, not a tool error; a
tool error means the call itself did not do what was asked.

## Codes this adapter adds

| Code | What it means, and what to do |
|---|---|
{adapter}

## Compile codes

The codes a case authoring mistake produces - the `DSL_*`, `STEP_*`, `TARGET_*`, `CONDITION_*`,
`VARIABLE_*`, `FRONT_MATTER_*`, `CASE_*`, `ACTION_*`, `IR_*` and `AI_*` names - do not arrive as tool
errors at all. A compile that finished with problems is data: `aita_get_compilation` answers with the
artifact's `diagnostics`, each carrying a code, a line, the offending field and a remedy.

## Run codes

These reach a run's `error_code`, one step's `error_code`, or a write refusal:

{runtime}

`FORBIDDEN`, `NOT_FOUND`, `VERSION_CONFLICT`, `IDEMPOTENCY_CONFLICT`, `RATE_LIMITED` and
`DEPENDENCY_UNAVAILABLE` are answered by the reads too. A resource that belongs to another tenant is
`NOT_FOUND` rather than a refusal that would confirm it exists.

## What to do next

{actions}

`retryable` says whether asking again can work. `next_action` says whether the *key* may change: a write
that timed out or lost its commit is retried with the same `idempotency_key`, because a new key would be
a second intent.
"""


def workflow_document() -> str:
    """The call order, named only by the operations, so nothing here advertises a tool twice."""
    # The registry is filled by importing the adapters, and that import is `build_mcp_server`'s business,
    # not this module's. Rendering without it would mark every step "not advertised" and read as a build
    # that had forgotten its own tools, so the document takes the import itself.
    from . import tools  # noqa: F401

    steps = "\n".join(_workflow_line(name, purpose) for name, purpose in WORKFLOW)
    return f"""# The closed loop

Nothing in this sequence needs an internal model: the platform compiles, runs and reports, and the
assistant reads what came back.

{steps}

## Rules that hold throughout

- Every write carries an `idempotency_key` and a `tenant_id`. Replaying the same key with the same body
  returns the original result; the same key with a different body is `IDEMPOTENCY_CONFLICT`.
- A revision is replaced at the `expected_row_version` the last read reported. A mismatch is
  `VERSION_CONFLICT`, and the fix is to read again, not to force the write.
- Saving a case queues a deterministic compile. Ask for it with `aita_get_compilation`; do not compile
  twice to find out what happened.
- A `NEEDS_REVIEW` artifact is confirmed by a person in the console against the exact `ir_digest`. There
  is no tool that confirms it and no `human_confirmed` argument; executability is read from the artifact.
- Reading a run is polling with a suggestion: a non-terminal answer carries `recommended_poll_after_ms`,
  a terminal one carries none. Only `FINISHED` is terminal.
- A project whose MCP access is switched off still answers `aita_get_context` and `aita_list_projects`,
  and still lets an existing run be read; the reads that would show case content are refused.
- Evidence is not downloadable through MCP. A report names its steps and their artifact references; the
  console is where a screenshot is opened.
"""


#: The order §7.2 documents, as (tool name, the one thing it is for). It is rendered against the tool
#: registry, so a step this build has not implemented is marked rather than promised, and a tool that
#: exists but is missing here is what the resources test fails on.
WORKFLOW: tuple[tuple[str, str], ...] = (
    ("aita_get_context", "who I am, which tenants I belong to, and what this deployment allows"),
    ("aita_list_projects", "the projects in the selected tenant, newest first"),
    ("aita_list_environments", "the environment revisions a run may point at"),
    ("aita_list_cases", "the cases, newest first, each with its latest compile summary"),
    ("aita_get_case", "one case, and its Markdown only when the project lets that be read"),
    ("aita_create_case", "save a new case as a revision; this queues the deterministic compile"),
    ("aita_add_case_revision", "replace a case's content at the version the last read reported"),
    ("aita_get_compilation", "the newest compile attempt for a revision, with its diagnostics"),
    ("aita_compile_case_revision", "queue a compile of one revision, deterministically or with the platform's model"),
    ("aita_run_test", "run a confirmed artifact in one environment revision"),
    ("aita_get_execution", "the run's status, step counts and readiness"),
    ("aita_get_execution_steps", "the steps in document order, as far as the policy allows"),
    ("aita_get_report", "the aggregate report and where a person opens the evidence"),
    ("aita_cancel_execution", "ask for a clean stop of a run that is still going"),
)

#: One line per adapter code. The document raises a build fault rather than serving a table that omits
#: a code, because a code a client meets with no advice is a code it cannot recover from.
ADVICE: Mapping[AdapterCode, str] = {
    AdapterCode.TENANT_SELECTION_CONFLICT: (
        "the `tenant_id` argument and the selected tenant disagree; re-read `aita_get_context`"
    ),
    AdapterCode.MCP_PROJECT_DISABLED: (
        "this project's MCP access is off; an administrator switches it in the console"
    ),
    AdapterCode.DATA_POLICY_DENIED: "the project is open but keeps this content to itself; read the metadata",
    AdapterCode.AI_DISABLED: "server-side AI is off for this project; compile deterministically",
    AdapterCode.COMMAND_BUSY: "no slot is free; wait `retry_after_ms`, with the same idempotency key",
    AdapterCode.IDEMPOTENCY_RESULT_UNKNOWN: (
        "the platform cannot prove whether an older record was written; replay the same key to find out"
    ),
    AdapterCode.TOOL_DEADLINE_EXCEEDED: (
        "the call ran out of budget; the write may already have committed, so query it or replay the key"
    ),
    AdapterCode.RESULT_TOO_LARGE: "the answer does not fit even at one row; ask for less or lower `limit`",
    AdapterCode.UNAUTHENTICATED: "the credential did not verify for this resource; authenticate again",
    AdapterCode.FORBIDDEN: "the credential lacks the scope or the role this operation needs",
    AdapterCode.NOT_FOUND: "no such resource for this caller; foreign and missing are the same answer",
    AdapterCode.VALIDATION_ERROR: "an argument is outside the published schema or bounds; fix it",
    AdapterCode.VERSION_CONFLICT: "`expected_row_version` is stale; read the case again, then reform the write",
    AdapterCode.IDEMPOTENCY_CONFLICT: (
        "this key already carried a different body; the original stands, and a new intent takes a new key"
    ),
    AdapterCode.RATE_LIMITED: "this caller's budget is spent; wait `retry_after_ms`",
    AdapterCode.DEPENDENCY_UNAVAILABLE: (
        "a dependency refused the work and nothing was sent, so the same request may be made again"
    ),
    AdapterCode.INTERNAL: "the platform failed inside; the `request_id` is what an operator needs",
}

ACTION_ADVICE: Mapping[NextAction, str] = {
    NextAction.none: "nothing; the answer is the result",
    NextAction.retry_same_key_or_query: "retry with the same idempotency key, or query for the result",
    NextAction.query_current_policy_and_etag: "read the current version and its policy before writing again",
    NextAction.reauth_with_correct_audience: "authenticate against this resource server's audience",
    NextAction.reauthorize_scope: "ask for a token that carries the scope this operation names",
    NextAction.review_in_console: "a person decides this one in the console",
    NextAction.poll_again: "ask again after `recommended_poll_after_ms`",
    NextAction.narrow_request: "ask for less: a smaller page, or without the content flags",
    NextAction.fix_input: "change the input; asking again as-is fails again",
}

#: The codes a reader of a run actually meets, listed because the document says what each one is about
#: rather than dumping a name per line.
RUNTIME_CODES: tuple[ErrorCode, ...] = (
    ErrorCode.COMPILE_FAILED,
    ErrorCode.COMPILE_REVIEW_REQUIRED,
    ErrorCode.COMPILE_STALE_DIGEST,
    ErrorCode.CASE_ARCHIVED,
    ErrorCode.ENVIRONMENT_REQUIRED,
    ErrorCode.DOMAIN_NOT_ALLOWED,
    ErrorCode.VARIABLE_MISSING,
    ErrorCode.VARIABLE_TYPE_INVALID,
    ErrorCode.SECRET_UNAVAILABLE,
    ErrorCode.SECRET_VERSION_REVOKED,
    ErrorCode.ATTACHMENT_NOT_READY,
    ErrorCode.BROWSER_NOT_SUPPORTED,
    ErrorCode.QUOTA_EXCEEDED,
    ErrorCode.BROWSER_START_FAILED,
    ErrorCode.BROWSER_CRASHED,
    ErrorCode.ACTION_OUTCOME_UNKNOWN,
    ErrorCode.ASSERTION_FAILED,
    ErrorCode.LOCATOR_NOT_FOUND,
    ErrorCode.LOCATOR_AMBIGUOUS,
    ErrorCode.LOCATOR_NOT_INTERACTABLE,
    ErrorCode.EVIDENCE_CAPTURE_FAILED,
    ErrorCode.SESSION_LOST,
    ErrorCode.STATE_STORE_UNAVAILABLE,
    ErrorCode.LEASE_LOST,
    ErrorCode.CANCELLED,
    ErrorCode.QUEUE_TIMEOUT,
    ErrorCode.ACTIVE_TIMEOUT,
    ErrorCode.HUMAN_WAIT_TIMEOUT,
    ErrorCode.HUMAN_TASK_TAKEN,
    ErrorCode.HUMAN_SESSION_LOST,
    ErrorCode.EGRESS_BLOCKED,
    ErrorCode.TARGET_HTTP_ERROR,
    ErrorCode.STEP_NOT_FOUND,
    ErrorCode.EXECUTION_NOT_RUNNABLE,
    ErrorCode.UNSUPPORTED_TARGET_SCOPE,
    ErrorCode.UNEXPECTED_DIALOG,
    ErrorCode.ANALYSIS_FAILED,
)

RUNTIME_ADVICE: Mapping[ErrorCode, str] = {
    ErrorCode.COMPILE_FAILED: "the revision has no executable IR; read its diagnostics",
    ErrorCode.COMPILE_REVIEW_REQUIRED: "a person confirms the exact `ir_digest` in the console",
    ErrorCode.COMPILE_STALE_DIGEST: "the artifact no longer matches the revision; compile it again",
    ErrorCode.CASE_ARCHIVED: "the case is archived; unarchive it or author a new one",
    ErrorCode.ENVIRONMENT_REQUIRED: "run against an environment revision from `aita_list_environments`",
    ErrorCode.DOMAIN_NOT_ALLOWED: "the target host is outside the environment's allow-list",
    ErrorCode.VARIABLE_MISSING: "supply the variable the case declares as required",
    ErrorCode.VARIABLE_TYPE_INVALID: "the value does not match the declared variable type",
    ErrorCode.SECRET_UNAVAILABLE: "the named secret version is not present in this environment",
    ErrorCode.SECRET_VERSION_REVOKED: "that secret version was revoked; point at a live one",
    ErrorCode.ATTACHMENT_NOT_READY: "the upload has not finished; ask again later",
    ErrorCode.BROWSER_NOT_SUPPORTED: "the environment names a browser this build cannot run",
    ErrorCode.QUOTA_EXCEEDED: "the tenant's execution quota is spent",
    ErrorCode.BROWSER_START_FAILED: "the browser did not start; the run is terminal",
    ErrorCode.BROWSER_CRASHED: "the browser died mid-run; the run is terminal",
    ErrorCode.ACTION_OUTCOME_UNKNOWN: "one step's effect is unknown; the run is terminal",
    ErrorCode.ASSERTION_FAILED: "an `assert` did not hold; this is a failed test, not a failed call",
    ErrorCode.LOCATOR_NOT_FOUND: "no candidate matched; the step needs a locator",
    ErrorCode.LOCATOR_AMBIGUOUS: "several elements matched one candidate; narrow it",
    ErrorCode.LOCATOR_NOT_INTERACTABLE: "the element exists but cannot be acted on yet",
    ErrorCode.EVIDENCE_CAPTURE_FAILED: "the screenshot or trace could not be written",
    ErrorCode.SESSION_LOST: "the execution session is gone; the run is terminal",
    ErrorCode.STATE_STORE_UNAVAILABLE: "the state store refused the step's write",
    ErrorCode.LEASE_LOST: "another replica holds the run; read its status instead",
    ErrorCode.CANCELLED: "a clean stop was asked for and reached",
    ErrorCode.QUEUE_TIMEOUT: "no worker took the run within the queue budget",
    ErrorCode.ACTIVE_TIMEOUT: "the run passed its active time budget",
    ErrorCode.HUMAN_WAIT_TIMEOUT: "nobody took the human step before its deadline",
    ErrorCode.HUMAN_TASK_TAKEN: "another person is already resolving that step",
    ErrorCode.HUMAN_SESSION_LOST: "the person's takeover session ended before it was verified",
    ErrorCode.EGRESS_BLOCKED: "the browser was stopped from reaching an address outside the allow-list",
    ErrorCode.TARGET_HTTP_ERROR: "the site answered an HTTP error the case did not expect",
    ErrorCode.STEP_NOT_FOUND: "the step id is not one this run had",
    ErrorCode.EXECUTION_NOT_RUNNABLE: "the artifact is not executable; read the compilation",
    ErrorCode.UNSUPPORTED_TARGET_SCOPE: "the case reaches a frame or window this build does not drive",
    ErrorCode.UNEXPECTED_DIALOG: "a native dialog appeared where the case did not expect one",
    ErrorCode.ANALYSIS_FAILED: "the failure analysis did not complete; the base report still stands",
}


def _workflow_line(name: str, purpose: str) -> str:
    suffix = "" if name in available_tool_names() else " _(not advertised by this build)_"
    return f"- `{name}` - {purpose}{suffix}"


def resource_specs() -> tuple[ResourceSpec, ...]:
    return (
        ResourceSpec(
            uri=DSL_URI,
            name="aita-dsl",
            title="Case authoring DSL",
            description="The Markdown structure and the actions, targets, conditions and values they may use.",
            render=dsl_document,
        ),
        ResourceSpec(
            uri=ERRORS_URI,
            name="aita-errors",
            title="Error codes and recovery",
            description="Every code this server answers with, what it means, and what the client does next.",
            render=errors_document,
        ),
        ResourceSpec(
            uri=WORKFLOW_URI,
            name="aita-workflow",
            title="The author-compile-run-report loop",
            description="The call order from saving a case to reading its report, with the replay rules.",
            render=workflow_document,
        ),
    )


def document_for(uri: str) -> str:
    """One document's text, checked against the build bound here rather than at import time."""
    for spec in resource_specs():
        if spec.uri == uri:
            text = spec.render()
            size = len(text.encode("utf-8"))
            if size > MAX_DOC_BYTES:
                raise AssertionError(f"{uri} renders {size} bytes, over the {MAX_DOC_BYTES}-byte document bound")
            return text
    raise KeyError(uri)


def documents() -> dict[str, str]:
    return {spec.uri: document_for(spec.uri) for spec in resource_specs()}
