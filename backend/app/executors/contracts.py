"""Executor and locator interfaces (§7.1).

The executor deliberately knows nothing about the database: every durable effect (artifact rows,
events, element-memory writes) goes through the injected sinks, which the worker implements with a
tenant-scoped session. That keeps a Celery prefork child free of shared ORM objects and lets the
same executor run against a stub sink in tests.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from ..ir.models import Step, Target, ValueSpec

#: `${{env.KEY}}` / `${{vars.KEY}}` — the only interpolation IR template values may contain.
_TEMPLATE_PLACEHOLDER = re.compile(r"\$\{(env|vars)\.([A-Za-z_][A-Za-z0-9_.-]*)\}")

FailureKind = Literal[
    "locator_not_found",
    "locator_ambiguous",
    "not_interactable",
    "assertion_failed",
    "navigation_failed",
    "timeout",
    "cancelled",
    "lease_lost",
    "browser_error",
    "outcome_unknown",
    "evidence_error",
    "unsupported_scope",
    "human_required",
]


@dataclass(frozen=True)
class CapabilitySet:
    """What this executor build can actually do; the compiler refuses to emit IR beyond it (§6.1)."""

    actions: tuple[str, ...]
    conditions: tuple[str, ...]
    locator_strategies: tuple[str, ...]
    supports_upload: bool = True
    supports_vision: bool = False
    supports_trace: bool = True
    supports_video: bool = True
    single_page_only: bool = True
    iframe_targets: bool = False

    def supports(self, action: str) -> bool:
        return action in self.actions


@dataclass(frozen=True)
class SessionConfig:
    """Browser identity frozen at execution-creation time (§7.2)."""

    browser_type: str = "chromium"
    browser_channel: str | None = None
    headless: bool = True
    sandbox: bool = True
    viewport: tuple[int, int] = (1280, 720)
    locale: str = "zh-CN"
    timezone: str = "Asia/Shanghai"
    device_scale_factor: float = 1.0
    ignore_https_errors: bool = False
    executable_path: str | None = None
    browsers_path: str | None = None
    user_agent: str | None = None
    record_video: bool = False
    start_trace: bool = False
    allowed_origins: tuple[str, ...] = ()
    navigation_timeout_ms: int = 30_000
    sensitive_evidence: bool = False
    #: Project-level permission for the §8.2 visual fallback; a target must also opt in.
    allow_vision: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "browser_type": self.browser_type,
            "browser_channel": self.browser_channel,
            "headless": self.headless,
            "viewport": list(self.viewport),
            "locale": self.locale,
            "timezone": self.timezone,
            "ignore_https_errors": self.ignore_https_errors,
            "record_video": self.record_video,
            "start_trace": self.start_trace,
            "allowed_origins": list(self.allowed_origins),
        }


@dataclass(frozen=True)
class ResolvedValue:
    """A resolved `ValueSpec` that cannot leak a secret through `repr` or a log line (§14.3)."""

    _raw: str
    secret: bool = False
    label: str = "value"

    @property
    def value(self) -> str:
        return self._raw

    def __repr__(self) -> str:  # pragma: no cover - defensive against accidental logging
        return f"ResolvedValue(secret={self.secret}, length={len(self._raw)})"

    def summary(self) -> dict[str, Any]:
        if self.secret:
            return {"secret": True, "length": len(self._raw)}
        return {"secret": False, "text": self._raw[:200]}


@dataclass
class Interruption:
    """Cooperative signals the worker pushes into a running step (§7.3, §9.5)."""

    cancel: bool = False
    lease_lost: bool = False
    human_requested: bool = False
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def stopped(self) -> bool:
        return self.cancel or self.lease_lost


class EvidenceSink(Protocol):
    """Durable evidence writes. Every method returns an artifact id, or None when capture failed."""

    def put_bytes(
        self,
        *,
        kind: str,
        name: str,
        data: bytes,
        media_type: str,
        step_id: str | None = None,
        sensitive: bool = False,
    ) -> str | None: ...

    def put_file(
        self,
        *,
        kind: str,
        name: str,
        path: Any,
        media_type: str,
        step_id: str | None = None,
        sensitive: bool = False,
    ) -> str | None: ...

    def record_console(self, entries: Sequence[dict[str, Any]], *, step_id: str | None = None) -> str | None: ...

    def record_network(self, entries: Sequence[dict[str, Any]], *, step_id: str | None = None) -> str | None: ...

    def mark_truncated(self, reason: str) -> None: ...


class MemoryCandidateSource(Protocol):
    """Element-memory reads for the locator engine (§8.3).

    Memory is keyed by target fingerprint, not by action: a healed selector belongs to the element, and
    the engine still re-validates it against the action it is about to perform.
    """

    def candidates(self, *, origin: str, route_pattern: str, target_fingerprint: str) -> list[tuple[str, str]]:
        """Return approved `(strategy, selector)` pairs, best-evidenced first."""
        ...

    def note_success(
        self,
        *,
        origin: str,
        route_pattern: str,
        target_fingerprint: str,
        strategy: str,
        selector: str,
    ) -> None: ...

    def note_failure(
        self,
        *,
        origin: str,
        route_pattern: str,
        target_fingerprint: str,
        strategy: str,
        selector: str,
    ) -> None: ...


class VisionResolver(Protocol):
    """AI visual fallback (§8.2). Implemented in `app/executors/vision.py`."""

    async def resolve(self, request: VisionRequest) -> VisionCandidate | None: ...


@dataclass(frozen=True)
class VisionRequest:
    target: Target
    action: str
    screenshot: bytes
    viewport: tuple[int, int]
    page_url: str
    page_version_digest: str
    deadline_ms: int


@dataclass(frozen=True)
class VisionCandidate:
    x: int
    y: int
    role: str | None
    accessible_name: str | None
    css_selector_hint: str | None
    model: str


@dataclass
class LocatorAttempt:
    """One candidate evaluation, kept for the report and for element-memory feedback (§8.1)."""

    strategy: str
    selector: str | None = None
    role: str | None = None
    name: str | None = None
    text: str | None = None
    source: Literal["explicit", "memory", "vision"] = "explicit"
    matched: int = 0
    outcome: Literal["resolved", "no_match", "ambiguous", "not_visible", "wrong_type", "rejected", "skipped"] = (
        "no_match"
    )
    reason: str | None = None
    elapsed_ms: int = 0

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "strategy": self.strategy,
            "source": self.source,
            "matched": self.matched,
            "outcome": self.outcome,
            "elapsed_ms": self.elapsed_ms,
        }
        for key, value in (
            ("selector", self.selector),
            ("role", self.role),
            ("name", self.name),
            ("text", self.text),
            ("reason", self.reason),
        ):
            if value is not None:
                payload[key] = value
        return payload


@dataclass
class LocatorResolution:
    """A validated, unique target plus the audit trail that produced it (§8.1)."""

    strategy: str
    source: str
    signature: str
    attempts: list[LocatorAttempt] = field(default_factory=list)
    description: str | None = None
    box: tuple[float, float, float, float] | None = None
    matched_text: str | None = None
    degraded: bool = False
    vision_skipped_reason: str | None = None
    #: The live Playwright locator for the verified element. Handlers act on this and never on a
    #: raw coordinate pair, so a DOM change between resolution and action fails instead of clicking
    #: whatever moved into the box.
    locator: Any = None

    def attempt_payload(self) -> list[dict[str, Any]]:
        return [attempt.as_dict() for attempt in self.attempts]


@dataclass
class StepResult:
    """Outcome of one IR step. `failure_kind` maps onto a §7/§8 error code by the caller."""

    step_id: str
    action: str
    status: Literal["PASSED", "FAILED", "ERROR", "CANCELLED", "SKIPPED", "WAIT_HUMAN"]
    failure_kind: FailureKind | None = None
    message: str | None = None
    locator: LocatorResolution | None = None
    artifact_ids: list[str] = field(default_factory=list)
    detail: dict[str, Any] = field(default_factory=dict)
    duration_ms: int = 0

    @property
    def ok(self) -> bool:
        return self.status == "PASSED"


@dataclass
class EvidenceBundle:
    """Session-scoped evidence collected while the execution ran (§12.1)."""

    console: list[dict[str, Any]] = field(default_factory=list)
    network: list[dict[str, Any]] = field(default_factory=list)
    dialogs: list[dict[str, Any]] = field(default_factory=list)
    console_truncated: bool = False
    network_truncated: bool = False
    errors: list[str] = field(default_factory=list)
    artifact_ids: list[str] = field(default_factory=list)

    @property
    def partial(self) -> bool:
        return bool(self.errors) or self.console_truncated or self.network_truncated


@dataclass
class RunContext:
    """Everything a step handler may need, injected rather than looked up (§7.1).

    Handlers must not reach for other tenants' resources: the context carries only the identity,
    variables and sinks belonging to this execution.
    """

    execution_id: str
    tenant_id: str
    project_id: str
    environment_id: str
    environment_revision_id: str
    lease_epoch: int
    values: dict[str, ResolvedValue] = field(default_factory=dict)
    attachments: dict[str, str] = field(default_factory=dict)
    evidence: EvidenceSink | None = None
    memory: MemoryCandidateSource | None = None
    vision: VisionResolver | None = None
    interruption: Interruption = field(default_factory=Interruption)
    variables: dict[str, Any] = field(default_factory=dict)
    origin: str | None = None
    route_pattern: str | None = None
    page_version_digest: str | None = None
    evidence_mode: str = "standard"
    settings: Any = None
    #: Called as `(step_id, INTENT_RECORDED|ACKNOWLEDGED)` around a side-effecting browser call.
    dispatch_hook: Any = None
    #: Called before the first locator attempt of a step that must pause for a human (§7.3).
    human_hook: Any = None
    #: Cumulative human pauses are excluded from action/active budgets, never from the hard limit.
    human_waited_ms: int = 0
    hard_deadline_ms: float | None = None

    def resolve(self, spec: ValueSpec) -> ResolvedValue:
        return resolve_value(spec, self.values, self.variables)


class ExecutorAdapter(Protocol):
    """§7.1 module interface."""

    def capabilities(self) -> CapabilitySet: ...

    async def create_session(self, config: SessionConfig, context: RunContext) -> Any: ...

    async def execute_step(
        self, session: Any, step: Step, context: RunContext, *, deadline_ms: float
    ) -> StepResult: ...

    async def collect_evidence(self, session: Any, context: RunContext) -> EvidenceBundle: ...

    async def close_session(self, session: Any, context: RunContext) -> EvidenceBundle: ...


_SECRET_MARKERS = ("secret", "password", "token", "otp", "pin", "authorization", "cookie", "credential")


def resolve_value(spec: ValueSpec, values: dict[str, ResolvedValue], variables: dict[str, Any]) -> ResolvedValue:
    """Map an IR `ValueSpec` onto a concrete value (§7.3).

    Secrets and env variables were resolved eagerly into `values` before the run; `vars` namespace
    entries come from the run's own variable overrides.
    """
    kind = spec.kind
    if kind == "literal":
        return ResolvedValue(_raw=str(spec.value))
    if kind == "secret":
        key = f"secret:{spec.key}"
        resolved = values.get(key)
        if resolved is None:
            raise MissingValue(key)
        return resolved
    if kind == "variable":
        namespaced = f"{spec.namespace}:{spec.key}"
        resolved = values.get(namespaced)
        if resolved is not None:
            return resolved
        if spec.namespace == "vars" and spec.key in variables:
            return ResolvedValue(_raw=str(variables[spec.key]))
        raise MissingValue(namespaced)
    if kind == "template":
        return ResolvedValue(_raw=_expand_template(spec.template, values, variables))
    raise MissingValue(str(kind))


def _expand_template(template: str, values: dict[str, ResolvedValue], variables: dict[str, Any]) -> str:
    """Substitute `${{env.*}}` / `${{vars.*}}` placeholders (§5.2).

    The compiler guaranteed a template holds nothing *but* placeholders and literal text, and never
    a secret reference. An unresolved placeholder is an error rather than an empty string, so a typo
    cannot silently submit a blank form field.
    """
    unresolved: list[str] = []

    def replace(match: re.Match[str]) -> str:
        namespace, key = match.groups()
        resolved = values.get(f"{namespace}:{key}")
        if resolved is not None:
            return resolved.value
        if namespace == "vars" and key in variables:
            return str(variables[key])
        unresolved.append(f"{namespace}.{key}")
        return match.group(0)

    expanded = _TEMPLATE_PLACEHOLDER.sub(replace, template)
    if unresolved:
        raise MissingValue(",".join(sorted(set(unresolved))))
    return expanded


class MissingValue(Exception):
    """A variable/secret the IR referenced is not available for this execution."""

    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__(f"value '{name}' is not available")

    @property
    def looks_secret(self) -> bool:
        return any(marker in self.name.lower() for marker in _SECRET_MARKERS)


class RunTermination(Exception):
    """A worker hook decided the run's conclusion; executors must let the worker handle it."""


class HumanRequired(Exception):
    """A step's `human_policy` needs a person before the browser may act (§10.1)."""

    def __init__(self, mode: str, reason: str | None, resume_condition: Any = None) -> None:
        self.mode = mode
        self.reason = reason
        self.resume_condition = resume_condition
        super().__init__(reason or f"human {mode} required")


class StepExecutionError(Exception):
    """A handler's typed failure; the adapter maps `kind` onto the §7/§8 error codes."""

    def __init__(self, kind: FailureKind, message: str, *, detail: dict[str, Any] | None = None) -> None:
        self.kind = kind
        self.message = message
        self.detail = detail or {}
        super().__init__(message)


class StepInterrupted(Exception):
    """Raised when the cancel/lease signal lands mid-step (§7.3 ordering, first check)."""

    def __init__(self, kind: FailureKind, message: str) -> None:
        self.kind = kind
        self.message = message
        super().__init__(message)
