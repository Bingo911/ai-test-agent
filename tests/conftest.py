"""Shared fixtures: an isolated SQLite workspace per test session, no network, no real AI."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

collect_ignore_glob = []

# SettingsConfigDict captures its env file when backend.app.config is imported.  Establish an isolated
# path before pytest imports application modules; setting AITA_ENV_FILE only in an autouse fixture is too
# late when a developer's local .env exists and can silently enable MCP or a real model during tests.
_TEST_ENV_ROOT = Path(tempfile.mkdtemp(prefix="aita-tests-"))
os.environ["AITA_ENV_FILE"] = str(_TEST_ENV_ROOT / "absent.env")


@pytest.fixture(scope="session", autouse=True)
def _environment() -> object:
    root = _TEST_ENV_ROOT
    browsers = Path(__file__).resolve().parent.parent / ".pw-browsers"
    if browsers.is_dir():
        # The downloaded browsers live next to the project, not in ~/.cache.
        os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(browsers))
    os.environ["AITA_ENV_FILE"] = str(root / "absent.env")
    os.environ.update(
        {
            "APP_ENV": "test",
            "DATABASE_URL": f"sqlite:///{root / 'test.db'}",
            "DATA_DIR": str(root / "data"),
            "QUEUE_BACKEND": "inprocess",
            "OBJECT_STORE": "local",
            "SECRET_MASTER_KEY": "",
            "AI_ENABLED": "false",
            "AI_BASE_URL": "",
            "REDIS_URL": "",
            "BROWSER_HEADLESS": "true",
            "BROWSER_SANDBOX": "false",
            "LOG_LEVEL": "WARNING",
        }
    )
    from backend.app.config import get_settings

    get_settings.cache_clear()
    settings = get_settings()
    settings.ensure_dirs()
    yield settings
    get_settings.cache_clear()


@pytest.fixture
def settings(_environment) -> object:
    from backend.app.config import get_settings

    return get_settings()


@pytest.fixture
def database(settings, tmp_path) -> object:
    """A schema-built SQLite database registered as the process-wide one, as every runtime path expects.

    One file per test: the orchestrator tables (outbox, pools, reservations) are read platform-wide,
    so a leftover row from another test would be visible to a test that did not write it.
    """
    from backend.app.db.base import Database, set_database
    from backend.app.db.schema import apply_schema
    from backend.app.services.object_store import set_object_store

    database = Database(f"sqlite:///{tmp_path / 'case.db'}")
    # The same entry point the deploy job uses: a test database that carries no recorded structure version
    # would be a database the readiness probe correctly calls stale, and every probe test would fail for the
    # wrong reason.
    apply_schema(database, applied_by="tests")
    set_database(database)
    set_object_store(None)
    yield database
    set_database(None)  # type: ignore[arg-type]
    set_object_store(None)
    database.dispose()


@pytest.fixture(scope="session")
def site(_environment):
    """The loopback fixture site: a real origin, because `file:` navigation is forbidden (§14.2)."""
    from .site_server import SiteServer

    with SiteServer() as server:
        yield server


@pytest.fixture
def session(database):
    with database.session() as session:
        yield session


@pytest.fixture
def workspace(session, settings):
    """Seeded tenant/project/environment ids for the local development workspace."""
    from backend.app.db.bootstrap import ensure_development_workspace

    return ensure_development_workspace(session, settings)
