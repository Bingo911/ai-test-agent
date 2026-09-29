#!/usr/bin/env python3
"""Drive a full run through the live HTTP API: author, compile, confirm, execute, report.

This is the acceptance path a client takes (§13.3), so it exercises the middleware, RBAC, the
outbox/scheduler loops, a real browser and the evidence routes rather than any in-process shortcut.

    AITA_API_URL=http://127.0.0.1:8137 ./scripts/live_check.py [markdown file]

Exits non-zero on any step that does not reach its expected state.
"""

from __future__ import annotations

import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import httpx

API_URL = os.environ.get("AITA_API_URL", "http://127.0.0.1:8137").rstrip("/")
TOKEN = os.environ.get("AITA_DEV_TOKEN", "dev-admin-token")
BASE = f"{API_URL}/api/v1"
POLL_SECONDS = float(os.environ.get("AITA_POLL_INTERVAL", "1.0"))
SETTLED_COMPILES = {"SUCCEEDED", "NEEDS_REVIEW", "FAILED"}

CASE_MARKDOWN = """---
dsl_version: "1.0"
tags: [live, smoke]
defaults:
  timeout_ms: 15000
---
# 百度搜索首页

## Step 1
```yaml
action: open
url: "${env.base_url}/"
```

## Step 2
```yaml
action: input
target:
  description: 首页搜索输入框
  type: textarea
  css: "#chat-textarea"
value: 人工智能
```

## Step 3
```yaml
action: click
target:
  description: 百度一下按钮
  type: button
  css: "#chat-submit-button"
  role: button
  name: 百度一下
```

## Step 4
```yaml
action: wait
condition:
  kind: url_contains
  expected: /s?
```

## Step 5
```yaml
action: assert
condition:
  kind: page_contains
  expected: 百度
```

## Step 6
```yaml
action: screenshot
name: live-result
```
"""


class Failure(Exception):
    pass


def call(
    client: httpx.Client,
    method: str,
    path: str,
    *,
    json_body: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    expect: tuple[int, ...] = (200, 201, 202, 204),
) -> Any:
    response = client.request(method, f"{BASE}{path}", json=json_body, headers=headers or {})
    label = f"{method} {path}"
    if response.status_code not in expect:
        raise Failure(f"{label} -> {response.status_code}: {response.text[:400]}")
    if response.status_code == 204 or not response.content:
        return None
    return response.json()


def step(message: str) -> None:
    print(f"\n== {message}", flush=True)


def show(value: Any, limit: int = 300) -> str:
    return json.dumps(value, ensure_ascii=False)[:limit]


