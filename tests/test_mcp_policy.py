"""§5.5: the per-project MCP data policy - its typed read, the REST-only writer, and the seed (AC-33, 35, 36).

The policy is the one place where an administrator decides what may leave the platform toward a model
this project has never met, so these cases are about refusal and about *which* value is authoritative:

* a stored policy that cannot be read is four flags off, everywhere, including in the console's own
  payload - while the stored bytes stay untouched for the administrator to repair;
* the partial endpoint is the only writer that keeps its neighbours, so it must never lose
  `allow_vision` or `human_slot_ratio`;
* seeding happens once, because a restart that re-enabled a policy an administrator closed would make
  the closure meaningless.
"""

from __future__ import annotations

from typing import Any

import pytest
from backend.app.config import Settings
from backend.app.db.bootstrap import DEVELOPMENT_MCP_POLICY, ensure_development_workspace
from backend.app.db.models import AuditLog, Project
from backend.app.domain.errors import ApiError
from backend.app.domain.mcp_policy import (
    POLICY_KEY,
    McpPolicy,
    apply_policy_to_settings,
    merge_policy,
    read_policy,
    validate_settings_for_write,
)
from backend.app.main import create_app
from fastapi.testclient import TestClient

CLOSED = {"enabled": False, "allow_case_content": False, "allow_report_details": False, "allow_server_ai": False}
#: A stored document this build cannot read: one field is a string, and a second one was never a field.
BROKEN = {"enabled": "yes", "allow_case_content": True}
FIELDS = ("enabled", "allow_case_content", "allow_report_details", "allow_server_ai")


@pytest.fixture
def client(database, session, workspace, settings) -> TestClient:
    session.commit()
    return TestClient(create_app(settings))


@pytest.fixture
def admin(settings) -> dict[str, str]:
    return {"Authorization": f"Bearer {settings.dev_admin_token}"}


@pytest.fixture
def engineer(settings) -> dict[str, str]:
    return {"Authorization": f"Bearer {settings.dev_engineer_token}"}


def _view(client: TestClient, auth: dict[str, str], project_id: str) -> tuple[int, dict[str, bool]]:
    response = client.get(f"/api/v1/projects/{project_id}", headers=auth)
    assert response.status_code == 200, response.text
    return int(response.json()["row_version"]), response.json()["mcp_policy"]


def _match(project_id: str, version: int, **headers: str) -> dict[str, str]:
    """The ETag the project resource issues also guards the policy writer: one `row_version`, two doors."""
    return {"If-Match": f'"project-{project_id}-{version}"', **headers}


def _store_settings(database, project_id: str, value: dict[str, Any]) -> None:
    """Write settings directly, bypassing both REST writers, to set up a state no writer can produce."""
    with database.session() as session:
        session.get(Project, project_id).settings = value
        session.commit()


def _settings_of(database, project_id: str) -> dict[str, Any]:
    with database.session() as session:
        return dict(session.get(Project, project_id).settings or {})


# --------------------------------------------------------------------------------------
# the typed read
# --------------------------------------------------------------------------------------


def test_a_project_without_a_policy_reads_as_four_flags_off():
    for stored in (None, {}, {"allow_vision": True}):
        view = read_policy(stored)
        assert view.policy.model_dump() == CLOSED
        assert view.invalid is False


@pytest.mark.parametrize(
    "broken",
    [
        {"enabled": "true"},  # a string is not a decision
        {"enabled": 1},  # nor is a number
        {"enabled": True, "allow_everything": True},  # an unknown key is not a typo to ignore
        {"enabled": True, "allow_case_content": "yes"},  # one bad field invalidates the whole document
        "on",
        [],
    ],
)
def test_a_stored_policy_that_does_not_parse_is_switched_off_whole(broken, caplog):
    with caplog.at_level("WARNING"):
        view = read_policy({POLICY_KEY: broken})

    assert view.policy.model_dump() == CLOSED
    assert view.invalid is True
    # A stable code, and no echo of the stored value: the reason has to be loggable without logging data.
    assert "mcp_policy_invalid" in caplog.text


