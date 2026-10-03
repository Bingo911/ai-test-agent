"""AC-25 / §13.6: who may change the structure, and what a process that cannot confirm it does.

`create_all` adds what is missing and never changes what is already there, so "the tables came up" is not the
same claim as "this is the structure this build runs on". Both halves are tested here: the one-shot job that
may create and must record, and the serving processes that may only read that record - including the two ways
that record can be wrong for them, a version they do not know and a table that exists in an older shape.

The "seeds nothing" cases run against an empty file rather than the seeded development workspace, because the
production rule ("never create a default admin or project") is only observable when nothing seeded it first.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml
from backend.app.config import Settings
from backend.app.db import schema as schema_module
from backend.app.db import schema_job
from backend.app.db.base import Database, _lock_key
from backend.app.db.bootstrap import bootstrap_runtime
from backend.app.db.models import AppUser, Project, SchemaMigration, Tenant
from backend.app.db.schema import (
    SCHEMA_CONTRACT,
    SCHEMA_LOCK,
    SCHEMA_VERSION,
    SchemaNotReady,
    apply_schema,
    declared_tables,
    verify_schema,
)
from sqlalchemy import func, inspect, select, text

REPO = Path(__file__).resolve().parents[1]

#: A `tag` table in the shape an older build had: the id, and none of the columns this one writes to.
NARROW_TAG = "CREATE TABLE tag (id VARCHAR(36) NOT NULL)"


@pytest.fixture
def empty_db(tmp_path: Path):
    """A database with nothing in it - the state a fresh deployment or a stale developer file is in."""
    database = Database(f"sqlite:///{tmp_path / 'schema.db'}")
    yield database
    database.dispose()


@pytest.fixture
def narrow_db(tmp_path: Path):
    """A database that already holds `tag` the way an older build created it, minus the newer columns."""
    database = Database(f"sqlite:///{tmp_path / 'narrow.db'}")
    with database.engine.begin() as connection:
        connection.execute(text(NARROW_TAG))
    yield database
    database.dispose()


def production(settings: Settings) -> Settings:
    return settings.model_copy(update={"app_env": "production"})


def table_names(database: Database) -> set[str]:
    return set(inspect(database.engine).get_table_names())


def recorded(database: Database) -> list[tuple[int, str, str]]:
    with database.session() as session:
        rows = session.scalars(select(SchemaMigration).order_by(SchemaMigration.version)).all()
        return [(row.version, row.contract, row.applied_by) for row in rows]


def count(database: Database, model: Any) -> int:
    with database.session() as session:
        return int(session.scalar(select(func.count()).select_from(model)) or 0)


def test_the_job_creates_every_table_and_records_the_version_it_applied(empty_db: Database) -> None:
    drift = apply_schema(empty_db)

    assert drift.ok, drift.summarize()
    assert drift.recorded_version == SCHEMA_VERSION
    assert set(declared_tables()) <= table_names(empty_db)
    assert recorded(empty_db) == [(SCHEMA_VERSION, SCHEMA_CONTRACT, "schema-job")]


def test_running_the_job_twice_leaves_one_record_per_version(empty_db: Database) -> None:
    apply_schema(empty_db)
    drift = apply_schema(empty_db)

    assert drift.ok, drift.summarize()
    assert recorded(empty_db) == [(SCHEMA_VERSION, SCHEMA_CONTRACT, "schema-job")]


def test_a_production_process_refuses_and_leaves_the_database_alone(empty_db: Database, settings: Settings) -> None:
    """AC-25: a serving process does not create structure, not even when creating it would have worked."""
    with pytest.raises(SchemaNotReady) as refused:
        bootstrap_runtime(production(settings), database=empty_db)

    assert table_names(empty_db) == set()
    assert "missing tables" in str(refused.value)


@pytest.mark.parametrize(
    ("setup", "expected"),
    [("bare", "no structure version"), ("ahead", f"recorded version {SCHEMA_VERSION + 3}")],
    ids=["never-recorded", "ahead-of-this-build"],
)
def test_a_structure_whose_record_does_not_match_is_refused(
    empty_db: Database, settings: Settings, setup: str, expected: str
) -> None:
    """A rollback to an older build finds a version ahead of its own and stops; so does an unrecorded one.

    `create_all` alone would call both of these databases ready - every table it declares is already there.
    """
    if setup == "bare":
        empty_db.create_schema()
    else:
        apply_schema(empty_db)
        with empty_db.session() as session:
            session.execute(
                text("UPDATE schema_migration SET version = :version"), {"version": SCHEMA_VERSION + 3}
            )

    with pytest.raises(SchemaNotReady) as refused:
        bootstrap_runtime(production(settings), database=empty_db)

    assert expected in str(refused.value)


def test_creating_tables_is_not_upgrading_them(narrow_db: Database) -> None:
    """A table that already exists in an older shape keeps its columns, and the job has to say so."""
    drift = apply_schema(narrow_db)

    assert "tag.name" in drift.missing_columns
    assert not drift.ok
    # The version is not recorded for a structure the job knows is short, or every later process would be
    # handed a row claiming this database had been brought up.
    assert recorded(narrow_db) == []
    with pytest.raises(SchemaNotReady):
        verify_schema(narrow_db)


def test_a_development_database_left_short_refuses_too(narrow_db: Database, settings: Settings) -> None:
    with pytest.raises(SchemaNotReady):
        bootstrap_runtime(settings, database=narrow_db)


def test_the_job_exits_zero_when_the_structure_is_complete(empty_db: Database, settings: Settings, monkeypatch) -> None:
    monkeypatch.setattr(schema_job, "get_database", lambda: empty_db)

    assert schema_job.run(settings) == 0
    assert recorded(empty_db) == [(SCHEMA_VERSION, SCHEMA_CONTRACT, "schema-job")]


def test_the_job_exits_nonzero_when_it_leaves_drift(narrow_db: Database, settings: Settings, monkeypatch) -> None:
    monkeypatch.setattr(schema_job, "get_database", lambda: narrow_db)

    assert schema_job.run(settings) == 1
    assert recorded(narrow_db) == []


def test_the_lock_is_held_across_the_ddl_and_the_version_record(empty_db: Database, monkeypatch) -> None:
    """AC-25's mechanism: the two writes that could interleave with another process happen inside one lock."""
    events: list[str] = []

    @contextmanager
    def counted(self: Database, name: str):
        events.append(f"enter:{name}")
        try:
            yield
        finally:
            events.append(f"exit:{name}")

    monkeypatch.setattr(Database, "advisory_lock", counted)
    monkeypatch.setattr(empty_db, "create_schema", lambda: events.append("ddl"))
    monkeypatch.setattr(schema_module, "_structure_gaps", lambda database: ((), ()))
    monkeypatch.setattr(
        schema_module, "_record_versions", lambda database, applied_by: events.append("record-versions")
    )

    apply_schema(empty_db)

    assert events == [f"enter:{SCHEMA_LOCK}", "ddl", "record-versions", f"exit:{SCHEMA_LOCK}"]


