"""The per-project MCP data policy: one typed read, one strict write contract (§5.5, AC-33).

MCP returns case content and report detail to a client that may forward it to a model the platform has
never heard of, so the four flags below are what an administrator agreed to. They are read here and
nowhere else, because the two directions are different decisions: `allow_case_content` says nothing
about whether the platform's own internal AI may send data out, and neither may be inferred from the
other.

Two properties are load-bearing:

* a missing or unusable policy is **all four false**, never partially applied - an operator cannot
  approve MCP by leaving the settings broken;
* only the `mcp_policy` sub-object is validated. Sibling keys such as `allow_vision` belong to the
  existing project settings and must survive a policy write untouched.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError

from ..observability import get_logger
from .errors import ApiError, ErrorCode

log = get_logger(__name__)

#: Where the policy lives inside `project.settings`.
POLICY_KEY = "mcp_policy"

#: The fields, in the order the contract documents them.
POLICY_FIELDS = ("enabled", "allow_case_content", "allow_report_details", "allow_server_ai")


class McpPolicy(BaseModel):
    """The effective policy. Strict: a string "true" is not a boolean, and an unknown key is not a typo."""

    model_config = ConfigDict(extra="forbid", strict=True)

    enabled: bool = False
    allow_case_content: bool = False
    allow_report_details: bool = False
    allow_server_ai: bool = False


class McpPolicyPatch(BaseModel):
    """A partial update: absent fields keep their current value, which is the point of the new endpoint."""

    model_config = ConfigDict(extra="forbid", strict=True)

    enabled: bool | None = None
    allow_case_content: bool | None = None
    allow_report_details: bool | None = None
    allow_server_ai: bool | None = None

    def provided(self) -> dict[str, bool]:
        return {name: value for name, value in self.model_dump().items() if value is not None}


@dataclass(frozen=True)
class PolicyView:
    """What the stored policy means, plus whether it could be read at all."""

    policy: McpPolicy
    #: True when something is stored under `mcp_policy` and this build cannot make sense of it.
    invalid: bool

    @property
    def effective(self) -> dict[str, bool]:
        return self.policy.model_dump()


def read_policy(settings: Any) -> PolicyView:
    """The one fail-closed read.

    An unparseable stored policy is reported as fully disabled and logged under a stable code: the
    administrator has to be able to tell "no policy yet" from "policy I cannot read", and a caller must
    never be handed a half-applied one.
    """
    raw = settings.get(POLICY_KEY) if isinstance(settings, dict) else None
    if raw is None:
        return PolicyView(McpPolicy(), invalid=False)
    try:
        return PolicyView(McpPolicy.model_validate(raw), invalid=False)
    except ValidationError:
        log.warning("mcp_policy_invalid", extra={"context": {"reason": "stored policy does not match the model"}})
        return PolicyView(McpPolicy(), invalid=True)


def merge_policy(current: McpPolicy, patch: dict[str, bool]) -> McpPolicy:
    """Apply a partial patch to the effective policy; keys left out keep their stored value."""
    return current.model_copy(update=patch)


def validate_settings_for_write(settings: dict[str, Any]) -> dict[str, Any]:
    """Check the policy a whole-settings replacement is carrying, and return the value it should hold.

    Omitting `mcp_policy` entirely is legal and means the default-closed policy, which is the existing
    replacement contract; carrying an unusable one is a 400, because accepting it would write a policy
    the next read has to fail closed over.
    """
    raw = settings.get(POLICY_KEY) if isinstance(settings, dict) else None
    if raw is None:
        return McpPolicy().model_dump()
    try:
        return McpPolicy.model_validate(raw).model_dump()
    except ValidationError as exc:
        raise ApiError(
            ErrorCode.VALIDATION_ERROR,
            "project.settings.mcp_policy must be an object of the four boolean policy fields",
            details={"invalid_fields": sorted({str(error["loc"][0]) for error in exc.errors() if error["loc"]})},
        ) from exc


def apply_policy_to_settings(settings: Any, policy: McpPolicy) -> dict[str, Any]:
    """Replace only the `mcp_policy` key of a settings document, leaving every sibling alone (§5.5)."""
    merged = dict(settings) if isinstance(settings, dict) else {}
    merged[POLICY_KEY] = policy.model_dump()
    return merged


def require_complete_repair(view: PolicyView, patch: dict[str, bool]) -> None:
    """A stored policy this build cannot read can only be replaced whole, never patched (§5.5).

    Merging a one-field patch into a fail-closed default would silently turn three flags off while the
    administrator believes they only touched one.
    """
    if view.invalid and set(patch) != set(POLICY_FIELDS):
        raise ApiError(
            ErrorCode.VALIDATION_ERROR,
            "The stored MCP policy is unreadable, so this request must set all four policy fields",
            details={"required_fields": list(POLICY_FIELDS)},
        )
