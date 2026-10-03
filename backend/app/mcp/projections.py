"""What may leave the platform, in what shape, and how big it may be (§5.5, §6.6, §8.1, §11).

The policy is re-read for every answer and applied *before* anything is serialised: trimming afterwards
would mean the sensitive bytes were already in the response, and one leaked field is enough to make the
promise false. The DTOs themselves are in `schemas.py`; the rule that a nullable field is `null` while a
policy-hidden field is absent from the object entirely is enforced there by `Wire`, and what is here is
the part that decides which of the two applies.
"""

from __future__ import annotations

import random
from datetime import datetime
from typing import Any

from ..config import Settings
from ..db.base import coerce_utc, utcnow
from ..domain.enums import CompileStatus, ExecutionStatus
from ..domain.mcp_policy import McpPolicy
from .errors import AdapterCode, NextAction, ToolFailure

#: §11 - a list page's default and its hard ceiling; the argument model enforces the range.
DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100
#: §11 - one free-text field in a summary, in characters. Full Markdown is not a summary and is not clipped.
FREE_TEXT_LIMIT = 2000
#: §6.6 - beyond this many review items the answer points at the console instead.
REVIEW_ITEMS_LIMIT = 20
#: §6.6 - diagnostics are paged, and this is where the page size is checked.
DIAGNOSTICS_LIMIT = 20

#: §6.3 - `NOT_CREATED` is a *query result*, not a `CompileStatus`: it says this revision has no compile
#: attempt on record yet, which is a statement about what the caller asked for, not about a compiler.
LOOKUP_FOUND = "FOUND"
LOOKUP_NOT_CREATED = "NOT_CREATED"

#: The statuses no poll will change: the attempt finished, and only a person or a new attempt moves it (§8.1).
SETTLED_COMPILE_STATUSES = frozenset(
    {CompileStatus.SUCCEEDED.value, CompileStatus.NEEDS_REVIEW.value, CompileStatus.FAILED.value}
)

#: The keys of a compiler diagnostic that carry a stable code or a position rather than prose (§6.6).
DIAGNOSTIC_METADATA_KEYS = ("code", "severity", "step_id", "source_range", "field")
#: The same for a review item: `source_text` is the case's own words and `generated` is the IR built from
#: them, so both are case content and neither survives a closed `allow_case_content`.
REVIEW_METADATA_KEYS = ("step_id", "source_range", "reason", "model", "prompt_version")

#: §8.1 - the polling suggestions, as ranges rather than one number, so a fleet of clients does not
#: wake up in lockstep on the same artifact.
FIRST_COMPILE_POLL_MS = 1000
LATER_COMPILE_POLL_MS = (2000, 5000)
EXECUTION_POLL_MS = (2000, 5000)
#: A run parked on a person is not worth checking every two seconds: the wait is minutes, and the poll
#: is what tells the assistant it is still waiting, not what unblocks it.
WAIT_HUMAN_POLL_MS = (5000, 10000)
#: How long a compilation counts as "just asked for", which is the one case §8.1 lets poll at 1 s.
FIRST_COMPILE_WINDOW_SECONDS = 2.0

#: The four §5.5 flags, named here so a tool cannot gate content on a flag that does not exist.
FLAG_ENABLED = "enabled"
FLAG_CASE_CONTENT = "allow_case_content"
FLAG_REPORT_DETAILS = "allow_report_details"
FLAG_SERVER_AI = "allow_server_ai"


def page_size(value: int | None) -> int:
    return DEFAULT_PAGE_SIZE if value is None else max(1, min(MAX_PAGE_SIZE, value))


def clip(value: str | None, *, limit: int = FREE_TEXT_LIMIT) -> tuple[str | None, bool]:
    """Bound one free-text field, and say whether it was bound at all."""
    if value is None:
        return None, False
    if len(value) <= limit:
        return value, False
    return value[:limit], True


def diagnostic_view(raw: Any, *, content_allowed: bool) -> dict[str, Any]:
    """One compiler diagnostic as it may leave the platform (§6.6)."""
    return _entry(raw, DIAGNOSTIC_METADATA_KEYS, content_allowed=content_allowed)


def review_view(raw: Any, *, content_allowed: bool) -> dict[str, Any]:
    """One review item, whose two prose fields are the case's own text and the IR built from it (§6.6)."""
    return _entry(raw, REVIEW_METADATA_KEYS, content_allowed=content_allowed)


