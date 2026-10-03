"""M1 / §11: MCP admission - the two-EVAL contract, the local slots, and bounded cleanup (§11, AC-43, AC-44).

These are unit tests on purpose. A live Redis would verify the Lua text, and that belongs to the M4
drills; what has to be proved here is the *shape* of the admission path, which a real server cannot see:
which commands are sent and in what order, what a refusal is allowed to say, how much of the call's
budget each command may take, and how many tasks and queue entries a saturated process is allowed to
create. The fake therefore records the script each EVAL carried and implements nothing else - so a
`ping`, an `EVALSHA` or a silent retry would fail as an `AttributeError` rather than passing quietly.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import pytest
from backend.app.config import Settings
from backend.app.mcp.auth import ALL_SCOPES, VerifiedPrincipal
from backend.app.mcp.callcontext import (
    CALL_STARTED_STATE_KEY,
    GLOBAL_SLOT_STATE_KEY,
    PRINCIPAL_STATE_KEY,
    REQUEST_ID_STATE_KEY,
    TENANT_HINT_STATE_KEY,
)
from backend.app.mcp.errors import AdapterCode, NextAction, ToolFailure
from backend.app.mcp.limits import (
    BUCKET_SCRIPT,
    CLEANUP_WORKERS,
    LEASE_SCRIPT,
    RELEASE_SCRIPT,
    RENEW_MAX_PARALLEL,
    RENEW_SCRIPT,
    Budget,
    McpLimiter,
    RedisAdmitter,
    subject_key_of,
)
from backend.app.mcp.server import McpServices, McpToolGateMiddleware
from backend.app.mcp.transport import MCP_PATH, McpAdmissionMiddleware
from starlette.requests import Request

RATE = "rate"
LEASE = "lease"
RENEW = "renew"
RELEASE = "release"

_SCRIPT_KINDS = {BUCKET_SCRIPT: RATE, LEASE_SCRIPT: LEASE, RENEW_SCRIPT: RENEW, RELEASE_SCRIPT: RELEASE}
#: The replies a healthy store gives: allowed by the bucket, registered, alive, removed.
_DEFAULT: dict[str, Any] = {RATE: [1, 0], LEASE: 1, RENEW: 1, RELEASE: 1}


class _Reply:
    """What one recorded EVAL should do: a result, an optional delay, or an injected fault.

    `result=None` means "the healthy default for this script", so a test that only cares about timing
    does not have to restate the reply.
    """

    def __init__(self, result: Any = None, *, delay: float = 0.0, error: bool = False) -> None:
        self.result = result
        self.delay = delay
        self.error = error


class FakeRedis:
    """A recording stand-in with exactly one method, because admission has exactly one command.

    `eval` is the whole surface §11 allows. Anything else - `ping`, `evalsha`, `script_load`, or a retry
    of a command that already failed - raises, which is the point: the contract is "two EVALs and
    nothing else", and a fake that answered politely would hide a third round trip.
    """

    def __init__(self, **replies: _Reply) -> None:
        self.replies: dict[str, _Reply] = dict(replies)
        self.calls: list[dict[str, Any]] = []
        self.inflight = 0
        self.max_inflight = 0

    def kinds(self) -> list[str]:
        return [str(call["kind"]) for call in self.calls]

    async def eval(self, script: str, numkeys: int, *keys_and_args: Any) -> Any:
        try:
            kind = _SCRIPT_KINDS[script]
        except KeyError as exc:
            raise AssertionError("the limiter sent a script this build does not know") from exc
        self.calls.append({"kind": kind, "keys": list(keys_and_args[:numkeys]), "args": list(keys_and_args[numkeys:])})
        reply = self.replies.get(kind, _Reply())
        self.inflight += 1
        self.max_inflight = max(self.max_inflight, self.inflight)
        try:
            if reply.delay:
                await asyncio.sleep(reply.delay)
            if reply.error:
                raise ConnectionError("the admission store is down")
            return _DEFAULT[kind] if reply.result is None else reply.result
        finally:
            self.inflight -= 1


def _settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "app_env": "test",
        "mcp_enabled": True,
        "redis_url": "redis://127.0.0.1:6379/0",
        "log_level": "WARNING",
    }
    values.update(overrides)
    return Settings(**values)


def _redis_limiter(fake: FakeRedis, **overrides: Any) -> McpLimiter:
    settings = _settings(**overrides)
    return McpLimiter(settings, admitter=RedisAdmitter(settings, client=fake))


def _local_limiter(**overrides: Any) -> McpLimiter:
    return McpLimiter(_settings(redis_url="", **overrides))


def _admit(limiter: McpLimiter, call_id: str, *, subject: str | None = None, tenant: str | None = None):
    return limiter.admit(
        subject_key=subject_key_of("local-dev", subject or call_id),
        tenant_id=tenant,
        call_id=call_id,
    )


def _hold(limiter: McpLimiter, call_id: str, **kwargs: Any):
    """Take both local slots the way the transport and the gate do, without reaching Redis."""
    slot = limiter.open_global()
    admission = _admit(limiter, call_id, **kwargs)
    slot.hand_to(admission)
    return admission


async def _admitted(limiter: McpLimiter, call_id: str, **kwargs: Any):
    """A call all the way through §11's order, so a test can assert what a live admission looks like."""
    admission = _hold(limiter, call_id, **kwargs)
    await limiter.admit_async(admission)
    await limiter.register_lease(admission)
    return admission


