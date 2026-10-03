"""Database engine, session and tenant context (§11.1, §14.2)."""

from __future__ import annotations

import hashlib
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone

from sqlalchemy import DATETIME, TIMESTAMP, TypeDecorator, create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import QueuePool

from ..config import get_settings
from ..observability import get_logger

log = get_logger(__name__)


class Base(DeclarativeBase):
    pass


def new_id() -> str:
    return str(uuid.uuid4())


def utcnow() -> datetime:
    """Wall clock for deadlines and leases; always tz-aware UTC."""
    return datetime.now(timezone.utc)


def coerce_utc(value: datetime | None) -> datetime | None:
    """Normalise a timestamp to an aware UTC value.

    SQLite hands back naive datetimes while PostgreSQL `timestamptz` returns aware ones, and
    Python refuses to mix the two. Everything crossing the database boundary goes through here.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class UTCDateTime(TypeDecorator):
    """Store every timestamp as UTC and always read it back tz-aware.

    Applied to all DateTime columns so a lease check or deadline comparison cannot raise
    `TypeError: can't compare offset-naive and offset-aware datetimes` on one dialect and not
    the other.
    """

    impl = TIMESTAMP(timezone=True)
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "sqlite":
            return dialect.type_descriptor(DATETIME())
        return dialect.type_descriptor(TIMESTAMP(timezone=True))

    def process_bind_param(self, value: datetime | None, dialect) -> datetime | None:
        if value is None:
            return None
        aware = coerce_utc(value)
        # SQLite has no offset in its text format, so hand it naive UTC text.
        return aware.replace(tzinfo=None) if dialect.name == "sqlite" else aware

    def process_result_value(self, value: datetime | None, dialect) -> datetime | None:
        return coerce_utc(value)


def monotonic_ms() -> float:
    """Monotonic clock for in-process durations (§9.5)."""
    return time.monotonic() * 1000


def is_past(value: datetime | None, *, now: datetime | None = None) -> bool:
    """True when a stored deadline has already elapsed; treats NULL as 'not past'."""
    reference = coerce_utc(value)
    if reference is None:
        return False
    return reference <= (now or utcnow())


def _lock_key(name: str) -> int:
    """Map a lock name onto the bigint `pg_advisory_lock` takes.

    `hash()` would be the obvious choice and is wrong: CPython salts string hashing per process, so
    two deployment processes would derive different keys and both would run DDL at once. sha256 is
    stable across processes, and the 63-bit mask keeps the value inside the signed bigint range.
    """
    digest = hashlib.sha256(name.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") & ((1 << 63) - 1)


class Database:
    def __init__(
        self,
        url: str,
        *,
        pool_size: int = 5,
        max_overflow: int | None = None,
        pool_timeout: float | None = None,
        sqlite_busy_timeout_ms: int = 10_000,
        echo: bool = False,
    ) -> None:
        # `max_overflow=None` keeps the historical behaviour of borrowing up to `pool_size` extra
        # connections; passing 0 is how a pool says it must never borrow beyond its own size (§4.4).
        overflow = pool_size if max_overflow is None else max_overflow
        kwargs: dict[str, object] = {"echo": echo, "future": True}
        if url.startswith("sqlite"):
            kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
            if max_overflow is not None or pool_timeout is not None:
                # An explicitly pooled sqlite engine is the MCP one: a fixed number of file-backed
                # connections with a short wait, so MCP cannot borrow the REST pool's capacity. The
                # pool class is named rather than inherited so `:memory:` could never reach it.
                kwargs.update(
                    poolclass=QueuePool,
                    pool_size=pool_size,
                    max_overflow=overflow,
                    pool_timeout=1 if pool_timeout is None else pool_timeout,
                )
        else:
            kwargs.update(pool_size=pool_size, max_overflow=overflow, pool_pre_ping=True, pool_recycle=1800)
            if pool_timeout is not None:
                kwargs["pool_timeout"] = pool_timeout
        self.url = url
        self.engine: Engine = create_engine(url, **kwargs)
        #: The lock wait this deployment agreed to, so a per-call bound can only ever lower it (§11).
        self.busy_timeout_ms = int(sqlite_busy_timeout_ms)
        from ..services.storage_crypto import cipher_for_dialect

        cipher_for_dialect(self.engine.dialect)
        if self.engine.dialect.name == "sqlite":

            @event.listens_for(self.engine, "connect")
            def _sqlite_pragmas(dbapi_connection, _record):  # pragma: no cover - driver callback
                cursor = dbapi_connection.cursor()
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.execute(f"PRAGMA busy_timeout={int(sqlite_busy_timeout_ms)}")
                cursor.close()

        self.session_factory = sessionmaker(bind=self.engine, expire_on_commit=False, future=True)

    @contextmanager
    def session(self, tenant_id: str | None = None) -> Iterator[Session]:
        """One unit of work. Tenant scoping is applied by the repositories; on PostgreSQL the
        transaction-local `app.tenant_id` setting additionally feeds row-level-security policies."""
        session = self.session_factory()
        try:
            if tenant_id and self.is_postgres:
                session.execute(text("SELECT set_config('app.tenant_id', :tenant, true)"), {"tenant": tenant_id})
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    @property
    def is_postgres(self) -> bool:
        return self.engine.dialect.name == "postgresql"

    def create_schema(self) -> None:
        from . import models  # noqa: F401  (register mappers)

        Base.metadata.create_all(self.engine)

    @contextmanager
    def advisory_lock(self, name: str) -> Iterator[None]:
        """Hold one named lock across connections so two processes cannot both run DDL (§13.6, AC-25).

        PostgreSQL serialises the holders. SQLite has one writer anyway, so the lock is skipped there
        rather than emulated. No timeout is set: this is held once per deployment, before any traffic, and
        a DDL that sticks should surface as a stuck job instead of a half-applied structure.
        """
        if self.engine.dialect.name != "postgresql":
            yield
            return
        key = _lock_key(name)
        with self.engine.connect() as connection:
            connection.execute(text("SELECT pg_advisory_lock(:key)"), {"key": key})
            try:
                yield
            finally:
                connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": key})
                connection.commit()

    def dispose(self) -> None:
        self.engine.dispose()


_database: Database | None = None


def get_database() -> Database:
    global _database
    if _database is None:
        settings = get_settings()
        settings.ensure_dirs()
        _database = Database(settings.resolved_database_url(), pool_size=settings.db_pool_size)
    return _database


def set_database(database: Database) -> None:
    global _database
    _database = database