def test_reading_a_policy_leaves_the_other_settings_keys_alone():
    stored = {"allow_vision": True, "human_slot_ratio": 0.5, POLICY_KEY: {"enabled": True}}
    view = read_policy(stored)

    # Only the sub-object is validated, so the pre-existing project settings are never extra fields.
    assert view.policy.enabled is True
    assert view.invalid is False
    assert apply_policy_to_settings(stored, merge_policy(view.policy, {"allow_report_details": True})) == {
        "allow_vision": True,
        "human_slot_ratio": 0.5,
        POLICY_KEY: {**CLOSED, "enabled": True, "allow_report_details": True},
    }


def test_a_whole_settings_replacement_reports_the_policy_it_will_effect():
    assert validate_settings_for_write({"allow_vision": True}) == CLOSED
    assert validate_settings_for_write({POLICY_KEY: {"enabled": True}})["enabled"] is True
    with pytest.raises(ApiError) as refused:
        validate_settings_for_write({POLICY_KEY: {"enabled": "false"}})
    assert refused.value.code.value == "VALIDATION_ERROR"


def test_the_policy_model_defaults_to_closed_and_carries_four_fields():
    assert McpPolicy().model_dump() == CLOSED
    assert tuple(McpPolicy.model_fields) == FIELDS


# --------------------------------------------------------------------------------------
# the REST-only partial update
# --------------------------------------------------------------------------------------


def test_the_partial_update_keeps_the_settings_it_was_not_given(client, admin, database, workspace, settings):
    project_id = workspace["project_id"]
    before, policy = _view(client, admin, project_id)
    assert policy == CLOSED

    response = client.patch(
        f"/api/v1/projects/{project_id}/mcp-policy",
        json={"enabled": True, "allow_case_content": True},
        headers={**admin, **_match(project_id, before)},
    )
    assert response.status_code == 200, response.text

    # Two flags changed; the neighbours survived, and the internal-AI direction stayed shut (§5.5).
    stored = _settings_of(database, project_id)
    assert stored["allow_vision"] == settings.ai_vision_enabled
    assert stored["human_slot_ratio"] == settings.human_slot_ratio
    assert stored[POLICY_KEY] == {**CLOSED, "enabled": True, "allow_case_content": True}
    assert response.json()["mcp_policy"] == stored[POLICY_KEY]
    assert response.json()["row_version"] == before + 1
    assert response.headers["etag"] == f'"project-{project_id}-{before + 1}"'

    after, effective = _view(client, admin, project_id)
    assert after == before + 1
    assert effective["enabled"] is True
    assert effective["allow_server_ai"] is False


def test_the_update_and_its_audit_commit_in_the_same_transaction(client, admin, database, workspace):
    project_id = workspace["project_id"]
    before, _ = _view(client, admin, project_id)
    client.patch(
        f"/api/v1/projects/{project_id}/mcp-policy",
        json={"enabled": True},
        headers={**admin, **_match(project_id, before)},
    )

    with database.session() as session:
        rows = session.query(AuditLog).filter(AuditLog.operation == "project.mcp_policy.update").all()
    assert len(rows) == 1
    # The audit carries the normalised pair and the field names, and nothing else (§5.5).
    assert rows[0].detail["fields"] == ["enabled"]
    assert rows[0].detail["before"] == CLOSED
    assert rows[0].detail["after"] == {**CLOSED, "enabled": True}


