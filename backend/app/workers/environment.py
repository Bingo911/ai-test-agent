"""Turn a persisted environment revision into the concrete inputs one run needs (§5.4, §7.3).

The environment owns the network and evidence policy; a run can narrow it but never widen it, and
secrets are only ever revealed for keys the IR actually references, at the bound version.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..config import Settings
from ..domain.errors import ApiError, ErrorCode
from ..executors.contracts import ResolvedValue, SessionConfig


@dataclass
class ExecutionPlan:
    session_config: SessionConfig
    values: dict[str, ResolvedValue] = field(default_factory=dict)
    variables: dict[str, Any] = field(default_factory=dict)
    origin: str = ""
    route_pattern: str = "*"
    evidence_mode: str = "NORMAL"
    known_secrets: tuple[str, ...] = ()
    browser_family: str = "chromium"
    navigation_timeout_ms: int = 30_000


def build_snapshot(
    *,
    case: Any,
    revision: Any,
    environment_revision: Any,
    project: Any,
    run_variables: dict[str, Any] | None,
    evidence_mode: str,
    run_ai: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The frozen inputs one execution runs with; nothing is re-read from live rows afterwards (§3.2).

    `run_ai` is present only when a caller named an AI intent, which today means the MCP adapter (§6.4).
    Its absence is the legacy REST case and keeps the behaviour those runs have always had; it is not a
    synonym for "no model", which is why the two are not collapsed into one default here.
    """
    config = dict(environment_revision.config or {})
    snapshot = {
        "case_id": case.id,
        "case_name": case.name,
        "revision_id": revision.id,
        "revision_no": getattr(revision, "version", None),
        "source_digest": getattr(revision, "source_digest", None),
        "environment_id": environment_revision.environment_id,
        "environment_revision_id": environment_revision.id,
        "environment_config": config,
        "secret_bindings": dict(environment_revision.secret_bindings or {}),
        "run_variables": dict(run_variables or {}),
        "evidence_mode": evidence_mode,
        "project_settings": dict(project.settings or {}) if project is not None else {},
    }
    if run_ai is not None:
        snapshot["run_ai"] = dict(run_ai)
    return snapshot


def ir_secret_keys(ir: dict[str, Any]) -> list[str]:
    """Every `secret` reference the IR makes, in first-seen order and de-duplicated."""
    found: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if node.get("kind") == "secret" and isinstance(node.get("key"), str) and node["key"] not in found:
                found.append(node["key"])
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(ir)
    return found


def ir_target_descriptions(ir: dict[str, Any]) -> dict[str, str]:
    """`strategy|selector` -> human description, so element memory keeps a readable label (§8.3)."""
    labels: dict[str, str] = {}

    def remember(target: dict[str, Any]) -> None:
        description = target.get("description")
        if not isinstance(description, str):
            return
        for candidate in target.get("candidates") or []:
            if isinstance(candidate, dict) and isinstance(candidate.get("strategy"), str):
                fallback = candidate.get("name") or candidate.get("text") or ""
                key = f"{candidate['strategy']}:{candidate.get('selector') or fallback}"
                labels.setdefault(key, description)

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if "description" in node and "candidates" in node:
                remember(node)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(ir)
    return labels