def wait_for_compile(client: httpx.Client, artifact_id: str, *, timeout: float = 90.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        artifact = call(client, "GET", f"/compilations/{artifact_id}")
        if artifact["status"] in SETTLED_COMPILES:
            return artifact
        time.sleep(POLL_SECONDS)
    raise Failure(f"compilation {artifact_id} never settled")


def wait_for_execution(client: httpx.Client, execution_id: str, *, timeout: float = 420.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    previous = None
    while time.monotonic() < deadline:
        view = call(client, "GET", f"/executions/{execution_id}")
        marker = (view.get("status"), view.get("outcome"), (view.get("step_summary") or {}).get("by_status"))
        if marker != previous:
            print(f"   {show(marker)} events={view.get('last_event_seq')}", flush=True)
            previous = marker
        if view.get("status") == "FINISHED":
            return view
        time.sleep(POLL_SECONDS)
    raise Failure(f"execution {execution_id} did not finish in {timeout}s")


def main() -> int:
    markdown = Path(sys.argv[1]).read_text(encoding="utf-8") if len(sys.argv) > 1 else CASE_MARKDOWN

    with httpx.Client(
        headers={"Authorization": f"Bearer {TOKEN}", "X-Request-ID": f"live-{uuid.uuid4().hex[:12]}"},
        timeout=httpx.Timeout(60.0, read=600.0),
    ) as client:
        step("identity, project and environment")
        whoami = call(client, "GET", "/whoami")
        projects = call(client, "GET", "/projects")
        project_id = projects["items"][0]["id"]
        print(f"   actor={whoami['display_name']} admin={whoami['is_admin']} project={project_id}")
        envs = call(client, "GET", f"/projects/{project_id}/environments")
        environment = envs["items"][0]
        env_revision = environment["current_revision"]
        print(
            f"   env={environment['name']} revision={env_revision['version']} "
            f"base_url={env_revision['config']['base_url']}"
        )
        caps = call(client, "GET", "/capabilities")
        print(
            f"   actions={','.join(caps['actions'])} browsers={','.join(caps['browsers'])} "
            f"ai={caps['features']['ai_compilation']}"
        )

        step("author the case")
        suffix = uuid.uuid4().hex[:8]
        created = call(
            client,
            "POST",
            f"/projects/{project_id}/cases",
            json_body={
                "name": f"live-baidu-{suffix}",
                "markdown": markdown,
                "title": "Live search from the homepage",
                "tags": ["live"],
            },
            headers={"Idempotency-Key": f"live-{suffix}"},
        )
        case_id, revision_id = created["case_id"], created["revision_id"]
        case = call(client, "GET", f"/cases/{case_id}")
        print(f"   case={case_id} revision={revision_id} digest={case['current_revision']['source_digest'][:19]}")
        print(f"   compile after save: {show(case['compile']) if case['compile'] else 'not produced yet'}")

        step("compile with the deterministic compiler")
        queued = call(client, "POST", f"/case-revisions/{revision_id}/compile", json_body={"use_ai": False})
        artifact = wait_for_compile(client, queued["compile_artifact_id"])
        ir = artifact.get("ir") or {}
        print(
            f"   artifact={artifact['compile_artifact_id']} status={artifact['status']} digest={artifact['ir_digest']}"
        )
        print(
            f"   ir steps={[s['action'] for s in ir.get('steps', [])]} "
            f"diagnostics={len(artifact['diagnostics'])} review={len(artifact['review_items'])}"
        )
        if artifact["diagnostics"]:
            print(f"   {show(artifact['diagnostics'], 500)}")
        if artifact["status"] == "FAILED":
            raise Failure(f"compilation failed: {show(artifact['diagnostics'], 500)}")
        if artifact["status"] == "NEEDS_REVIEW":
            print(f"   review={show(artifact['review_items'], 400)}")
            confirmed = call(
                client,
                "POST",
                f"/compilations/{artifact['compile_artifact_id']}/confirm",
                json_body={"ir_digest": artifact["ir_digest"]},
            )
            print(f"   confirmed={confirmed['status']} executable={confirmed['executable']}")
            executable_id = artifact["compile_artifact_id"]
        else:
            executable_id = artifact["compile_artifact_id"]

        step("execute on chromium")
        execution = call(
            client,
            "POST",
            "/executions",
            json_body={
                "compile_artifact_id": executable_id,
                "environment_revision_id": env_revision["environment_revision_id"],
                "browser": "chromium",
                "evidence_mode": "NORMAL",
            },
            headers={"Idempotency-Key": f"live-exec-{suffix}"},
        )
        execution_id = execution["id"]
        print(f"   execution={execution_id} status={execution['status']}")

        step("follow the SSE journal")
        ticket = call(client, "POST", f"/executions/{execution_id}/events/ticket", json_body={})
        frames: list[str] = []
        with client.stream(
            "GET",
            f"{BASE}/executions/{execution_id}/events?ticket={ticket['ticket']}",
            headers={"Accept": "text/event-stream"},
        ) as stream:
            if stream.status_code != 200:
                raise Failure(f"SSE connect -> {stream.status_code}: {stream.read()[:300]}")
            event = ""
            for line in stream.iter_lines():
                if line.startswith("event:"):
                    event = line.split(":", 1)[1].strip()
                elif line.startswith("data:"):
                    body = json.loads(line.split(":", 1)[1].strip())
                    frames.append(f"{event or 'message'}:{body.get('seq')}")
                    if body.get("status") == "FINISHED" or event in {
                        "stream.end",
                        "run_finished",
                        "execution_finished",
                    }:
                        break
        print(f"   {len(frames)} frames, first={show(frames[:4], 160)}")

        view = wait_for_execution(client, execution_id)
        print(f"   final status={view['status']} outcome={view['outcome']} steps={show(view['step_summary'], 200)}")
        for row in view["steps"]:
            print(
                f"   - {row['step_id']} {row['action']:<10} {row['status']:<8} "
                f"{row['duration_ms']}ms {row['error_code'] or ''}"
            )

        step("report")
        report = call(client, "GET", f"/executions/{execution_id}/report")
        run = report["execution"]
        degraded = [row["step_id"] for row in report["steps"] if len(row.get("locator_attempts") or []) > 1]
        print(
            f"   outcome={run['outcome']} error={run.get('error_code')} "
            f"phase={report['report_phase']} degradation={degraded}"
        )
        print(
            f"   evidence={show(report['evidence'].get('by_kind'), 200)} "
            f"status={report['evidence'].get('status')} missing={len(report['evidence'].get('missing', []))}"
        )
        if report.get("warnings"):
            print(f"   warnings={show(report['warnings'], 300)}")

        step("evidence download through a ticket")
        detail = call(client, "GET", f"/executions/{execution_id}/steps")
        refs = [item for row in detail["items"] for item in row["artifacts"]]
        print(f"   {len(detail['items'])} steps, {len(refs)} artifact refs")
        for row in detail["items"]:
            if row["locator_attempts"]:
                print(f"   locator {row['step_id']}: {show(row['locator_attempts'], 220)}")
        downloaded = 0
        probed: tuple[str, str] | None = None
        for ref in refs:
            if ref["kind"] not in {"SCREENSHOT", "DOM", "CONSOLE", "NETWORK"} or not ref["available"]:
                continue
            artifact_id = ref["ref"].split(":", 1)[1]
            issued = call(client, "POST", f"/artifacts/{artifact_id}/download-ticket", json_body={"usage": "debug"})
            raw = client.get(
                f"{BASE}/artifacts/{artifact_id}/download", params={"ticket": issued["ticket"]}, timeout=60
            )
            if raw.status_code != 200:
                raise Failure(f"download {artifact_id} -> {raw.status_code}: {raw.text[:200]}")
            print(
                f"   {ref['kind']} {len(raw.content)} bytes ct={raw.headers.get('content-type')} "
                f"csp={raw.headers.get('content-security-policy', '')[:40]}"
            )
            downloaded += 1
            probed = (artifact_id, issued["ticket"])
            break
        if probed:
            replayed = client.get(f"{BASE}/artifacts/{probed[0]}/download", params={"ticket": probed[1]})
            print(f"   same ticket replayed -> {replayed.status_code} {replayed.json()['error']['code']}")

        step("failure analysis")
        analysis = call(client, "POST", f"/executions/{execution_id}/analysis", json_body={}, expect=(200, 202, 409))
        print(f"   {show(analysis, 300)}")

        step("audit trail")
        audits = call(client, "GET", f"/projects/{project_id}/audit-logs?limit=6")
        for row in audits["items"]:
            print(f"   {row.get('operation')} on {row.get('resource_type')}")

        metrics = call(client, "GET", f"/projects/{project_id}/metrics")
        print(f"\n== metrics\n   {show(metrics, 400)}")

        ok = view["outcome"] == "PASSED" and downloaded == 1
        print(f"\nRESULT: outcome={view['outcome']} evidence_downloaded={downloaded} sse_frames={len(frames)}")
        return 0 if ok else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Failure as exc:
        print(f"\nFAILED: {exc}", file=sys.stderr)
        raise SystemExit(2) from None
