"""Executor behaviour against the local fixture site (§7, §8, §12.1).

The cases are compiled from real Markdown through the deterministic pipeline, so what runs here is
the same IR the platform would execute — not a hand-built approximation of it.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import pytest
from backend.app.compiler.pipeline import compile_revision
from backend.app.executors.contracts import ResolvedValue, RunContext, SessionConfig, VisionCandidate
from backend.app.executors.locator import LocatorBudget
from backend.app.executors.playwright.adapter import PlaywrightExecutor
from backend.app.executors.playwright.session import host_allowed
from backend.app.ir import models as ir_models

from .site_server import SiteServer

BROWSERS_PATH = Path(__file__).resolve().parents[1] / ".pw-browsers"


class RecordingSink:
    """In-memory stand-in for the artifact repository + object store."""

    def __init__(self) -> None:
        self.entries: list[dict[str, Any]] = []
        self.truncations: list[str] = []

    def put_bytes(self, *, kind, name, data, media_type, step_id=None, sensitive=False):
        self.entries.append({"kind": kind, "name": name, "size": len(data), "step_id": step_id, "sensitive": sensitive})
        return f"artifact-{len(self.entries)}"

    def put_file(self, *, kind, name, path, media_type, step_id=None, sensitive=False):
        self.entries.append(
            {"kind": kind, "name": name, "size": Path(path).stat().st_size if Path(path).exists() else 0}
        )
        return f"artifact-{len(self.entries)}"

    def record_console(self, entries, *, step_id=None):
        return None

    def record_network(self, entries, *, step_id=None):
        return None

    def mark_truncated(self, reason):
        self.truncations.append(reason)

    def kinds(self) -> set[str]:
        return {entry["kind"] for entry in self.entries}


def compile_ir(
    markdown: str,
    settings,
    *,
    attachments: dict[str, str] | None = None,
    allow_vision: bool = False,
    tolerate_review: bool = False,
) -> ir_models.TestIR:
    outcome = compile_revision(
        markdown,
        revision_id="rev-executor",
        settings=settings,
        attachments=attachments,
        allow_vision=allow_vision,
    )
    if tolerate_review:
        # A target with no written locator compiles, but only into a confirmation-gated artifact.
        assert outcome.ir is not None, outcome.diagnostics
    else:
        assert outcome.executable, outcome.diagnostics
    return ir_models.ir_from_payload(outcome.ir)


def make_config(base_url: str, **overrides: Any) -> SessionConfig:
    options: dict[str, Any] = {
        "browser_type": "chromium",
        "headless": True,
        "sandbox": False,
        "browsers_path": str(BROWSERS_PATH),
        "allowed_origins": ("127.0.0.1", "localhost"),
        "viewport": (1280, 720),
    }
    options.update(overrides)
    return SessionConfig(**options)


def run_case(
    markdown: str,
    base_url: str,
    *,
    settings,
    sink: RecordingSink | None = None,
    values: dict[str, ResolvedValue] | None = None,
    config: SessionConfig | None = None,
    record_trace: bool = False,
    attachments: dict[str, str] | None = None,
    attachment_payloads: dict[str, bytes] | None = None,
    attachment_paths: dict[str, str] | None = None,
    vision: Any = None,
    tolerate_review: bool = False,
) -> dict[str, Any]:
    allow_vision = vision is not None
    ir = compile_ir(
        markdown.replace("BASE_URL", base_url),
        settings,
        attachments=attachments,
        allow_vision=allow_vision,
        tolerate_review=tolerate_review,
    )
    session_config = config or make_config(base_url, start_trace=record_trace, allow_vision=allow_vision)

    async def main() -> dict[str, Any]:
        executor = PlaywrightExecutor(
            settings.model_copy(update={"ai_vision_enabled": True}) if allow_vision else settings
        )
        context = RunContext(
            execution_id="exec-1",
            tenant_id="t",
            project_id="p",
            environment_id="e",
            environment_revision_id="er",
            lease_epoch=1,
            values=values or {},
            evidence=sink or RecordingSink(),
            variables={"base_url": base_url},
            origin=base_url,
            route_pattern="*",
            settings=settings,
            vision=vision,
        )
        session = await executor.create_session(session_config, context)
        # the worker materialises scanned attachments into this execution's scratch directory (§14.3)
        for name, payload in (attachment_payloads or {}).items():
            target = session.scratch_dir / name
            target.write_bytes(payload)
            context.attachments[name] = str(target)
        context.attachments.update(attachment_paths or {})
        results: dict[str, Any] = {}
        try:
            for step in ir.steps:
                timeout = step.timeout_ms or ir.defaults.timeout_ms
                deadline = time.monotonic() * 1000 + timeout
                result = await executor.execute_step(session, step, context, deadline_ms=deadline)
                results[step.id] = result
            results["__bundle__"] = await executor.close_session(session, context)
        except Exception:
            await executor.close_session(session, context)
            raise
        return results

    return asyncio.run(main())


MARKDOWN_LOGIN = """---
dsl_version: "1.0"
tags: [smoke]
defaults:
  timeout_ms: 8000
