"""The public error contract (§13.1) and the compile policy defaults (§6.2).

These are wire-level promises a client builds against, so they are pinned here rather than left to
the drift of whatever a service happened to raise last.
"""

from __future__ import annotations

import inspect
from typing import Any

import pytest
from backend.app.api.cases import CompileRequest
from backend.app.domain.errors import ApiError, ErrorCode
from backend.app.main import create_app
from fastapi.testclient import TestClient

MARKDOWN = """---
dsl_version: "1.0"
---
# 登录冒烟测试

## Step 1
```yaml
action: open
url: "${env.base_url}/index.html"
```
"""


@pytest.fixture
def client(database, session, workspace, settings) -> TestClient:
    # The seeded workspace is only visible to a request's own session once the fixture's has committed.
    session.commit()
    return TestClient(create_app(settings))


@pytest.fixture
def auth(settings) -> dict[str, str]:
    return {"Authorization": f"Bearer {settings.dev_engineer_token}"}


def _save(client: TestClient, workspace: dict[str, str], auth: dict[str, str], name: str, **body: Any) -> None:
    response = client.post(
        f"/api/v1/projects/{workspace['project_id']}/cases",
        json={"name": name, "markdown": MARKDOWN, "dsl_version": "1.0", **body},
        headers=auth,
    )
    assert response.status_code == 201, response.text


def test_no_tag_filter_lists_the_untagged_cases_too(client, workspace, auth) -> None:
    """An absent `tag` query means "do not filter", not "every tag in the project" (§13.2).

    The development workspace seeds three tags, so joining on all of them hid every untagged case from
    the console while `total` kept counting them — a list page that said there was one case and showed
    none.
    """
    project = workspace["project_id"]
    _save(client, workspace, auth, "untagged-case")
    _save(client, workspace, auth, "tagged-case", tags=["smoke"])
    _save(client, workspace, auth, "both-tags-case", tags=["smoke", "e2e"])

    def names(query: str = "") -> list[str]:
        response = client.get(f"/api/v1/projects/{project}/cases{query}", headers=auth)
        assert response.status_code == 200, response.text
        body = response.json()
        assert len(body["items"]) == body["total"], "a list page must not contradict its own count"
        return sorted(item["name"] for item in body["items"])

    assert names() == ["both-tags-case", "tagged-case", "untagged-case"]
    assert names("?tag=smoke") == ["both-tags-case", "tagged-case"]
    # Two tags are an OR over the requested tags, and a case carrying both is still listed once.
    assert names("?tag=smoke&tag=e2e") == ["both-tags-case", "tagged-case"]
    unknown = client.get(f"/api/v1/projects/{project}/cases?tag=nope", headers=auth)
    assert unknown.status_code == 400
    assert unknown.json()["error"]["code"] == "VALIDATION_ERROR"


def test_whoami_renders_the_specialist_grants_the_caller_holds(client, workspace, auth) -> None:
    """/whoami is the console's only authority source, so a grant row must never break it (§13.5)."""
    response = client.get("/api/v1/whoami", headers=auth)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["grants_by_project"][workspace["project_id"]] == ["human_control", "sensitive_artifact_read"]
    assert "human_control" in body["permissions_by_project"][workspace["project_id"]]


def test_status_split_matches_the_documented_classes():
    """413 is size, 422 is syntax or semantics, 409 is a state conflict — never interchangeable."""
    assert ApiError(ErrorCode.CASE_TOO_LARGE, "too big").http_status == 413
    assert ApiError(ErrorCode.PAYLOAD_TOO_LARGE, "too big").http_status == 413
    assert ApiError(ErrorCode.SEMANTIC_ERROR, "no such variable").http_status == 422
    for code in (
        ErrorCode.DSL_AMBIGUOUS_SYNTAX,
        ErrorCode.TARGET_NEEDS_LOCATOR,
        ErrorCode.ACTION_UNSUPPORTED,
        ErrorCode.IR_VALIDATION_FAILED,
        ErrorCode.VARIABLE_UNDECLARED,
        ErrorCode.SECRET_FIELD_NOT_ALLOWED,
    ):
        assert ApiError(code, "case says something the compiler cannot honour").http_status == 422, code
    for code in (
        ErrorCode.CONFLICT,
        ErrorCode.VERSION_CONFLICT,
        ErrorCode.IDEMPOTENCY_CONFLICT,
        ErrorCode.COMPILE_REVIEW_REQUIRED,
        ErrorCode.COMPILE_STALE_DIGEST,
    ):
        assert ApiError(code, "the resource moved").http_status == 409, code


def test_envelope_carries_code_message_and_details():
    error = ApiError(
        ErrorCode.FORBIDDEN, "Permission 'case_write' is required", details={"permission": "case_write"}
    ).as_envelope("req_1")
    assert error == {
        "error": {
            "code": "FORBIDDEN",
            "message": "Permission 'case_write' is required",
            "request_id": "req_1",
            "details": {"permission": "case_write"},
        }
    }


def test_ai_compilation_is_opt_in_at_every_layer():
    """A client that names no policy gets the deterministic compiler, so no save or recompile is a model call."""
    from backend.app.services.cases import compile_now, request_compile

    assert CompileRequest().use_ai is False
    # The policy lives in the signatures the save and recompile paths call.
    assert inspect.signature(request_compile).parameters["use_ai"].default is False
    assert inspect.signature(compile_now).parameters["use_ai"].default is False