def build_plan(
    *,
    settings: Settings,
    config: dict[str, Any],
    secret_bindings: dict[str, Any],
    ir: dict[str, Any],
    requested_browser: str,
    run_variables: dict[str, Any] | None,
    evidence_mode: str | None,
    allow_vision: bool = False,
    reveal_secret=None,
) -> ExecutionPlan:
    """`reveal_secret(logical_name, version)` returns the plaintext; it is injected so this stays IO-free."""
    browsers = [str(item) for item in (config.get("browsers") or settings.browser_channel_list)]
    # Never substitute another browser for the one the run was created with: the recorded browser is part
    # of the verdict's meaning, and an environment that stopped allowing it is a conflict, not a hint.
    if requested_browser not in browsers:
        raise ApiError(
            ErrorCode.BROWSER_NOT_SUPPORTED,
            f"browser '{requested_browser}' is not allowed by this environment",
            details={"allowed": browsers},
        )
    browser = requested_browser

    allowed_domains = tuple(str(item) for item in (config.get("allowed_domains") or ()))
    viewport = config.get("viewport") or {}
    width = int(viewport.get("width") or settings.viewport_width)
    height = int(viewport.get("height") or settings.viewport_height)
    evidence = config.get("evidence") or {}
    mode = (evidence_mode or evidence.get("mode") or settings.screenshot_policy or "NORMAL").upper()
    sensitive = mode == "SENSITIVE"
    trace_policy = str(evidence.get("trace") or settings.trace_policy)
    video_policy = str(evidence.get("video") or settings.video_policy)

    session_config = SessionConfig(
        browser_type="chromium",
        browser_channel=browser,
        headless=settings.browser_headless,
        sandbox=settings.browser_sandbox,
        viewport=(width, height),
        locale=settings.browser_locale,
        timezone=settings.browser_timezone,
        ignore_https_errors=settings.ignore_https_errors,
        executable_path=settings.browser_executable_path,
        browsers_path=str(settings.playwright_browsers_path) if settings.playwright_browsers_path else None,
        record_video=(not sensitive and video_policy == "on"),
        start_trace=(not sensitive and trace_policy in ("on", "retain_on_failure")),
        allowed_origins=allowed_domains,
        navigation_timeout_ms=int(settings.navigation_timeout_ms),
        sensitive_evidence=sensitive,
        allow_vision=bool(allow_vision and settings.ai_vision_enabled),
    )

    env_values = config.get("variables") or {}
    values: dict[str, ResolvedValue] = {}
    for key, value in env_values.items():
        values[f"env:{key}"] = ResolvedValue(_raw=_scalar(value))
    base_url = str(config.get("base_url") or "")
    if base_url:
        values.setdefault("env:base_url", ResolvedValue(_raw=base_url))

    variables: dict[str, Any] = dict(env_values)
    variables.update(run_variables or {})
    for key, value in (run_variables or {}).items():
        values[f"vars:{key}"] = ResolvedValue(_raw=str(value))

    revealed: list[str] = []
    for name in ir_secret_keys(ir):
        version = secret_bindings.get(name)
        if reveal_secret is None:
            raise ApiError(ErrorCode.SECRET_UNAVAILABLE, f"secret '{name}' cannot be resolved here")
        try:
            plaintext = reveal_secret(name, int(version) if version is not None else None)
        except ApiError:
            raise
        except Exception as exc:
            raise ApiError(
                ErrorCode.SECRET_UNAVAILABLE, f"secret '{name}' is not available: {type(exc).__name__}"
            ) from exc
        values[f"secret:{name}"] = ResolvedValue(_raw=plaintext, secret=True)
        revealed.append(plaintext)

    origin = _origin_of(base_url)
    return ExecutionPlan(
        session_config=session_config,
        values=values,
        variables=variables,
        origin=origin,
        route_pattern=_route_pattern(ir),
        evidence_mode=mode,
        known_secrets=tuple(revealed),
        browser_family="chromium",
        navigation_timeout_ms=int(settings.navigation_timeout_ms),
    )


def _scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return ""
    return str(value)


def _origin_of(base_url: str) -> str:
    if "://" not in base_url:
        return base_url.rstrip("/")
    scheme, _, rest = base_url.partition("://")
    host, _, _ = rest.partition("/")
    return f"{scheme}://{host}".rstrip("/")


def _route_pattern(ir: dict[str, Any]) -> str:
    """The route the run starts on, used as the element-memory key scope (§8.3)."""
    for step in ir.get("steps") or []:
        if not isinstance(step, dict):
            continue
        url = step.get("url")
        if isinstance(url, str) and "://" in url:
            _, _, rest = url.partition("://")
            _, path, _ = rest.partition("/")
            return "/" + path.split("?")[0]
    return "*"
