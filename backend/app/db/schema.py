"""The structure this build needs, and who is allowed to give it (§13.6, AC-25).

Creating tables is not upgrading them: `create_all` adds what is missing and never changes a column, a
constraint or an index that already exists. So the deploy job records the version it brought the database
to, and every process that only serves traffic - the API, the orchestrator, a Worker - reads that record
and the actual column list before it touches the database. A process that cannot confirm the structure says
so and stops, rather than starting a request path that fails on the first write.

What is compared is tables and columns. Constraints and indexes are not: changing one of those is a
migration with its own DDL and its own rollback steps, and a starting process could not infer it from a
number anyway.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, inspect, select
from sqlalchemy.exc import IntegrityError

from .base import Base, Database, utcnow
from .models import SchemaMigration

#: The structure this build's models describe. Every DDL-bearing change appends to `MIGRATIONS` and raises
#: this number. A deployment that rolls back to an older build finds the recorded version ahead of its own
#: and refuses to start - that answer is why the version is recorded at all.
SCHEMA_VERSION = 1

#: Which document the number comes from, kept beside every row so an operator reading the table does not
#: need this build to interpret it.
SCHEMA_CONTRACT = "detail-design-v1.3"

#: The names a startup log may quote at most. Table and column names are not secrets, but a log line built
#: from a database that has drifted badly should still be a line.
REPORT_LIMIT = 20

#: The lock that serialises structure changes. One name for the whole platform: two processes applying
#: different parts of the same structure is the failure this prevents, so they must contend for the same key.
SCHEMA_LOCK = "ai-test-agent:schema"

#: Read from the model rather than repeated as a string: the guard in `current_drift` has to name the table
#: that holds the version, and a renamed model would otherwise turn "no structure yet" back into a driver error.
SCHEMA_MIGRATION_TABLE = SchemaMigration.__tablename__


@dataclass(frozen=True)
class Migration:
    version: int
    contract: str
    note: str


MIGRATIONS: tuple[Migration, ...] = (
    Migration(
        version=1,
        contract=SCHEMA_CONTRACT,
        note="baseline: every table this build declares, including the command, event and idempotency tables",
    ),
)


@dataclass(frozen=True)
class SchemaDrift:
    """The difference between the structure the models declare and the one the database holds."""

    missing_tables: tuple[str, ...] = ()
    missing_columns: tuple[str, ...] = ()
    recorded_version: int | None = None

    @property
    def ok(self) -> bool:
        return not self.missing_tables and not self.missing_columns and self.recorded_version == SCHEMA_VERSION

    def summarize(self) -> str:
        parts: list[str] = []
        if self.missing_tables:
            parts.append(f"missing tables: {', '.join(self.missing_tables[:REPORT_LIMIT])}")
        if self.missing_columns:
            parts.append(f"missing columns: {', '.join(self.missing_columns[:REPORT_LIMIT])}")
        if self.recorded_version is None:
            parts.append("no structure version has been recorded here")
        elif self.recorded_version != SCHEMA_VERSION:
            parts.append(f"recorded version {self.recorded_version}, this build needs {SCHEMA_VERSION}")
        return "; ".join(parts) or "the structure matches"


class SchemaNotReady(RuntimeError):
    """The database this process was pointed at is not the structure it runs on (§13.6)."""

    def __init__(self, drift: SchemaDrift) -> None:
        self.drift = drift
        super().__init__(drift.summarize())


def declared_tables() -> dict[str, tuple[str, ...]]:
    """Every table this build declares, with its columns - the list the database is compared against."""
    return {name: tuple(column.name for column in table.columns) for name, table in Base.metadata.tables.items()}


def _structure_gaps(database: Database) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Tables and columns this build declares that the database does not hold.

    An unreachable database raises from the driver rather than returning gaps: "not there" and "not this
    shape" are two different answers, and the readiness probe reports them under different codes (§13.5).
    """
    inspector = inspect(database.engine)
    present = set(inspector.get_table_names())
    missing_tables: list[str] = []
    missing_columns: list[str] = []
    for name, columns in sorted(declared_tables().items()):
        if name not in present:
            missing_tables.append(name)
            continue
        actual = {str(column["name"]) for column in inspector.get_columns(name)}
        missing_columns.extend(f"{name}.{column}" for column in columns if column not in actual)
    return tuple(missing_tables), tuple(missing_columns)


def recorded_version(database: Database) -> int | None:
    """The highest structure version a job has recorded here, or None when nothing has been recorded.

    The missing-table case is answered rather than raised: a production process pointed at an empty database
    must be told "this is not the structure you run on", and a query against a table that is not there would
    be a driver error instead of that answer.
    """
    inspector = inspect(database.engine)
    if SCHEMA_MIGRATION_TABLE not in set(inspector.get_table_names()):
        return None
    with database.session() as session:
        recorded = session.scalar(select(func.max(SchemaMigration.version)))
    return int(recorded) if recorded is not None else None


def current_drift(database: Database) -> SchemaDrift:
    """Read the database as it is now, without changing anything."""
    missing_tables, missing_columns = _structure_gaps(database)
    return SchemaDrift(
        missing_tables=missing_tables,
        missing_columns=missing_columns,
        recorded_version=recorded_version(database),
    )


def apply_schema(database: Database, *, applied_by: str = "schema-job") -> SchemaDrift:
    """Create what is missing, record the version, and return whatever drift is still there.

    The version is only recorded when the shape came out right: a job that left a table older than this
    build would otherwise hand every later process a row saying "version 1 applied" about a structure it
    knows is wrong (§13.6).

    The lock is what makes AC-25's "no concurrent create_all" true in the shipped development topology,
    where the API and both Celery queues boot at the same moment against one PostgreSQL. Two holders cannot
    interleave DDL, and the second one finds every table already present, so `create_all` degenerates to a
    no-op read of the catalogue.
    """
    with database.advisory_lock(SCHEMA_LOCK):
        database.create_schema()
        missing_tables, missing_columns = _structure_gaps(database)
        if not missing_tables and not missing_columns:
            _record_versions(database, applied_by)
    return current_drift(database)


def _record_versions(database: Database, applied_by: str) -> None:
    with database.session() as session:
        for migration in MIGRATIONS:
            existing = session.scalar(select(SchemaMigration).where(SchemaMigration.version == migration.version))
            if existing is not None:
                continue
            try:
                with session.begin_nested():
                    session.add(
                        SchemaMigration(
                            version=migration.version,
                            contract=migration.contract,
                            applied_by=applied_by,
                            note=migration.note[:300],
                            created_at=utcnow(),
                            updated_at=utcnow(),
                        )
                    )
            except IntegrityError:
                # Another process of this same build recorded it first. The structure belongs to the
                # database rather than to the process, so the loser of that race applied the same thing.
                pass
        session.commit()


def verify_schema(database: Database) -> SchemaDrift:
    """Confirm the structure, or refuse: this is the only schema call a serving process makes."""
    drift = current_drift(database)
    if not drift.ok:
        raise SchemaNotReady(drift)
    return drift