def test_the_lock_key_is_the_same_number_in_every_process() -> None:
    """`hash()` is salted per interpreter run, so two deployment processes would take two different locks."""
    name = "schema-lock-name"
    printed = subprocess.run(  # noqa: S603 - this interpreter, this repository, fixed arguments
        [sys.executable, "-c", f"from backend.app.db.base import _lock_key; print(_lock_key({name!r}))"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ, "PYTHONPATH": str(REPO), "PYTHONHASHSEED": "random"},
    ).stdout.strip()

    assert printed == str(_lock_key(name))
    assert _lock_key(SCHEMA_LOCK) == int.from_bytes(
        hashlib.sha256(SCHEMA_LOCK.encode("utf-8")).digest()[:8], "big"
    ) & ((1 << 63) - 1)
    assert 0 <= _lock_key(SCHEMA_LOCK) < 2**63
    assert _lock_key("schema") != _lock_key("migration")


def test_sqlite_never_opens_a_connection_for_the_lock(empty_db: Database, monkeypatch) -> None:
    def refuse() -> None:
        raise AssertionError("SQLite has one writer; the lock must not reach for a connection")

    monkeypatch.setattr(empty_db.engine, "connect", refuse)

    with empty_db.advisory_lock(SCHEMA_LOCK):
        passed = True

    assert passed