---
# 登录测试

## Step 1
```yaml
action: open
url: BASE_URL/index.html
```

## Step 2
```yaml
action: input
target:
  description: 用户名输入框
  type: input
  css: 'input[name="username"]'
value: demo
```

## Step 3
```yaml
action: input
target:
  description: 密码输入框
  type: input
  css: 'input[name="password"]'
value: "${secrets.login_password}"
```

## Step 4
```yaml
action: click
target:
  description: 登录按钮
  type: button
  role: button
  name: 登录
```

## Step 5
```yaml
action: assert
condition:
  kind: page_contains
  expected: 欢迎回来
```

## Step 6
```yaml
action: screenshot
name: after-login
```
"""


def test_login_flow_passes_and_records_evidence(settings):
    with SiteServer() as site:
        sink = RecordingSink()
        results = run_case(
            MARKDOWN_LOGIN,
            site.base_url,
            settings=settings,
            sink=sink,
            values={"secret:login_password": ResolvedValue(_raw="s3cret-value", secret=True, label="login_password")},
        )
    assert [results[step].status for step in ("s1", "s2", "s3", "s4", "s5", "s6")] == ["PASSED"] * 6
    assert results["s4"].detail["locator_strategy"] == "role"
    assert results["s6"].detail["artifact_id"]
    assert "screenshot" in sink.kinds()
    bundle = results["__bundle__"]
    assert any("login ok" in entry["text"] for entry in bundle.console)
    assert bundle.console_truncated is False


def test_secret_value_never_reaches_the_report(settings):
    with SiteServer() as site:
        sink = RecordingSink()
        results = run_case(
            MARKDOWN_LOGIN,
            site.base_url,
            settings=settings,
            sink=sink,
            values={"secret:login_password": ResolvedValue(_raw="hunter2-otp-secret", secret=True)},
        )
    assert results["s3"].ok
    assert results["s3"].detail["secret"] is True
    assert results["s3"].detail["typed_length"] == len("hunter2-otp-secret")
    dumped = repr([results[step].detail for step in ("s1", "s2", "s3", "s4", "s5", "s6")])
    assert "hunter2-otp-secret" not in dumped


def test_ambiguous_target_fails_instead_of_clicking_first(settings):
    markdown = """---
dsl_version: "1.0"
---
# 歧义定位

## Step 1
```yaml
action: open
url: BASE_URL/index.html
```

## Step 2
```yaml
action: click
target:
  description: 重复按钮
  text: 重复
```
"""
    with SiteServer() as site:
        results = run_case(markdown, site.base_url, settings=settings)
    failure = results["s2"]
    assert failure.status == "FAILED"
    assert failure.failure_kind == "locator_ambiguous"
    ambiguous = [attempt for attempt in failure.detail.get("locator_attempts", []) if attempt["outcome"] == "ambiguous"]
    assert ambiguous
    assert ambiguous[0]["matched"] == 2


def test_hidden_and_visible_conditions(settings):
    markdown = """---
dsl_version: "1.0"
---
# 可见性断言

## Step 1
```yaml
action: open
url: BASE_URL/index.html
```

## Step 2
```yaml
action: assert
condition:
  kind: element_visible
  target:
    description: 登录按钮
    css: '#login-button'
```

## Step 3
```yaml
action: assert
condition:
  kind: element_hidden
  target:
    description: 错误提示
    css: '#error'
```