@pytest.mark.parametrize(
    ("body", "field"),
    [
        ({"enabled": "true"}, "enabled"),
        ({"approve_mcp": True}, "approve_mcp"),
        ({"allow_server_ai": 1}, "allow_server_ai"),
    ],
)
def test_the_policy_endpoint_refuses_what_is_not_a_boolean(client, admin, database, workspace, body, field):
    project_id = workspace["project_id"]
    before, _ = _view(client, admin, project_id)

    response = client.patch(
        f"/api/v1/projects/{project_id}/mcp-policy",
        json=body,
        headers={**admin, **_match(project_id, before)},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
    assert field in str(response.json()["error"]["details"])
    # Refused means nothing moved: not the version, and not one flag.
    assert _view(client, admin, project_id) == (before, CLOSED)


def test_the_policy_endpoint_requires_the_version_the_caller_actually_read(client, admin, workspace):
    project_id = workspace["project_id"]
    missing = client.patch(f"/api/v1/projects/{project_id}/mcp-policy", json={"enabled": True}, headers=admin)
    assert missing.status_code == 400
    assert "If-Match" in missing.json()["error"]["message"]

    stale = client.patch(
        f"/api/v1/projects/{project_id}/mcp-policy",
        json={"enabled": True},
        headers={**admin, **_match(project_id, 9999)},
    )
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "VERSION_CONFLICT"


def test_an_empty_patch_changes_nothing_and_says_so(client, admin, workspace):
    project_id = workspace["project_id"]
    before, _ = _view(client, admin, project_id)
    response = client.patch(
        f"/api/v1/projects/{project_id}/mcp-policy",
        json={},
        headers={**admin, **_match(project_id, before)},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_turning_mcp_on_is_not_a_permission_a_model_can_ask_for(client, engineer, workspace):
    """The endpoint sits behind PROJECT_MANAGE, and no MCP tool reaches it (§5.5)."""
    project_id = workspace["project_id"]
    before, _ = _view(client, engineer, project_id)
    refused = client.patch(
        f"/api/v1/projects/{project_id}/mcp-policy",
        json={"enabled": True},
        headers={**engineer, **_match(project_id, before)},
    )
    assert refused.status_code == 403
    assert refused.json()["error"]["code"] == "FORBIDDEN"
    assert _view(client, engineer, project_id)[1] == CLOSED


def test_an_unreadable_stored_policy_can_only_be_replaced_whole(client, admin, database, workspace):
    project_id = workspace["project_id"]
    _store_settings(database, project_id, {"allow_vision": True, "human_slot_ratio": 0.5, POLICY_KEY: BROKEN})
    before, effective = _view(client, admin, project_id)
    # The console shows what MCP will actually do with this project, while the stored bytes stay put.
    assert effective == CLOSED

    partial = client.patch(
        f"/api/v1/projects/{project_id}/mcp-policy",
        json={"enabled": True},
        headers={**admin, **_match(project_id, before)},
    )
    assert partial.status_code == 400
    assert partial.json()["error"]["details"]["required_fields"] == list(FIELDS)
    # A refused repair must not have normalised anything behind the caller's back.
    assert _settings_of(database, project_id)[POLICY_KEY] == BROKEN

    repair = client.patch(
        f"/api/v1/projects/{project_id}/mcp-policy",
        json={**CLOSED, "enabled": True},
        headers={**admin, **_match(project_id, before)},
    )
    assert repair.status_code == 200, repair.text
    stored = _settings_of(database, project_id)
    assert stored[POLICY_KEY] == {**CLOSED, "enabled": True}
    assert stored["allow_vision"] is True
    assert stored["human_slot_ratio"] == 0.5


# --------------------------------------------------------------------------------------
# the existing whole-settings replacement, which now has to agree about the policy
# --------------------------------------------------------------------------------------


def test_replacing_settings_without_a_policy_line_closes_mcp_and_records_it(client, admin, database, workspace):
    project_id = workspace["project_id"]
    before, _ = _view(client, admin, project_id)
    client.patch(
        f"/api/v1/projects/{project_id}/mcp-policy",
        json={"enabled": True, "allow_report_details": True},
        headers={**admin, **_match(project_id, before)},
    )

    response = client.patch(
        f"/api/v1/projects/{project_id}",
        json={"settings": {"allow_vision": False, "human_slot_ratio": 0.25}},
        headers={**admin, **_match(project_id, before + 1)},
    )
    assert response.status_code == 200, response.text

    # Omitting the key is the default-closed policy, not "keep what was there" - and still whole-replace.
    stored = _settings_of(database, project_id)
    assert POLICY_KEY not in stored
    assert stored["allow_vision"] is False
    assert response.json()["mcp_policy"] == CLOSED

    with database.session() as session:
        row = (
            session.query(AuditLog)
            .filter(AuditLog.operation == "project.update")
            .order_by(AuditLog.id.desc())
            .first()
        )
    change = row.detail["mcp_policy"]
    assert change["after"] == CLOSED
    assert change["changed"] is True
    assert change["before"] == {**CLOSED, "enabled": True, "allow_report_details": True}


def test_replacing_settings_still_validates_the_policy_it_carries(client, admin, database, workspace):
    project_id = workspace["project_id"]
    before, _ = _view(client, admin, project_id)
    stored_before = _settings_of(database, project_id)

    response = client.patch(
        f"/api/v1/projects/{project_id}",
        json={"settings": {"allow_vision": False, POLICY_KEY: {"enabled": "true"}}},
        headers={**admin, **_match(project_id, before)},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
    # A rejected replacement must not have written any of the settings it came with, not even the ones
    # that were valid on their own.
    assert _settings_of(database, project_id) == stored_before
    assert _view(client, admin, project_id) == (before, CLOSED)


# --------------------------------------------------------------------------------------
# the seed, and what a restart must not do to it
# --------------------------------------------------------------------------------------


def _workspace_settings(tmp_path, database, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "app_env": "test",
        "database_url": database.url,
        "data_dir": tmp_path / "data",
        "queue_backend": "inprocess",
        "object_store": "local",
        "ai_enabled": False,
        "redis_url": "",
        "log_level": "WARNING",
    }
    values.update(overrides)
    return Settings(**values)


def test_a_new_development_project_is_seeded_with_mcp_open_and_server_ai_shut(database, tmp_path):
    settings = _workspace_settings(tmp_path, database, mcp_enabled=True)
    with database.session() as session:
        workspace = ensure_development_workspace(session, settings)
        session.commit()

    stored = _settings_of(database, workspace["project_id"])
    assert stored[POLICY_KEY] == DEVELOPMENT_MCP_POLICY
    assert DEVELOPMENT_MCP_POLICY["allow_server_ai"] is False
    assert "allow_vision" in stored


def test_the_seed_leaves_the_policy_alone_when_mcp_is_disabled(database, tmp_path):
    settings = _workspace_settings(tmp_path, database)
    with database.session() as session:
        workspace = ensure_development_workspace(session, settings)
        session.commit()

    # MCP off means the original seed default: no policy written, so nothing is enabled by implication.
    assert POLICY_KEY not in _settings_of(database, workspace["project_id"])


def test_seeding_twice_does_not_reopen_a_policy_an_administrator_closed(
    client, admin, database, workspace, settings
):
    project_id = workspace["project_id"]
    before, _ = _view(client, admin, project_id)
    client.patch(
        f"/api/v1/projects/{project_id}/mcp-policy",
        json={"enabled": True},
        headers={**admin, **_match(project_id, before)},
    )
    # A deliberately closed, and deliberately unreadable, stored policy: the worst case for a restart.
    _store_settings(database, project_id, {"allow_vision": True, POLICY_KEY: BROKEN})
    closed_version = _view(client, admin, project_id)[0]

    with database.session() as session:
        ensure_development_workspace(session, settings)
        session.commit()

    # A restart must not undo a closure, must not rewrite what it stored, and must not touch the version.
    assert _view(client, admin, project_id) == (closed_version, CLOSED)
    assert _settings_of(database, project_id)[POLICY_KEY] == BROKEN
    with database.session() as session:
        assert session.query(AuditLog).filter(AuditLog.operation == "project.mcp_policy.update").count() == 1
