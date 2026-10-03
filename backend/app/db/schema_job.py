"""The one-shot the deployment runs before any serving process starts (§13.6, AC-25).

This is the only thing in the production topology that creates tables. It checks its own work rather than
trusting `create_all`, because a database that already held an older shape of a table would otherwise be
reported as ready: `create_all` adds what is missing and changes nothing that is there.

    python -m app.db.schema_job            # inside the api image, with the deployment's environment

Exits 0 only when the structure this build declares is present and recorded. Anything else is a deployment
that must not start its API, orchestrator or Workers.
"""

from __future__ import annotations

import sys

from ..config import Settings, get_settings
from ..observability import configure_logging, get_logger
from .base import get_database
from .schema import SCHEMA_CONTRACT, SCHEMA_VERSION, apply_schema, declared_tables

log = get_logger(__name__)


def run(settings: Settings | None = None) -> int:
    settings = settings or get_settings()
    settings.validate_runtime()
    settings.ensure_dirs()
    drift = apply_schema(get_database())
    if not drift.ok:
        log.error("schema job left drift", extra={"context": {"detail": drift.summarize()}})
        return 1
    log.info(
        "schema applied",
        extra={"context": {"version": SCHEMA_VERSION, "contract": SCHEMA_CONTRACT, "tables": len(declared_tables())}},
    )
    return 0


def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level, json_output=not settings.is_development)
    try:
        code = run(settings)
    except Exception as exc:
        # Only the class name, and no traceback: a driver exception can carry the connection string it could
        # not open, and a deployment log is not somewhere to put a DSN (§11.1).
        log.error(  # noqa: TRY400 - logging.exception would write that DSN into the deployment log
            "schema job failed", extra={"context": {"error": type(exc).__name__}}
        )
        sys.exit(2)
    sys.exit(code)


if __name__ == "__main__":
    main()