## Step 4
```yaml
action: click
target:
  description: 加载详情按钮
  role: button
  name: 加载详情
```

## Step 5
```yaml
action: assert
timeout_ms: 5000
condition:
  kind: page_contains
  expected: 详情已加载
```
"""
    with SiteServer() as site:
        results = run_case(markdown, site.base_url, settings=settings)
    assert [results[step].status for step in ("s1", "s2", "s3", "s4", "s5")] == ["PASSED"] * 5
    assert results["s5"].detail["polls"] >= 2


def test_value_equals_and_url_conditions(settings):
    markdown = """---
dsl_version: "1.0"
---
# 值断言

## Step 1
```yaml
action: open
url: BASE_URL/index.html
```

## Step 2
```yaml
action: input
target:
  description: 用户名输入框
  css: 'input[name="username"]'
value: demo
```

## Step 3
```yaml
action: assert
condition:
  kind: value_equals
  target:
    description: 用户名输入框
    css: 'input[name="username"]'
  expected: demo
```

## Step 4
```yaml
action: assert
condition:
  kind: url_contains
  expected: /index.html
```
"""
    with SiteServer() as site:
        results = run_case(markdown, site.base_url, settings=settings)
    assert [results[step].status for step in ("s1", "s2", "s3", "s4")] == ["PASSED"] * 4


def test_assertion_failure_keeps_the_original_error_and_adds_evidence(settings):
    markdown = """---
dsl_version: "1.0"
---
# 断言失败

## Step 1
```yaml
action: open
url: BASE_URL/index.html
```

## Step 2
```yaml
action: assert
timeout_ms: 1500
condition:
  kind: page_contains
  expected: 永远不会出现的文字
```
"""
    with SiteServer() as site:
        sink = RecordingSink()
        results = run_case(markdown, site.base_url, settings=settings, sink=sink)
    assert results["s2"].failure_kind == "assertion_failed"
    assert results["s2"].detail["polls"] >= 1
    kinds = sink.kinds()
    assert {"screenshot", "dom", "locator_diagnostic"} <= kinds or {"screenshot", "dom"} <= kinds


def test_navigation_outside_the_allowlist_is_blocked(settings):
    markdown = """---
dsl_version: "1.0"
---
# 越权导航

## Step 1
```yaml
action: open
url: https://example.com/
```
"""
    with SiteServer() as site:
        results = run_case(markdown, site.base_url, settings=settings)
    assert results["s1"].status == "FAILED"
    assert results["s1"].failure_kind in {"navigation_failed", "outcome_unknown"}


def test_network_evidence_records_the_status_api(settings):
    markdown = """---
dsl_version: "1.0"
---
# 接口状态

## Step 1
```yaml
action: open
url: BASE_URL/index.html
```

## Step 2
```yaml
action: click
target:
  description: 查询状态按钮
  role: button
  name: 查询状态
```

## Step 3
```yaml
action: assert
timeout_ms: 5000
condition:
  kind: page_contains
  expected: 服务正常
```
"""
    with SiteServer() as site:
        results = run_case(markdown, site.base_url, settings=settings)
    assert results["s3"].ok
    bundle = results["__bundle__"]
    assert any("/api/status" in entry["url"] for entry in bundle.network)


def test_upload_accepts_a_scratch_attachment(settings):
    markdown = """---
dsl_version: "1.0"
---
# 文件上传

## Step 1
```yaml
action: open
url: BASE_URL/index.html
```

## Step 2
```yaml
action: upload
target:
  description: 头像控件
  type: file
  css: 'input[name="avatar"]'
attachment_ids: ["att-1"]
```
"""
    with SiteServer() as site:
        results = run_case(
            markdown,
            site.base_url,
            settings=settings,
            attachments={"att-1": "CLEAN"},
            attachment_payloads={"att-1": b"not-a-real-png"},
        )
    assert results["s1"].ok
    assert results["s2"].ok, results["s2"].message
    assert results["s2"].detail["files"] == 1


def test_upload_rejects_a_path_outside_the_scratch_directory(settings, tmp_path):
    markdown = """---
dsl_version: "1.0"
---
# 目录穿越

## Step 1
```yaml
action: open
url: BASE_URL/index.html
```

