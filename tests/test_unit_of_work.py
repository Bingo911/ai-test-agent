"""§9.3, §11: the transaction boundaries a caller's budget has to survive.

Three promises here are invisible from the wire. A SQLite write transaction either takes the database's
write lock when it opens or when it first writes, and the difference only shows as a second command that
has already paid for its reads before it loses. A PostgreSQL transaction's `SET LOCAL` values only show
up as a query that outlives the caller who asked for it. Both are properties of the transaction itself,
so both are driven here rather than through an adapter that cannot see them.

The read cases matter as much as the write ones. A lock that every transaction took would make the
development dialect serialise reads behind an unrelated writer, which is the opposite of what §11 asks
for; `write=False` is a caller's promise, and these are the tests that collect it.
"""

from __future__ import annotations

import time
from typing import Any

import pytest
from backend.app.application.context import CallContext
from backend.app.application.unit_of_work import UnitOfWork, command_transaction
from backend.app.domain.errors import ApiError, ErrorCode
from backend.app.domain.rbac import Identity
from sqlalchemy import text


@pytest.fixture
def seeded(database, session, workspace) -> dict[str, str]:
    """The development workspace, committed, so a second connection can see the rows it names."""
    session.commit()
    return workspace


def call(settings: Any, tenant_id: str, *, budget_seconds: float | None = None) -> CallContext:
    """A caller with the shape a real one has, and a deadline measured from this instant."""
    return CallContext(
        identity=Identity(user_id="u-lock", tenant_id=tenant_id),
        request_id="req-uow",
        settings=settings,
        deadline=None if budget_seconds is None else time.monotonic() + budget_seconds,
    )


def pragma(uow: UnitOfWork, name: str) -> int:
    """What the database itself says this connection is currently configured to wait."""
    return int(uow.scope.execute(text(f"PRAGMA {name}")).scalar())


# --------------------------------------------------------------------------------------
# when the SQLite write lock is taken
# --------------------------------------------------------------------------------------


def test_a_write_transaction_holds_the_sqlite_write_lock_before_it_writes(database, settings, seeded) -> None:
    """§11: 写 UoW 显式使用 BEGIN IMMEDIATE 序列化.

    A deferred transaction leaves the writer lock untouched until the first statement, so two commands
    could both open, both read the state they intend to change, and only then discover that one of them
    cannot write - after the loser has spent its budget on reads whose answer it no longer holds. Taking
    the lock at open is what makes the refusal mean "nothing was decided, retry with the same key"
    (§9.3.6), and it is the only way the single-writer dialect serialises the way §9.3.2's advisory lock
    does on PostgreSQL.
    """
    holder = UnitOfWork(database=database, call=call(settings, seeded["tenant_id"], budget_seconds=5.0), write=True)
    contender = call(settings, seeded["tenant_id"], budget_seconds=0.2)
    # The discriminating detail: nothing has been flushed. The lock is held by opening, not by writing.
    with holder, pytest.raises(ApiError) as refused, command_transaction(database, contender):
        pass
    assert refused.value.code is ErrorCode.COMMAND_BUSY, refused.value.details
    # And the wait is a wait, not a wedge: releasing the holder lets the next command in.
    with command_transaction(database, call(settings, seeded["tenant_id"], budget_seconds=2.0)) as late:
        assert late.scope is not None


def test_a_read_transaction_never_blocks_a_writer(database, settings, seeded) -> None:
    """The other half: `write=False` must not take a lock, or a page of results queues behind nothing."""
    reader = UnitOfWork(database=database, call=call(settings, seeded["tenant_id"], budget_seconds=5.0), write=False)
    with reader, command_transaction(database, call(settings, seeded["tenant_id"], budget_seconds=2.0)) as writer:
        writer.scope.execute(text("SELECT 1"))


def test_the_lock_wait_is_bounded_by_the_call_and_never_raised_above_the_deployment(
    database, settings, seeded
) -> None:
    """§11: the driver's busy timeout is a ceiling the deployment chose, and the caller's budget is inside it.

    The pool sets one when it opens a connection. A caller with 15 s left must not turn that into a 15 s
    lock wait - an admission slot and a pooled connection would sit occupied long after the point where
    retrying is the better answer - so the per-call value only ever lowers it. A caller with 250 ms left
    gets 250 ms, because waiting past one's own deadline is not waiting, it is hanging.
    """
    configured = int(database.busy_timeout_ms)
    roomy = UnitOfWork(database=database, call=call(settings, seeded["tenant_id"], budget_seconds=15.0), write=True)
    with roomy:
        assert pragma(roomy, "busy_timeout") == configured
    tight = UnitOfWork(database=database, call=call(settings, seeded["tenant_id"], budget_seconds=0.25), write=True)
    with tight:
        expected = round(tight.call.remaining_seconds() * 1000)
        assert expected - 20 <= pragma(tight, "busy_timeout") <= expected