# --------------------------------------------------------------------------------------
# AC-44: the two EVALs, their codes, and the cumulative network budget
# --------------------------------------------------------------------------------------


async def test_admission_spends_exactly_two_evals_and_nothing_else():
    fake = FakeRedis()
    limiter = _redis_limiter(fake)
    admission = await _admitted(limiter, "call_1", tenant="tenant-a")

    assert fake.kinds() == [RATE, LEASE]
    bucket, lease = fake.calls
    assert bucket["keys"] == [f"mcp:bucket:{admission.subject_key}", "mcp:bucket:tenant:tenant-a"]
    # rate, burst, now_ms, ttl: the script is given the numbers, it does not read a configuration source.
    assert bucket["args"][:2] == [limiter.settings.mcp_rate_limit_per_minute, limiter.settings.mcp_rate_limit_burst]
    assert lease["keys"] == [f"mcp:lease:{admission.subject_key}"]
    assert lease["args"][3] == "call_1"
    assert admission.lease_registered is True
    # The lease is in the renewal worklist only because the store confirmed it.
    assert list(limiter.live) == ["call_1"]
    await limiter.close()


async def test_a_rate_refusal_stops_before_the_lease_is_written():
    fake = FakeRedis(rate=_Reply([0, 300]))
    limiter = _redis_limiter(fake)
    admission = _hold(limiter, "call_1")

    with pytest.raises(ToolFailure) as refused:
        await limiter.admit_async(admission)

    failure = refused.value
    assert failure.code == AdapterCode.RATE_LIMITED.value
    assert (failure.retryable, failure.retry_after_ms, failure.next_action) == (
        True,
        300,
        NextAction.retry_same_key_or_query,
    )
    # Spent quota is a quota event, not a dependency event, and only the bucket command was sent.
    assert fake.kinds() == [RATE]
    assert limiter.rate_refusals == 1
    assert admission.lease_registered is False
    assert limiter.live == {}
    await limiter.close()


async def test_an_unconfirmed_lease_never_becomes_database_work():
    fake = FakeRedis(lease=_Reply(0))
    limiter = _redis_limiter(fake)
    admission = _hold(limiter, "call_1")
    await limiter.admit_async(admission)

    with pytest.raises(ToolFailure) as refused:
        await limiter.register_lease(admission)

    assert refused.value.code == AdapterCode.COMMAND_BUSY.value
    assert refused.value.next_action is NextAction.retry_same_key_or_query
    # A lease the store did not confirm is not in-flight work, so it must not be renewed or released.
    assert admission.lease_registered is False
    assert limiter.live == {}
    assert fake.kinds() == [RATE, LEASE]
    await limiter.close()