## Step 2
```yaml
action: upload
target:
  description: 头像控件
  css: 'input[name="avatar"]'
attachment_ids: ["att-evil"]
```
"""
    outside = tmp_path / "secret.txt"
    outside.write_text("host file", encoding="utf-8")
    with SiteServer() as site:
        results = run_case(
            markdown,
            site.base_url,
            settings=settings,
            attachments={"att-evil": "CLEAN"},
            attachment_paths={"att-evil": str(outside)},
        )
    assert results["s2"].failure_kind == "unsupported_scope"
    assert "scratch" in (results["s2"].message or "")


def test_trace_and_video_are_captured_when_the_environment_allows_them(settings):
    with SiteServer() as site:
        sink = RecordingSink()
        results = run_case(
            MARKDOWN_LOGIN,
            site.base_url,
            settings=settings,
            sink=sink,
            values={"secret:login_password": ResolvedValue(_raw="pw", secret=True)},
            config=make_config(site.base_url, start_trace=True, record_video=True),
        )
    bundle = results["__bundle__"]
    assert bundle.errors == [], bundle.errors
    assert {"trace", "video"} <= sink.kinds(), sink.entries


class _FixedPointVision:
    """Stands in for the visual model: it points at the fixture page's fixed-position button (§8.2)."""

    def __init__(self) -> None:
        self.requests: list[Any] = []

    async def resolve(self, request: Any) -> VisionCandidate:
        self.requests.append(request)
        return VisionCandidate(
            x=110, y=50, role="button", accessible_name="固定目标", css_selector_hint=None, model="stub-vision"
        )


MARKDOWN_VISION = """---
dsl_version: "1.0"
defaults:
  timeout_ms: 20000
---
# 视觉兜底

## Step 1
```yaml
action: open
url: BASE_URL/vision.html
```

## Step 2
```yaml
action: click
target:
  description: 固定目标
  type: button
  allow_vision: true
```

## Step 3
```yaml
action: assert
condition:
  kind: page_contains
  expected: 视觉点击已记录
```
"""


def test_vision_fallback_completes_the_step_it_resolved(settings):
    """A located-by-coordinates target must act, not be reported as "no candidate found" (§8.1, §8.2)."""
    with SiteServer() as site:
        vision = _FixedPointVision()
        results = run_case(MARKDOWN_VISION, site.base_url, settings=settings, vision=vision, tolerate_review=True)
    assert [results[step].status for step in ("s1", "s2", "s3")] == ["PASSED"] * 3
    assert (results["s2"].locator.strategy, results["s2"].locator.source) == ("vision", "vision")
    assert results["s2"].locator.degraded is True
    assert len(vision.requests) == 1


def test_vision_resolves_only_after_the_deterministic_ladder_is_exhausted(settings):
    """A remembered or written locator wins the step, and the visual call never happens (§8.1)."""
    with SiteServer() as site:
        vision = _FixedPointVision()
        markdown = MARKDOWN_VISION.replace("  allow_vision: true\n", "  allow_vision: true\n  css: '#vision-target'\n")
        results = run_case(markdown, site.base_url, settings=settings, vision=vision)
    assert results["s2"].status == "PASSED"
    assert results["s2"].locator.strategy == "css"
    assert vision.requests == []


@pytest.mark.parametrize(
    ("host", "expected"),
    [("127.0.0.1", True), ("localhost", True), ("example.com", False), ("169.254.169.254", False)],
)
def test_allowlist_matching(host, expected):
    assert host_allowed(host, ("127.0.0.1", "localhost")) is expected


def test_locator_budget_never_extends_an_explicit_step_timeout():
    budget = LocatorBudget.for_step(step_timeout_ms=3000, vision_enabled=True)
    assert budget.step_ms == 3000
    assert budget.deterministic_ms + budget.vision_ms + budget.reserve_action_ms <= 3000
    wide = LocatorBudget.for_step(step_timeout_ms=60_000, vision_enabled=True)
    assert wide.step_ms == 20_000
    assert wide.vision_affordable
    plain = LocatorBudget.for_step(step_timeout_ms=10_000, vision_enabled=False)
    assert plain.vision_ms == 0
    assert plain.deterministic_ms == 10_000