def review_page(items: Any, *, content_allowed: bool) -> tuple[list[dict[str, Any]], bool]:
    """At most twenty review items, plus whether there were more than the answer could carry (§6.6).

    The bound is a count rather than a byte size, so `review_required` is a statement about the artifact and
    not about how much room this response happened to have: the console is where a person reads all of them.
    """
    known = [item for item in (items or []) if isinstance(item, dict)]
    return (
        [review_view(item, content_allowed=content_allowed) for item in known[:REVIEW_ITEMS_LIMIT]],
        len(known) > REVIEW_ITEMS_LIMIT,
    )


def _entry(raw: Any, keep: tuple[str, ...], *, content_allowed: bool) -> dict[str, Any]:
    """One JSON entry from a compile artifact, projected through the project's content policy (§5.5).

    An allow-list rather than a deny-list, and deliberately so: the compiler may one day add a field that
    quotes the case, and a deny-list would send it out the door the day it lands while this one refuses it.
    """
    if not isinstance(raw, dict):
        return {}
    if content_allowed:
        return {
            key: (clip(value)[0] if isinstance(value, str) else value)
            for key, value in raw.items()
            if value is not None
        }
    return {key: raw[key] for key in keep if key in raw}


def require_mcp_enabled(project_id: str, policy: McpPolicy, *, tool: str) -> None:
    """Refuse a content-bearing tool while the project's MCP gate is shut (§6.2).

    `enabled` outranks every content flag: a leftover `allow_report_details=true` from before the
    project was closed must not become a way back in, and a policy that could not be parsed arrives
    here already reading as fully closed (§5.5).
    """
    if not policy.enabled:
        raise ToolFailure(
            AdapterCode.MCP_PROJECT_DISABLED,
            "MCP is not enabled for this project; an administrator can enable it in the platform console",
            details={"project_id": project_id, "tool": tool, "mcp_enabled": False},
        )


def require_content(policy: McpPolicy, *, tool: str, flag: str, project_id: str | None = None) -> None:
    """Refuse an explicit request for content the project keeps to itself (§5.5, §6.2).

    Asking for `include_markdown` on a project that has not agreed to send case content is a refusal,
    not a silent no-op: an assistant that quietly received an empty string would conclude the case is
    empty and rewrite it.
    """
    if not getattr(policy, flag):
        raise ToolFailure(
            AdapterCode.DATA_POLICY_DENIED,
            f"This project's policy does not allow '{flag}' to be sent to a model",
            details={"tool": tool, "flag": flag, "project_id": project_id},
            # The caller's own fix is to ask for less; widening the policy is an administrator's decision,
            # so the answer must not imply that retrying the same request could ever work (§10).
            next_action=NextAction.narrow_request,
        )


def content_open(policy: McpPolicy, flag: str) -> bool:
    """The non-raising form, for a field that is simply left out rather than refused."""
    return bool(getattr(policy, flag)) and policy.enabled


def limits_view(settings: Settings) -> dict[str, Any]:
    """The bounds this deployment actually answers under, so a client can plan a page (§6.2, §11)."""
    return {
        "page_size_default": DEFAULT_PAGE_SIZE,
        "page_size_max": MAX_PAGE_SIZE,
        "free_text_chars": FREE_TEXT_LIMIT,
        "response_bytes": settings.mcp_max_response_bytes,
        "metadata_response_bytes": settings.mcp_max_metadata_response_bytes,
        "tool_timeout_seconds": settings.mcp_tool_timeout_seconds,
        "inflight_per_user": settings.mcp_max_inflight_per_user,
        "rate_per_minute": settings.mcp_rate_limit_per_minute,
        "idempotency_key_required_for_writes": True,
        "evidence_download": False,
        "prompts": False,
        "dynamic_resources": False,
    }