async def test_a_store_fault_is_a_dependency_refusal_not_a_rate_limit():
    fake = FakeRedis(rate=_Reply(error=True))
    limiter = _redis_limiter(fake)
    admission = _admit(limiter, "call_1")

    with pytest.raises(ToolFailure) as refused:
        await limiter.admit_async(admission)

    failure = refused.value
    assert failure.code == AdapterCode.DEPENDENCY_UNAVAILABLE.value
    assert failure.retryable is True
    # Fail closed, and say which dependency broke - without repeating what the driver said about it.
    assert "ConnectionError" in failure.message
    assert limiter.rate_refusals == 0
    await limiter.close()


async def test_the_two_admission_commands_share_one_cumulative_budget():
    fake = FakeRedis(rate=_Reply(delay=0.2), lease=_Reply(delay=5.0))
    limiter = _redis_limiter(fake, mcp_redis_admission_timeout_seconds=0.3)
    admission = _admit(limiter, "call_1")

    started = time.monotonic()
    await limiter.admit_async(admission)
    # The first command drew on the budget; the second gets what is left, not a fresh timeout.
    assert admission.budget.left == pytest.approx(0.1, abs=0.06)

    with pytest.raises(ToolFailure) as refused:
        await limiter.register_lease(admission)
    elapsed = time.monotonic() - started

    assert refused.value.code == AdapterCode.DEPENDENCY_UNAVAILABLE.value
    # Cut off by the ~0.1 s that was left, so the 0.5 s per-command ceiling cannot have been granted
    # again: a reset-per-step budget would have taken this call to ~0.7 s before it failed.
    assert elapsed < 0.45
    assert fake.kinds() == [RATE, LEASE]
    await limiter.close()


async def test_a_spent_budget_issues_no_command_at_all():
    fake = FakeRedis()
    limiter = _redis_limiter(fake)
    admission = await _admitted(limiter, "call_1")
    admission.budget.left = 0.0

    with pytest.raises(ToolFailure) as refused:
        await limiter.admit_async(admission)

    assert refused.value.code == AdapterCode.DEPENDENCY_UNAVAILABLE.value
    assert fake.kinds() == [RATE, LEASE]
    await limiter.close()


async def test_the_tool_deadline_bounds_the_admission_budget_not_the_other_way_round():
    limiter = _redis_limiter(FakeRedis())

    assert _admit(limiter, "call_1").budget.left == limiter.settings.mcp_redis_admission_timeout_seconds
    assert limiter.admit(subject_key="k", tenant_id=None, call_id="c", remaining_seconds=0.2).budget.left == 0.2
    # An already-expired call is the deadline plane's to refuse, so the budget stays as configured.
    assert limiter.admit(subject_key="k", tenant_id=None, call_id="c", remaining_seconds=-1).budget.left == 1.5


async def test_a_single_command_never_exceeds_the_per_command_ceiling():
    assert RedisAdmitter(_settings(), client=FakeRedis()).command_timeout == 0.5
    assert RedisAdmitter(_settings(mcp_redis_admission_timeout_seconds=0.2), client=FakeRedis()).command_timeout == 0.2
    assert Budget(left=0.2).wait_seconds(0.5) == 0.2


async def test_an_exhausted_connection_pool_refuses_instead_of_queueing():
    fake = FakeRedis()
    limiter = _redis_limiter(fake)
    admitter = limiter.admitter
    assert admitter is not None
    for _ in range(8):
        # Saturating the pool is the case under test, and `locked()` is how the limiter sees it.
        await admitter._permits.acquire()

    with pytest.raises(ToolFailure) as refused:
        await limiter.register_lease(_admit(limiter, "call_1"))

    # Pool exhaustion is this process being busy, not the store being down (§11).
    assert refused.value.code == AdapterCode.COMMAND_BUSY.value
    assert fake.kinds() == []
    await limiter.close()


# --------------------------------------------------------------------------------------
# AC-43: the local global and subject slots
# --------------------------------------------------------------------------------------


async def test_the_global_slot_is_shared_and_the_excess_is_refused_immediately():
    limiter = _local_limiter(mcp_max_admission_inflight=3)
    slots = [limiter.open_global() for _ in range(3)]
    assert limiter.held == 3

    started = time.monotonic()
    with pytest.raises(ToolFailure) as refused:
        limiter.open_global()

    assert time.monotonic() - started < 0.05
    assert refused.value.code == AdapterCode.COMMAND_BUSY.value
    assert limiter.busy_refusals == 1
    for slot in slots:
        slot.finish()
    assert limiter.held == 0


