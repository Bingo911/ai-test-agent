"""M1: two factory-built app instances in one process, each with its own Settings and Database (AC-02, §4.4).

Injection is only worth having if it is total. One path left reading `get_database()` would make the
isolation below intermittent — it would pass for the routes that were converted and leak through the
rest, which is exactly the kind of bug a single-app test suite cannot see. So every case here writes
through one app and reads through both, and re-checks the process-wide pool afterwards.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from backend.app.config import Settings
from backend.app.db.base import Database, get_database
from backend.app.db.models import Project, TestCase
from backend.app.main import create_app
from backend.app.orchestrator.runtime import Supervisor
from fastapi.testclient import TestClient
from sqlalchemy import func, select

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


class Workspace:
    """One app, the database it was handed, and the file its own data directory lives in."""

    def __init__(self, client: TestClient, database: Database, settings: Settings) -> None:
        self.client = client
        self.database = database
        self.settings = settings

    @property
    def auth(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.settings.dev_engineer_token}"}

    def project_id(self) -> str:
        """Read from this workspace's own pool, so a row found here proves it was not borrowed."""
        with self.database.session() as session:
            return str(session.scalar(select(Project.id).limit(1)))

    def case_names(self) -> list[str]:
        response = self.client.get(
            f"/api/v1/projects/{self.project_id()}/cases", headers=self.auth
        )
        assert response.status_code == 200, response.text
        return [item["name"] for item in response.json()["items"]]

    def save_case(self, name: str) -> dict[str, Any]:
        response = self.client.post(
            f"/api/v1/projects/{self.project_id()}/cases",
            json={"name": name, "markdown": MARKDOWN, "dsl_version": "1.0"},
            headers={**self.auth, "Idempotency-Key": f"compose-{name}"},
        )
        assert response.status_code == 201, response.text
        return response.json()

    def rows(self) -> int:
        with self.database.session() as session:
            return int(session.scalar(select(func.count()).select_from(TestCase)) or 0)


@contextmanager
def _workspace(tmp_path, name: str, **overrides: Any) -> Iterator[Workspace]:
    """Build and start one app with its own Settings, its own database file and its own data directory.

    The lifespan runs because the context manager enters it: that is what seeds the schema into the
    injected database rather than into whichever pool the process happened to register first.
    """
    database = Database(f"sqlite:///{tmp_path / f'{name}.db'}")
    values: dict[str, Any] = {
        "app_env": "test",
        "database_url": database.url,
        "data_dir": tmp_path / f"{name}-data",
        "queue_backend": "inprocess",
        "object_store": "local",
        "ai_enabled": False,
        "redis_url": "",
        "log_level": "WARNING",
    }
    values.update(overrides)
    settings = Settings(**values)
    client = TestClient(create_app(settings, database=database))
    with client:
        yield Workspace(client, database, settings)
    database.dispose()


def test_each_app_answers_from_the_database_it_was_built_with(tmp_path) -> None:
    """A case saved through one app is invisible to the other, in both directions (§4.4)."""
    with _workspace(tmp_path, "left") as left, _workspace(tmp_path, "right") as right:
        assert left.save_case("login-smoke")["case_id"]
        assert right.save_case("cart-checkout")["case_id"]

        assert left.case_names() == ["login-smoke"]
        assert right.case_names() == ["cart-checkout"]
        # The two seed the same deterministic development ids, so a shared pool would show both names.
        assert left.project_id() == right.project_id()


def test_the_injected_databases_are_not_the_process_one(tmp_path, database) -> None:
    """Building and starting an app must never rebind the module-global pool (§9.4, AC-37)."""
    process = get_database()
    with _workspace(tmp_path, "outer") as outer:
        assert get_database() is process
        assert outer.database is not process
        assert outer.save_case("login-smoke")
        assert get_database() is process
        assert outer.rows() == 1
    with process.session() as session:
        assert session.scalar(select(func.count()).select_from(TestCase)) == 0


def test_an_app_without_a_database_of_its_own_uses_the_process_one(tmp_path, database, session, workspace) -> None:
    """The single-root deployment is the common case: state.database left unset keeps old behaviour."""
    session.commit()
    settings = Settings(
        app_env="test",
        database_url=database.url,
        data_dir=tmp_path / "shared-data",
        queue_backend="inprocess",
        object_store="local",
        ai_enabled=False,
        redis_url="",
        log_level="WARNING",
    )
    inner = Workspace(TestClient(create_app(settings)), database, settings)
    assert inner.project_id() == workspace["project_id"]
    assert inner.save_case("login-smoke")["case_id"]
    assert inner.rows() == 1


def test_health_probes_the_pool_the_app_was_built_with(tmp_path) -> None:
    """A liveness check that reports the process pool would keep answering `ok` for a dead app (§15.4).

    No lifespan here on purpose: the point is the probe, and starting one against an unusable path
    would fail in `create_schema` before ever reaching the route.
    """
    broken = Database(f"sqlite:///{tmp_path / 'absent' / 'nested.db'}")
    settings = Settings(
        app_env="test",
        database_url=broken.url,
        data_dir=tmp_path / "broken-data",
        queue_backend="inprocess",
        object_store="local",
        ai_enabled=False,
        redis_url="",
        log_level="WARNING",
    )
    dead = TestClient(create_app(settings, database=broken))
    with _workspace(tmp_path, "alive") as alive:
        assert alive.client.get("/api/v1/health").json()["database"] == "ok"
        assert dead.get("/api/v1/health").json()["status"] == "degraded"
    broken.dispose()


def test_the_development_lifespan_starts_one_supervisor_and_test_none(tmp_path, monkeypatch) -> None:
    """The loops belong to the single-process server, once per app, and to nothing else (§15.1)."""
    started: list[str] = []

    class Stub(Supervisor):
        def __init__(self, settings: Settings) -> None:
            super().__init__(settings)
            started.append("constructed")

        def start(self) -> Stub:
            started.append("started")
            return self

        def stop(self) -> None:
            started.append("stopped")

    import backend.app.orchestrator.runtime as runtime

    monkeypatch.setattr(runtime, "Supervisor", Stub)
    with _workspace(tmp_path, "loops", app_env="development") as looped:
        assert started == ["constructed", "started"]
        assert looped.case_names() == []
    assert started == ["constructed", "started", "stopped"]

    started.clear()
    with _workspace(tmp_path, "api") as api_only:
        assert started == []
        assert api_only.case_names() == []


def test_an_app_reads_its_own_limits_and_another_app_reads_its_own(tmp_path) -> None:
    """Capability discovery is per instance: the console must not be shown another app's budgets."""
    with (
        _workspace(tmp_path, "small", max_case_bytes=2_000) as small,
        _workspace(tmp_path, "large", max_case_bytes=90_000) as large,
    ):
        assert small.client.get("/api/v1/capabilities", headers=small.auth).json()["limits"][
            "max_case_bytes"
        ] == 2_000
        assert large.client.get("/api/v1/capabilities", headers=large.auth).json()["limits"][
            "max_case_bytes"
        ] == 90_000
        assert small.settings.max_case_bytes != large.settings.max_case_bytes
