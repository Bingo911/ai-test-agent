"""The AI decision a queued job makes when it runs, which is not the one its caller made (§6.4).

`allow_server_ai` is checked by the command that queues the work, but a job can sit in the queue while an
administrator tightens that policy, and the model call is the moment the text actually leaves the platform.
So the worker re-reads the policy and intersects it with the intent the job was created with: agreed then
and agreed now means the model is asked, anything else means the deterministic path.

Only a job that came in through MCP is re-read. The console's AI behaviour is not governed by
`mcp_policy` at all, so applying this check there would change REST under an MCP flag (§12, AC-40).
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from ..domain.mcp_policy import read_policy
from ..repositories.platform import AccessRepository

#: The entrypoint whose requests for server-side AI the project's own policy governs.
MCP_ORIGIN = "mcp"

#: Recorded wherever a stopped call has to leave a trace a person can read later.
STOPPED_BY_POLICY = "AI_POLICY_STOPPED"

#: The journal event a run worker writes when the intersection stops a call it was asked to make (§13.4).
AI_STOPPED_EVENT = "execution.ai_stopped"


def server_ai_allowed(session: Session, tenant_id: str, project_id: str) -> bool:
    """The project's answer right now, rather than the one the queue message remembered.

    A project this cannot read is a project that does not allow it: the missing row and an unreadable
    policy both fail closed, because the alternative is sending data out on the strength of a policy that
    no longer exists.
    """
    project = AccessRepository(session, tenant_id).project(tenant_id, project_id)
    if project is None:
        return False
    return read_policy(project.settings).policy.allow_server_ai


def intersection(origin: Any, asked: bool, allowed_now: bool) -> tuple[bool, str | None]:
    """(may the model still be asked, reason it may not) for one queued job.

    A job from another entrypoint keeps the decision its caller made; an MCP job that never asked for the
    model is not given one by a later policy, and one that did ask is stopped when the policy moved.
    """
    if str(origin or "") != MCP_ORIGIN:
        return asked, None
    if not asked:
        return False, None
    return (True, None) if allowed_now else (False, STOPPED_BY_POLICY)