async def test_one_subject_cannot_occupy_the_process_across_tenants():
    limiter = _local_limiter(mcp_max_admission_per_user=2)
    first = _admit(limiter, "same", subject="dev-engineer", tenant="tenant-a")
    _admit(limiter, "same-2", subject="dev-engineer", tenant="tenant-b")

    with pytest.raises(ToolFailure) as refused:
        _admit(limiter, "same-3", subject="dev-engineer", tenant="tenant-c")
    assert refused.value.code == AdapterCode.COMMAND_BUSY.value

    # Switching the tenant selector buys nothing: the subject cap is merged across tenants, and the
    # other subject is still admissible - fairness improves, it is not a promise of strict ordering.
    other = _admit(limiter, "other", subject="someone-else")
    assert limiter.local.active_subjects == 2

    first.drop()
    assert limiter.local.acquire_subject(subject_key_of("local-dev", "dev-engineer")) is True
    other.drop()


async def test_a_subject_counter_exists_only_while_a_call_holds_it():
    limiter = _local_limiter(mcp_max_admission_per_user=2)
    peak = 0
    for index in range(50):
        # A distinct verified subject each time: the point is that the dictionary describes the calls
        # in flight, not every identity this process has ever been asked about.
        admission = _admit(limiter, f"c{index}")
        peak = max(peak, limiter.local.active_subjects)
        admission.drop()

    # One at a time across 50 different subjects, and empty at the end: no unbounded history (§11).
    assert peak == 1
    assert limiter.local.active_subjects == 0


async def test_a_request_does_not_free_a_slot_its_database_thread_still_holds():
    limiter = _local_limiter()
    admission = _hold(limiter, "c1")
    admission.retain()

    # The handler returns while the thread runs: the transport unwinds, and the count must not move.
    if admission.global_slot is not None:
        admission.global_slot.finish()
    admission.drop()
    assert limiter.held == 1
    assert limiter.local.active_subjects == 1

    admission.drop()
    assert limiter.held == 0
    assert limiter.local.active_subjects == 0


async def test_an_unstarted_limiter_leaves_no_worklist_behind():
    fake = FakeRedis()
    limiter = _redis_limiter(fake)
    admission = await _admitted(limiter, "c1")

    # No cleanup worker exists, so nothing will ever pop this: the call's own unwinding has to.
    assert list(limiter.live) == ["c1"]
    admission.drop()
    assert limiter.live == {}
    assert fake.kinds() == [RATE, LEASE]


# --------------------------------------------------------------------------------------
# the gate and the transport, where the slots are actually taken
# --------------------------------------------------------------------------------------


class _Context:
    """A stand-in for the SDK's server context, carrying the state the transport would have written."""

    def __init__(self, limiter: McpLimiter, *, tenant_hint: str | None = None, with_slot: bool = True) -> None:
        state: dict[str, Any] = {
            PRINCIPAL_STATE_KEY: VerifiedPrincipal("local-dev", "dev-engineer", ALL_SCOPES),
            REQUEST_ID_STATE_KEY: "req_gate",
            CALL_STARTED_STATE_KEY: time.monotonic(),
        }
        if with_slot:
            state[GLOBAL_SLOT_STATE_KEY] = limiter.open_global()
        if tenant_hint:
            state[TENANT_HINT_STATE_KEY] = tenant_hint
        self.request = Request({"type": "http", "method": "POST", "path": MCP_PATH, "headers": [], "state": state})
        self.method = "tools/call"
        self.request_id: str | None = "1"
        self.params: dict[str, Any] = {"name": "aita_get_context", "arguments": {}}


def _gate(services: McpServices) -> McpToolGateMiddleware:
    from .mcp_live import tool_specs

    return McpToolGateMiddleware(services, tool_specs(services))


async def test_a_call_without_a_global_slot_is_refused_rather_than_uncounted():
    services = McpServices(_settings(redis_url=""))
    try:
        reached: list[str] = []

        async def call_next(_ctx: Any) -> str:
            reached.append("handler")
            return "reached"

        result = await _gate(services)(_Context(services.limiter, with_slot=False), call_next)
        assert reached == []
        # Admitting an uncounted call would make the process bound a fiction, so this is a refusal.
        assert result.structured_content["error"]["code"] == AdapterCode.COMMAND_BUSY.value
    finally:
        await services.close()