def test_a_deadline_less_writer_still_serialises_and_keeps_the_pools_wait(database, settings, seeded) -> None:
    """REST commands carry no MCP budget, and that must not cost them the serialisation or add a wait.

    Taking the lock is what makes two writers honest; it has nothing to do with how long the caller may
    wait for it. A deadline-less unit of work therefore still blocks the next writer, and still gets the
    wait its deployment configured rather than one invented for it here.
    """
    plain = UnitOfWork(database=database, call=call(settings, seeded["tenant_id"]), write=True)
    contender = call(settings, seeded["tenant_id"], budget_seconds=0.2)
    with plain, pytest.raises(ApiError) as refused, command_transaction(database, contender):
        assert pragma(plain, "busy_timeout") == int(database.busy_timeout_ms)
    assert refused.value.code is ErrorCode.COMMAND_BUSY, refused.value.details


# --------------------------------------------------------------------------------------
# what the transaction tells the database its budget is
# --------------------------------------------------------------------------------------


class _SpySession:
    """The statements a PostgreSQL transaction would send, without a PostgreSQL to send them to."""

    def __init__(self) -> None:
        self.statements: list[tuple[str, Any]] = []

    def execute(self, statement: Any, parameters: Any = None) -> None:
        self.statements.append((" ".join(str(statement).split()), parameters))

    def texts(self) -> list[str]:
        return [text for text, _parameters in self.statements]

    def value_of(self, keyword: str) -> int:
        """The number a `SET LOCAL … = n` assigned, which is the whole content of these clauses."""
        matches = [int(text.split("=")[1]) for text in self.texts() if keyword in text and "=" in text]
        assert len(matches) == 1, self.texts()
        return matches[0]


class _Postgres:
    """The database as seen by the one branch that is dialect-dependent."""

    is_postgres = True
    busy_timeout_ms = 1000
    url = "postgresql+psycopg://db.test/aita"


def configured(settings: Any, *, write: bool, budget_seconds: float | None, tenant_id: str = "t-1") -> _SpySession:
    """One transaction's configuration pass, with its statements recorded instead of executed."""
    spy = _SpySession()
    uow = UnitOfWork(
        database=_Postgres(),  # type: ignore[arg-type]
        call=call(settings, tenant_id, budget_seconds=budget_seconds),
        write=write,
        session=spy,  # type: ignore[arg-type]
    )
    uow._configure()
    return spy


def test_a_statement_is_cut_off_at_the_design_cap_when_the_call_could_keep_going(settings) -> None:
    """§11: SQL statement timeout 不超过剩余工具预算，单条默认上限 5 秒；锁等待最多 2 秒.

    Both halves of the sentence are load-bearing. "Not more than the remaining budget" without a cap
    leaves a statement holding a connection and an execution slot for a quarter minute; a cap without
    the budget lets a nearly-expired call run a fresh five seconds of SQL after its caller gave up.
    """
    spy = configured(settings, write=True, budget_seconds=15.0)
    assert spy.value_of("statement_timeout") == 5000
    assert spy.value_of("lock_timeout") == 2000


def test_a_nearly_expired_call_gets_the_smaller_number(settings) -> None:
    """The budget wins when it is the tighter of the two, which is the case the cap exists to bound."""
    spy = configured(settings, write=True, budget_seconds=0.4)
    expected = round(0.4 * 1000)
    for keyword in ("statement_timeout", "lock_timeout"):
        value = spy.value_of(keyword)
        assert expected - 20 <= value <= expected, keyword


def test_the_deadlines_are_transaction_local_so_a_pooled_connection_carries_none_over(settings) -> None:
    """§11: 用 SET LOCAL 或 set_config(..., true)，不用会跨连接复用遗留的 SET SESSION.

    A connection returns to a pool with its session settings intact, so `SET SESSION` would let one
    tenant's budget and one tenant's id become the next caller's - including a caller from another
    tenant. Every statement here has to say "this transaction".
    """
    spy = configured(settings, write=True, budget_seconds=9.0, tenant_id="t-9")
    statements = spy.texts()
    for statement in statements:
        assert "SESSION" not in statement, statement
    timeouts = [statement for statement in statements if "timeout" in statement]
    assert len(timeouts) == 2, timeouts
    assert all(statement.startswith("SET LOCAL") for statement in timeouts), timeouts
    scoped = [statement for statement in statements if "set_config" in statement]
    assert len(scoped) == 1, scoped
    assert scoped[0].endswith(", true)"), scoped


def test_a_read_only_transaction_says_so_and_a_write_one_does_not(settings) -> None:
    """`write=False` is answered at the database, not only in the code that respects it."""
    read = configured(settings, write=False, budget_seconds=9.0)
    assert "SET TRANSACTION READ ONLY" in read.texts()
    write = configured(settings, write=True, budget_seconds=9.0)
    assert "SET TRANSACTION READ ONLY" not in write.texts()


def test_the_tenant_context_is_the_callers_own_and_nothing_else(settings) -> None:
    """One pooled connection serves many tenants, so the value has to come from this call.

    Two callers are needed to test this: against a single tenant id, a hard-coded one looks exactly like
    the right one.
    """
    for tenant_id in ("t-42", "t-7"):
        spy = configured(settings, write=False, budget_seconds=9.0, tenant_id=tenant_id)
        scoped = [(text, parameters) for text, parameters in spy.statements if "set_config" in text]
        assert len(scoped) == 1, spy.texts()
        assert scoped[0][1] == {"tenant": tenant_id}, scoped
    discovery = configured(settings, write=False, budget_seconds=9.0, tenant_id="")
    assert [text for text in discovery.texts() if "set_config" in text] == [], discovery.texts()
