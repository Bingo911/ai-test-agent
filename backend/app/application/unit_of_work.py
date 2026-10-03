"""One business transaction with exactly one committer (§9.3).

`Database.session()` commits when its block exits, which is correct for the older single-purpose routes
and wrong for a shared case: an atomic command has to hold reservation, business writes, Outbox and
audit inside one transaction that commits only after every piece succeeded. So the unit of work opens
its own `Session` - never the auto-committing wrapper - and everything it hands to the business core
is expected to flush and return, never to commit.

The write path also takes the database's own serialisation seriously rather than pretending a process
lock is enough: `BEGIN IMMEDIATE` on the SQLite development dialect, a transaction-level advisory lock on
PostgreSQL, and statement and lock deadlines that are the smaller of the caller's remaining budget and the
ceiling §11 puts on one statement.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from types import TracebackType
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import OperationalError, SQLAlchemyError
from sqlalchemy.orm import Session

from ..config import Settings
from ..db.base import Database
from ..domain.enums import Permission
from ..domain.errors import ApiError, ErrorCode
from ..observability import get_logger
from .context import CallContext, Entrypoint
from .identity import principal_only, resolve_subject

log = get_logger(__name__)

#: Deadlines are passed to the database as milliseconds, and a rounding-error wait is not a bug worth
#: an error: anything below this is clamped so `SET LOCAL` never receives `0`, which would mean "no
#: timeout" on PostgreSQL rather than "no waiting".
_MIN_DEADLINE_MS = 1

#: What one statement and one lock wait may cost even when the caller could still wait longer: 5 s and
#: 2 s of §11's connection contract. The budget decides when a call is over; these decide that a single
#: query never holds a connection and an execution slot for the whole of a 15 s response.
_MAX_STATEMENT_MS = 5_000
_MAX_LOCK_WAIT_MS = 2_000

#: The advisory lock key is derived from a digest, never from Python's `hash()`: the built-in string
#: hash is salted per process, so two workers would take two different locks for one command (§9.3.2).
_LOCK_DOMAIN = 20260728


@dataclass
class UnitOfWork:
    """The session, the caller and the commit, in one object that owns all three.

    `write=True` is how a caller says this transaction will change something. On the SQLite development
    dialect that takes the write lock with the transaction rather than at first touch, so a read-only
    page of results never queues behind a writer it has nothing to do with.
    """

    database: Database
    call: CallContext
    write: bool = False
    session: Session | None = None
    connection: Connection | None = None
    _entered: bool = False
    #: Set by an atomic command that answered from an earlier record instead of executing (§15). Only this
    #: object sees both halves of a write, so it is where the caller learns whether it committed anything.
    replayed: bool = False

    def __enter__(self) -> UnitOfWork:
        if self._entered:
            raise RuntimeError("a unit of work is one transaction; build a second one instead")
        self._entered = True
        connection = self.database.engine.connect()
        try:
            self.connection = connection
            if self.write and not self.database.is_postgres:
                self._open_sqlite_write(connection)
            self.session = Session(bind=connection, expire_on_commit=False)
            self.session.begin()
            self._configure()
        except BaseException:
            self._wind_down(rollback=True)
            raise
        return self

    def _open_sqlite_write(self, connection: Connection) -> None:
        """Take the development dialect's write lock when the transaction opens, not at its first write.

        A deferred transaction does not touch the writer lock until a statement asks for it, so two
        commands could open together, each read the row it intends to change, and only then discover
        that one of them cannot write - after the loser has spent its budget on reads whose answer it no
        longer holds. `BEGIN IMMEDIATE` moves that decision to the moment of opening, which is the only
        point at which `COMMAND_BUSY` honestly means "nothing was decided, retry with the same key"
        (§9.3.6), and the closest this single-writer dialect comes to §9.3.2's advisory lock. It is still
        not that lock: a writer whose snapshot has been overwritten fails rather than waiting and
        replaying, which is why §9.3.2 refuses to treat SQLite as a substitute for testing PostgreSQL.

        Both statements go to the driver rather than through the SQLAlchemy connection, because an
        executed statement would open the ORM's own transaction first and the wait being bounded is the
        lock acquisition itself. The bound only ever *lowers* the pool's busy timeout: a caller with a
        long budget must not turn a lock wait into a long hold on a connection and an execution slot.
        """
        remaining = self.call.remaining_seconds()
        driver_error = self.database.engine.dialect.loaded_dbapi.Error
        cursor = connection.connection.cursor()
        try:
            if remaining is not None:
                wait_ms = min(self.database.busy_timeout_ms, round(remaining * 1000))
                cursor.execute(f"PRAGMA busy_timeout={max(_MIN_DEADLINE_MS, wait_ms)}")
            cursor.execute("BEGIN IMMEDIATE")
        except driver_error as orig:
            # Nothing of SQLAlchemy's ever saw this statement, so its error translation has to be applied
            # here or the busy refusal below would never fire and a lock wait would surface as a 500.
            raise OperationalError("BEGIN IMMEDIATE", {}, orig) from orig
        finally:
            cursor.close()

    def _configure(self) -> None:
        if not self.database.is_postgres:
            return
        session = self.scope
        if not self.write:
            # Said to the database, not only to the code: a transaction that could write holds locks
            # nobody asked for, and `write=False` is a caller's promise that it will not.
            session.execute(text("SET TRANSACTION READ ONLY"))
        # Transaction-local, so a pooled connection never carries one tenant into the next call.
        if self.call.tenant_id:
            session.execute(text("SELECT set_config('app.tenant_id', :tenant, true)"), {"tenant": self.call.tenant_id})
        remaining = self.call.remaining_seconds()
        if remaining is None:
            return
        # A statement that cannot finish inside the caller's own budget must not be allowed to occupy a
        # connection past it; the client retries with the same key instead (§9.3.6). Neither ceiling is
        # tradeable for a bigger one, so the smaller of the two is what reaches the database.
        session.execute(text(f"SET LOCAL statement_timeout = {self.call.bounded_ms(_MAX_STATEMENT_MS)}"))
        session.execute(text(f"SET LOCAL lock_timeout = {self.call.bounded_ms(_MAX_LOCK_WAIT_MS)}"))

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        if not self._entered:
            return
        self._wind_down(rollback=exc_type is not None)

    def _wind_down(self, *, rollback: bool) -> None:
        session, connection = self.session, self.connection
        self.session, self.connection = None, None
        try:
            if session is not None:
                if rollback:
                    session.rollback()
                else:
                    session.commit()
        finally:
            if session is not None:
                session.close()
            if connection is not None:
                # The session was bound to this connection explicitly, so nothing else will put it back.
                connection.close()

    @property
    def scope(self) -> Session:
        """The session the business core must use; there is no second one to fall back to."""
        if self.session is None:
            raise RuntimeError("this unit of work is not open")
        return self.session

    def flush(self) -> None:
        self.scope.flush()

    def lock_command(self, *, route: str, key: str) -> None:
        """Serialise one logical command across processes before reading any mutable state (§9.3.2).

        Only PostgreSQL has advisory locks; on SQLite the immediate write lock already serialises the
        whole transaction, which is why the development dialect is not a substitute for a deployment
        test of this path.
        """
        if not self.database.is_postgres:
            return
        digest = _lock_digest(self.call.tenant_id, self.call.actor_id, route, key)
        self.scope.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": digest})

    def audit(
        self,
        *,
        operation: str,
        resource_type: str,
        resource_id: str | None = None,
        project_id: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        """Append to the audit trail inside this transaction, so a rolled-back command erases it too."""
        from ..repositories.platform import AuditRepository

        AuditRepository(self.scope, self.call.tenant_id).append(
            operation=operation,
            resource_type=resource_type,
            resource_id=resource_id,
            actor_id=self.call.actor_id,
            project_id=project_id,
            request_id=self.call.request_id,
            detail=detail or {},
        )

    def require(self, permission: Permission, project_id: str | None = None) -> None:
        self.call.identity.require(permission, project_id)


#: The two waits a bounded command can lose: the write lock was held too long, or the database cut the
#: statement off. Both mean nothing was decided, which is what makes them retryable with the same key.
_BUSY_MARKERS = ("database is locked", "database table is locked", "lock timeout", "statement timeout", "deadlock")


@contextmanager
def command_transaction(database: Database, call: CallContext) -> Iterator[UnitOfWork]:
    """One write command: the single committer, plus the error shape a lost wait produces (§9.3.6)."""
    uow = UnitOfWork(database=database, call=call, write=True)
    try:
        with uow:
            yield uow
    except SQLAlchemyError as exc:
        if _is_busy_wait(exc):
            raise ApiError(
                ErrorCode.COMMAND_BUSY,
                "Another command is holding this resource; retry with the same Idempotency-Key",
                details={"reason": type(exc.orig).__name__ if exc.orig is not None else "database_wait"},
            ) from exc
        raise


def _is_busy_wait(exc: SQLAlchemyError) -> bool:
    haystack = f"{exc.__class__.__name__} {exc.orig}".lower()
    return any(marker in haystack for marker in _BUSY_MARKERS)


@contextmanager
def read_transaction(
    database: Database,
    settings: Settings,
    *,
    issuer: str,
    subject: str,
    request_id: str,
    tenant_hint: str | None = None,
    scopes: tuple[str, ...] = (),
    deadline: float | None = None,
    entrypoint: Entrypoint = "mcp",
    select_tenant: bool = True,
    record_audit: bool = False,
) -> Iterator[UnitOfWork]:
    """One bounded, read-only transaction for a caller who has proved *who* they are (§5.1, §11).

    The identity is resolved first, in a transaction that is closed before the page's own opens. Two
    live transactions per call would need two connections per execution slot, and the MCP pool is
    deliberately the same size as the slot count - borrowing one more would have this path wait on its
    own pool instead of on the database. That costs nothing in authority: the page re-authorises the
    project and re-reads the policy inside the session this yields, so a membership revoked in between
    does not ride through on the identity snapshot (§11: every page re-checks tenant, project and
    policy, and this is where it does).

    `select_tenant=False` is the discovery case: the caller has not chosen a tenant yet, so the unit of
    work carries an empty one and only the caller's own rows are readable.

    `record_audit=True` is the one exception to `write=False`, and it is a narrow one: a read whose
    answer carries policy-gated content must have its access audit committed *before* the bytes are
    sent (§11). Only the audit row is written, the transaction still stops at the caller's deadline, and
    a caller that ends up emitting no content simply does not insert the row.
    """
    with database.session() as session:
        if select_tenant:
            identity = resolve_subject(session, issuer=issuer, subject=subject, tenant_hint=tenant_hint)
        else:
            identity = principal_only(session, issuer=issuer, subject=subject)
    uow = UnitOfWork(
        database=database,
        call=CallContext(
            identity=identity,
            request_id=request_id,
            settings=settings,
            entrypoint=entrypoint,
            scopes=scopes,
            deadline=deadline,
        ),
        write=record_audit,
    )
    with uow:
        yield uow


@contextmanager
def write_transaction(
    database: Database,
    settings: Settings,
    *,
    issuer: str,
    subject: str,
    request_id: str,
    tenant_id: str,
    scopes: tuple[str, ...] = (),
    deadline: float | None = None,
    entrypoint: Entrypoint = "mcp",
) -> Iterator[UnitOfWork]:
    """One atomic write command for a caller who has proved *who* they are (§9.3).

    The identity is resolved in a session that closes before the command's own opens, for the same
    reason a read does it: the pool is the size of the slot count, and a command that held two live
    transactions would wait on its own pool instead of on the database. Authority is not weakened by
    the two steps - the command re-reads the project and its permission inside the transaction it
    commits in, and re-reads them again after any lock wait (§9.3.1).

    Unlike a read, `tenant_id` is not optional. A created row belongs to a tenant, so a command run
    against an empty one would write a resource no member can read, and §6.2 refuses the "caller's
    first tenant" default for a write. `resolve_subject` is what enforces that the named tenant is one
    the subject belongs to; naming it is the adapter's job, and an adapter with no tenant to name
    refuses the call instead of reaching here.

    The unit of work this yields is the one an idempotent command is meant to be handed: the command
    reserves its key, writes its rows and flushes, and nothing commits until the whole block succeeds.
    """
    with database.session() as session:
        identity = resolve_subject(session, issuer=issuer, subject=subject, tenant_hint=tenant_id)
    call = CallContext(
        identity=identity,
        request_id=request_id,
        settings=settings,
        entrypoint=entrypoint,
        scopes=scopes,
        deadline=deadline,
    )
    with command_transaction(database, call) as uow:
        yield uow


def _lock_digest(tenant_id: str, actor_id: str, route: str, key: str) -> int:
    material = "\n".join((str(_LOCK_DOMAIN), tenant_id, actor_id, route, key)).encode("utf-8")
    # `pg_advisory_xact_lock` takes a signed bigint, so the digest is folded into that range. A
    # collision only makes two commands wait on each other; it never merges their digests.
    unsigned = int.from_bytes(hashlib.sha256(material).digest()[:8], "big")
    return unsigned - (1 << 63) if unsigned >= (1 << 63) else unsigned