async def test_the_gate_and_the_executor_together_take_and_return_every_slot():
    fake = FakeRedis()
    services = McpServices(_settings(), limiter=_redis_limiter(fake))
    services.limiter.start()
    try:
        held_during_handler: list[int] = []

        async def call_next(_ctx: Any) -> str:
            # The handler body is where the execution slot and the lease are taken (§11 order).
            held_during_handler.append(services.limiter.held)
            return await services.run_blocking(lambda: "done")

        assert await _gate(services)(_Context(services.limiter, tenant_hint="tenant-a"), call_next) == "done"
        assert held_during_handler == [1]
        assert fake.kinds() == [RATE, LEASE]

        await asyncio.sleep(0.05)
        # The release is a cleanup worker's job, by internal call id, after the thread completed.
        assert fake.kinds() == [RATE, LEASE, RELEASE]
        assert fake.calls[-1]["keys"] == [f"mcp:lease:{subject_key_of('local-dev', 'dev-engineer')}"]
        assert services.limiter.held == 0
        assert services.limiter.local.active_subjects == 0
        assert services.limiter.live == {}
    finally:
        await services.close()


class _Inner:
    """The next ASGI layer: it records that it was reached and answers with an empty JSON body."""

    def __init__(self) -> None:
        self.reached = 0

    async def __call__(self, scope: dict[str, Any], _receive: Any, send: Any) -> None:
        self.reached += 1
        # The slot this layer took has to be visible to the gate through the same scope state.
        assert GLOBAL_SLOT_STATE_KEY in scope["state"]
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"{}"})


class _Send:
    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []

    async def __call__(self, message: dict[str, Any]) -> None:
        self.messages.append(message)

    @property
    def status(self) -> int:
        return int(self.messages[0]["status"])

    @property
    def headers(self) -> dict[bytes, bytes]:
        return {key.lower(): value for key, value in self.messages[0]["headers"]}

    @property
    def body(self) -> dict[str, Any]:
        return json.loads(self.messages[1]["body"])


def _scope(request_id: str) -> dict[str, Any]:
    return {"type": "http", "method": "POST", "path": MCP_PATH, "headers": [], "state": {"request_id": request_id}}


async def _receive() -> dict[str, Any]:
    return {"type": "http.request", "body": b"", "more_body": False}


async def test_a_saturated_process_refuses_at_the_transport_with_a_503():
    limiter = _local_limiter(mcp_max_admission_inflight=1)
    held = limiter.open_global()
    middleware = McpAdmissionMiddleware(_Inner(), limiter=limiter)

    sent = _Send()
    await middleware(_scope("req_2"), _receive, sent)

    assert sent.status == 503
    assert sent.headers[b"retry-after"] == b"1"
    assert sent.headers[b"x-request-id"] == b"req_2"
    # A busy process is refused before the SDK sees the message, under the static busy code (§5.3).
    assert sent.body["error"]["code"] == AdapterCode.COMMAND_BUSY.value
    assert limiter.busy_refusals == 1

    held.finish()
    assert limiter.held == 0
    await limiter.close()


async def test_an_unverified_request_holds_its_slot_only_for_the_request():
    limiter = _local_limiter(mcp_max_admission_inflight=2)
    inner = _Inner()
    middleware = McpAdmissionMiddleware(inner, limiter=limiter)

    sent = _Send()
    await middleware(_scope("req_1"), _receive, sent)

    assert sent.status == 200
    assert inner.reached == 1
    # A handshake or a refusal is not a command: nothing may keep a global slot after the request ends.
    assert limiter.held == 0
    await limiter.close()


# --------------------------------------------------------------------------------------
# AC-44: bounded cleanup and the single renewal loop
# --------------------------------------------------------------------------------------