class _Connection:
    """Just enough of a DBAPI-wrapped connection to see which statements the lock issues."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, int]]] = []

    def execute(self, statement: Any, params: dict[str, int]) -> None:
        self.calls.append((str(statement.text), params))

    def commit(self) -> None:
        self.calls.append(("COMMIT", {}))

    def __enter__(self) -> _Connection:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


def test_postgres_locks_and_unlocks_the_same_key_even_when_the_body_fails() -> None:
    connection = _Connection()
    engine = SimpleNamespace(dialect=SimpleNamespace(name="postgresql"), connect=lambda: connection)
    stub = SimpleNamespace(engine=engine)

    with pytest.raises(RuntimeError), Database.advisory_lock(stub, SCHEMA_LOCK):  # type: ignore[arg-type]
        raise RuntimeError("the body failed")

    key = _lock_key(SCHEMA_LOCK)
    assert [call[0] for call in connection.calls] == [
        "SELECT pg_advisory_lock(:key)",
        "SELECT pg_advisory_unlock(:key)",
        "COMMIT",
    ]
    assert all(call[1] == {"key": key} for call in connection.calls[:2])


def test_a_production_bootstrap_seeds_nothing(empty_db: Database, settings: Settings) -> None:
    """§13.6: tenant, user and project configuration is a separate, controlled step - never a side effect."""
    apply_schema(empty_db)

    assert bootstrap_runtime(production(settings), database=empty_db) == {}
    assert count(empty_db, Tenant) == 0
    assert count(empty_db, Project) == 0
    assert count(empty_db, AppUser) == 0


def test_a_worker_process_confirms_the_structure_and_seeds_nothing(
    empty_db: Database, settings: Settings, monkeypatch
) -> None:
    """The Celery child checks before it announces, through the same role-gated call the API makes."""
    from backend.app import celery_app

    monkeypatch.setattr("backend.app.db.bootstrap.get_database", lambda: empty_db)
    monkeypatch.setattr(celery_app, "get_settings", lambda: production(settings))

    with pytest.raises(SchemaNotReady):
        celery_app._confirm_structure()

    assert table_names(empty_db) == set()

    monkeypatch.setattr(celery_app, "get_settings", lambda: settings)
    celery_app._confirm_structure()

    assert set(declared_tables()) <= table_names(empty_db)
    assert count(empty_db, Tenant) == 0


def test_no_serving_module_touches_the_structure() -> None:
    """AC-25 as a standing rule rather than a behaviour: DDL lives in exactly one place.

    A future `create_all()` added to a lifespan would pass every test above and still break the deployment.
    """
    bodies = {
        str(path.relative_to(REPO)).replace("\\", "/"): path.read_text(encoding="utf-8")
        for path in (REPO / "backend" / "app").rglob("*.py")
    }

    def written_outside(pattern: str, allowed: set[str]) -> set[str]:
        return {path for path, body in bodies.items() if pattern in body and path not in allowed}

    schema_files = {"backend/app/db/base.py", "backend/app/db/schema.py"}
    job_files = {"backend/app/db/bootstrap.py", "backend/app/db/schema_job.py"}
    assert not written_outside("metadata.create_all", schema_files)
    assert not written_outside(".create_schema(", schema_files)
    assert not written_outside("apply_schema(", schema_files | job_files)
    assert not written_outside("verify_schema(", schema_files | {"backend/app/db/bootstrap.py"})


def test_the_compose_topology_boots_the_structure_once() -> None:
    """The shipped development topology runs the job as a one-shot and gates everything else on its exit code."""
    services = yaml.safe_load((REPO / "compose.yaml").read_text(encoding="utf-8"))["services"]

    assert services["schema"]["command"] == ["python", "-m", "app.db.schema_job"]
    assert services["api"]["depends_on"]["schema"] == {"condition": "service_completed_successfully"}
    assert "schema" not in services["worker-execution"].get("depends_on", {})
    for name, service in services.items():
        assert "create_schema" not in " ".join(service.get("command") or []), f"{name} would run DDL at boot"