def console_links(settings: Settings, *, execution_id: str | None = None) -> dict[str, Any]:
    """Console entry points built only from configured URLs (§7.4).

    The request's Host is never used, and no token, ticket or variable value is ever part of a link.
    The console's hash routing reaches a run and its report directly; `#/cases` and `#/assist` are list
    entries only, so nothing here claims a per-case or per-task deep link that does not exist.

    `requires_project_selection` is stated rather than inferred: the console still has to be pointed at
    this tenant and project by hand, so an assistant must not assume the link opens on the right screen.
    """
    base = settings.mcp_console_url.rstrip("/") + "/"
    links: dict[str, Any] = {
        "cases": f"{base}#/cases",
        "assist": f"{base}#/assist",
        "requires_project_selection": True,
    }
    if execution_id:
        links["run"] = f"{base}#/runs/{execution_id}"
        links["report"] = f"{base}#/report/{execution_id}"
    return links


def human_entry(settings: Settings, *, task_id: str, execution_id: str) -> dict[str, Any]:
    """Where a person takes over a paused run - and the only thing about it a model gets (§7.3)."""
    return {
        "human_task_id": task_id,
        "console_url": console_links(settings, execution_id=execution_id)["run"],
    }


def human_task_view(
    settings: Settings,
    *,
    task_id: str,
    step_id: str,
    reason: str,
    deadline: datetime | None,
    execution_id: str,
) -> dict[str, Any]:
    """One open human task as §6.6 allows it to be seen: identifier, stable code, deadline, entry point.

    `reason` is the platform's own stable code (`captcha`, `login`, ...), not what the operator typed, and
    the task's `detail` and `pause_token` are not here at all - the first is the operator's prose about the
    page they took over, and the second is a control credential that could hand the run back to whoever
    read it (§7.3). A deadline is sent because it changes what a caller does next: a task about to expire
    is worth reporting now rather than on the next poll.
    """
    return human_entry(settings, task_id=task_id, execution_id=execution_id) | {
        "step_id": step_id,
        "reason": reason,
        "deadline": stamp(deadline),
    }


def _jitter(bounds: tuple[int, int]) -> int:
    # A polling suggestion, so an unseeded generator is the right tool: it carries no security meaning,
    # and a seeded one would make every client in a process wake up together, which is the lockstep §8.1
    # asks this range to avoid.
    return random.randrange(bounds[0], bounds[1] + 1)  # noqa: S311


def compile_poll(created_at: datetime | None, *, now: datetime | None = None) -> int:
    """How long to wait before asking about a compilation again (§8.1: first 1 s, then 2-5 s).

    "First" is measured from the artifact's own creation time rather than from a client-supplied attempt
    count: a client may ask twice within a second, and honouring that would restart the fast interval
    forever. Past the window the suggestion widens with jitter, which is the point of a range.
    """
    moment = coerce_utc(created_at)
    if moment is not None:
        age = (coerce_utc(now) or utcnow()) - moment
        if age.total_seconds() <= FIRST_COMPILE_WINDOW_SECONDS:
            return FIRST_COMPILE_POLL_MS
    return _jitter(LATER_COMPILE_POLL_MS)


def execution_poll(status: str | None) -> int:
    """The polling suggestion for a run that has not reached a terminal state (§8.1)."""
    return _jitter(WAIT_HUMAN_POLL_MS if status == ExecutionStatus.WAIT_HUMAN.value else EXECUTION_POLL_MS)


#: §8 - only `FINISHED` ends a run. `FINALIZING` may already know its outcome and still have evidence,
#: analysis or cleanup in flight, so calling it terminal would present a partial document as a closed one.
TERMINAL_EXECUTION_STATUSES = frozenset({ExecutionStatus.FINISHED.value})


def execution_terminal(status: str | None) -> bool:
    """Whether a run is over - which is also the whole of what makes its base report ready (§8).

    Asked separately from the poll suggestion because the two answer different questions: `terminal` is
    about this run, while the readiness fields tell a caller whether the *report* and the *analysis* are
    settled. A run can be finished with its analysis still pending, and §8 insists they are reported
    separately rather than one standing in for the other.
    """
    return status in TERMINAL_EXECUTION_STATUSES


def stamp(value: datetime | None) -> str | None:
    """A database timestamp as §6.6 asks for it: ISO 8601 carrying an offset, or genuinely absent.

    The business database stores UTC, so `coerce_utc` is what makes the offset explicit rather than
    leaving a naive string for each client to guess at - the design's "客户端按自身时区显示" only works if
    what went out said which timezone it was in.
    """
    moment = coerce_utc(value)
    return None if moment is None else moment.isoformat()