async def test_the_release_has_its_own_budget_after_admission_spent_its():
    fake = FakeRedis()
    limiter = _redis_limiter(fake)
    admission = _admit(limiter, "c1")
    admission.budget.left = 0.0
    admission.lease_registered = True

    await limiter._release_one(admission)

    # §11: a completed call's cleanup is never skipped because admission ran out of network time.
    assert fake.kinds() == [RELEASE]
    assert limiter.live == {}


async def test_the_queue_is_bounded_and_its_overflow_is_counted_not_stacked():
    limiter = _redis_limiter(FakeRedis(), mcp_max_inflight_total=2)
    limiter.start()
    assert len(limiter._cleanup_workers) == CLEANUP_WORKERS

    accepted = 0
    for index in range(8):
        admission = _admit(limiter, f"c{index}")
        admission.lease_registered = True
        limiter.live[admission.call_id] = admission
        # No await inside this loop on purpose: the workers only get the event loop when this frame
        # yields, so what fills up is the queue's own bound - twice the total, and no more.
        accepted += int(limiter.submit_release(admission))
        admission.drop()

    assert (accepted, limiter.dropped_cleanups) == (4, 4)
    # Overflow is a recorded cleanup failure, not an unbounded task and not a lost local slot.
    assert limiter.local.active_subjects == 0
    await limiter.close()
    assert limiter.live == {}


async def test_shutdown_drains_the_cleanup_queue_for_at_most_one_second():
    fake = FakeRedis(release=_Reply(delay=0.9))
    limiter = _redis_limiter(fake, mcp_max_inflight_total=2)
    limiter.start()
    for index in range(4):
        admission = _admit(limiter, f"c{index}")
        admission.lease_registered = True
        limiter.submit_release(admission)

    started = time.monotonic()
    await limiter.close()
    elapsed = time.monotonic() - started

    # Each release is capped at 0.5 s and the whole drain at 1 s; the rest is left to the lease TTL.
    assert 0.9 < elapsed < 1.4
    assert fake.kinds().count(RELEASE) <= 2
    assert limiter._cleanup is None


async def test_renewal_scans_at_most_the_total_leases_two_at_a_time():
    fake = FakeRedis(renew=_Reply(delay=0.05))
    limiter = _redis_limiter(fake, mcp_max_inflight_total=4)
    for index in range(6):
        admission = _admit(limiter, f"c{index}")
        admission.lease_registered = True
        admission.retain()
        limiter.live[admission.call_id] = admission

    renewed = await limiter.renew_once()

    # One loop, bounded work: at most `TOTAL` leases per round, at most two network operations at once.
    assert renewed == 4
    assert fake.kinds().count(RENEW) == 4
    assert fake.max_inflight <= RENEW_MAX_PARALLEL
    assert limiter.lease_lost_count == 0
    await limiter.close()


async def test_a_lost_lease_is_recorded_and_never_buys_a_free_local_slot():
    fake = FakeRedis(renew=_Reply(0))
    limiter = _redis_limiter(fake, mcp_max_inflight_total=2)
    admission = _admit(limiter, "c1")
    admission.lease_registered = True
    admission.retain()
    limiter.live["c1"] = admission
    held_before = limiter.held

    assert await limiter.renew_once() == 0

    assert admission.lease_lost is True
    assert admission.renewals == 0
    assert limiter.lease_lost_count == 1
    # The thread is still running, so the per-process bound holds whatever Redis now believes (§11).
    assert limiter.held == held_before
    assert limiter.local.active_subjects == 1
    await limiter.close()


async def test_renewal_leaves_admissions_that_already_finished_alone():
    fake = FakeRedis()
    limiter = _redis_limiter(fake)
    admission = _admit(limiter, "c1")
    admission._references = 0
    limiter.live["c1"] = admission

    assert await limiter.renew_once() == 0
    assert fake.kinds() == []
    await limiter.close()


async def test_the_renewal_loop_is_one_task_for_the_process_not_one_per_lease():
    limiter = _redis_limiter(FakeRedis())
    before = len(asyncio.all_tasks())

    limiter.start()
    limiter.start()

    # Two workers plus one renewer, exactly once: a task per lease is the leak §11 names explicitly.
    assert len(asyncio.all_tasks()) - before == CLEANUP_WORKERS + 1
    assert limiter._renewer is not None
    await limiter.close()
    assert limiter._renewer is None
