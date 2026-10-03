"""Admission for MCP calls: local global/subject slots, the Redis bucket and lease, bounded cleanup
(§11).

The order is the one §11 fixes and it is not negotiable, because each step exists to bound the cost of
the one behind it:

    global local slot -> authentication -> subject local slot -> rate bucket -> execution slot
    -> in-flight lease -> database executor

A caller that is refused therefore pays only for what the platform had already committed to it: the
global and subject slots are plain counters taken immediately or refused, and only a call that holds
them reaches Redis. The lease is the opposite case - it is taken *after* the execution slot, because
until a thread exists there is nothing for a cross-replica lease to describe - and a lease the platform
could not confirm must never turn into database work.

Everything here is per app instance: two processes with different `Settings` have different limits and
never share a counter, which is what makes the numbers in §11 mean something.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from dataclasses import dataclass, field
from typing import Any

from ..config import Settings
from ..observability import get_logger
from .errors import AdapterCode, NextAction, ToolFailure

log = get_logger(__name__)

#: §11 - a single Redis command may not run longer than this, whatever the call's own budget says.
COMMAND_TIMEOUT_SECONDS = 0.5
#: §11 - the limiter's own connection pool, and the local semaphore that refuses instead of queueing.
POOL_MAX_CONNECTIONS = 8
#: §11 - an idle bucket must be able to refill to `burst` before it is rebuilt as full.
BUCKET_MIN_TTL_SECONDS = 60.0
#: §11 - a lease outlives its own renewals by this much; it is not a quota and never a permission.
LEASE_TTL_SECONDS = 60.0
CLEANUP_WORKERS = 2
#: §11 - shutdown drains the cleanup queue for at most this long and leaves the rest to the TTL.
CLEANUP_DRAIN_SECONDS = 1.0
RENEW_INTERVAL_SECONDS = 10.0
RENEW_ROUND_DEADLINE_SECONDS = 1.0
RENEW_MAX_PARALLEL = 2


def subject_key_of(issuer: str, subject: str) -> str:
    """A stable key for the *verified* identity: never a client-supplied actor id, never raw PII (§11)."""
    digest = hashlib.sha256(f"{issuer}\n{subject}".encode()).hexdigest()
    return f"mcp:{digest[:32]}"


BUCKET_SCRIPT = """
-- One subject bucket and, when a tenant is selected, that tenant's bucket, checked and taken
-- atomically: a caller must not be able to consume one and be refused by the other.
local rate = tonumber(ARGV[1])
local burst = tonumber(ARGV[2])
local now = tonumber(ARGV[3])
local ttl = tonumber(ARGV[4])
local function refill(key)
  local s = redis.call('HMGET', key, 'tokens', 'ts')
  local tokens = tonumber(s[1])
  if tokens == nil then tokens = burst end
  local ts = tonumber(s[2])
  if ts == nil or ts > now then ts = now end
  return math.min(burst, tokens + (now - ts) * rate / 60000)
end
local subject = refill(KEYS[1])
local shared = subject
if KEYS[2] ~= '' then shared = refill(KEYS[2]) end
if subject < 1 or shared < 1 then
  local short = math.max(1 - subject, 1 - shared)
  return {0, math.ceil(short * 60000 / rate)}
end
redis.call('HSET', KEYS[1], 'tokens', subject - 1, 'ts', now)
redis.call('PEXPIRE', KEYS[1], ttl)
if KEYS[2] ~= '' then
  redis.call('HSET', KEYS[2], 'tokens', shared - 1, 'ts', now)
  redis.call('PEXPIRE', KEYS[2], ttl)
end
return {1, 0}
"""

LEASE_SCRIPT = """
-- Register one in-flight call for this subject, pruning leases whose owner stopped renewing.
local now = tonumber(ARGV[1])
local ttl = tonumber(ARGV[2])
local cap = tonumber(ARGV[3])
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now)
if redis.call('ZCARD', KEYS[1]) >= cap then return 0 end
redis.call('ZADD', KEYS[1], now + ttl, ARGV[4])
redis.call('PEXPIRE', KEYS[1], ttl)
return 1
"""

RENEW_SCRIPT = """
-- Extend a lease that still exists. `XX` is the point: an expired lease is gone, and resurrecting it
-- from a late renewal would hand a finished call a second life.
local now = tonumber(ARGV[1])
local ttl = tonumber(ARGV[2])
local updated = redis.call('ZADD', KEYS[1], 'XX', now + ttl, ARGV[3])
if updated == 0 then return 0 end
redis.call('PEXPIRE', KEYS[1], ttl)
return 1
"""

RELEASE_SCRIPT = """
-- Idempotent, and by member id only: a releaser that arrived late must not drop someone else's lease.
return redis.call('ZREM', KEYS[1], ARGV[1])
"""


class LocalAdmission:
    """The two in-process counters that decide whether a call is admitted at all (§11).

    Both are taken immediately or refused: waiting here would just move a bounded queue from the
    executor into the event loop. Subject entries exist only for callers that actually hold a slot, so
    the dictionary is bounded by the global slot count and cannot grow into a history of every subject
    the process has ever seen.

    The two levels are separate because they are taken at different moments: the global slot belongs to
    the request from arrival (before identity is trustworthy), the subject slot only after the token
    has been verified.
    """

    def __init__(self, *, global_limit: int, subject_limit: int) -> None:
        self.global_limit = max(1, int(global_limit))
        self.subject_limit = max(1, int(subject_limit))
        self._subjects: dict[str, int] = {}
        self._held = 0

    @property
    def held(self) -> int:
        return self._held

    @property
    def active_subjects(self) -> int:
        return len(self._subjects)

    def acquire_global(self) -> bool:
        if self._held >= self.global_limit:
            return False
        self._held += 1
        return True

    def release_global(self) -> None:
        self._held = max(0, self._held - 1)

    def acquire_subject(self, subject_key: str) -> bool:
        count = self._subjects.get(subject_key, 0)
        if count >= self.subject_limit:
            return False
        self._subjects[subject_key] = count + 1
        return True

    def release_subject(self, subject_key: str) -> None:
        count = self._subjects.get(subject_key, 0)
        if count <= 1:
            # Deleting at zero is what keeps `active_subjects` a measure of work, not of history.
            self._subjects.pop(subject_key, None)
        else:
            self._subjects[subject_key] = count - 1


@dataclass
class Budget:
    """A cumulative network allowance that operations draw from, never one they hand back (§11).

    The admission budget is the whole point of this type: two EVALs share 1.5 seconds, so a slow first
    command must shorten the second one instead of each getting a fresh timeout. The tool deadline is
    separate and is never restarted by admission work.
    """

    left: float
    started_at: float = field(default_factory=time.monotonic)

    def wait_seconds(self, cap: float) -> float:
        """The per-operation wait: the smaller of the single-command cap and what this budget has left."""
        return min(cap, self.left)

    def spend(self) -> None:
        self.left = max(0.0, self.left - (time.monotonic() - self.started_at))
        self.started_at = time.monotonic()


class RedisAdmitter:
    """The bucket and lease pair, on `redis.asyncio` and never on the event loop's own thread (§11).

    Admission spends exactly two EVALs and nothing else: no `ping`, no `EVALSHA`/`NOSCRIPT` dance, no
    automatic retry. Each is bounded by the smaller of the per-command cap and whatever is left of the
    call's cumulative network budget, so a slow Redis cannot extend the call beyond the budget the
    operator configured.
    """

    def __init__(self, settings: Settings, *, client: Any | None = None) -> None:
        self.rate = max(1, int(settings.mcp_rate_limit_per_minute))
        self.burst = max(1, int(settings.mcp_rate_limit_burst))
        self.per_user = max(1, int(settings.mcp_max_inflight_per_user))
        self.command_timeout = min(COMMAND_TIMEOUT_SECONDS, settings.mcp_redis_admission_timeout_seconds)
        self.network_budget = settings.mcp_redis_admission_timeout_seconds
        self.lease_ttl_ms = int(LEASE_TTL_SECONDS * 1000)
        self.bucket_ttl_ms = int(max(BUCKET_MIN_TTL_SECONDS, 60.0 * self.burst / self.rate) * 1000)
        self._client = client if client is not None else self._build_client(settings)
        self._owns_client = client is None
        # Refusing is the point: redis-py's pool would otherwise wait for a connection indefinitely.
        self._permits = asyncio.Semaphore(POOL_MAX_CONNECTIONS)

    @staticmethod
    def _build_client(settings: Settings) -> Any:
        from redis.asyncio import Redis

        return Redis.from_url(
            settings.resolved_mcp_limiter_url,
            max_connections=POOL_MAX_CONNECTIONS,
            socket_connect_timeout=COMMAND_TIMEOUT_SECONDS,
            socket_timeout=COMMAND_TIMEOUT_SECONDS,
            decode_responses=True,
        )

    async def close(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    async def probe(self) -> bool:
        """One bounded PING for the readiness sampler; the request path never calls this (§13.5).

        Admission still spends exactly two EVALs and no ping - that rule bounds what a *caller* pays. This
        is a sample the probe takes from a cache every few seconds, so a Redis that is slow or down costs
        one bounded command and answers "unavailable" rather than "assume it is up".
        """
        if self._client is None or self._permits.locked():
            return False
        try:
            async with self._permits:
                return bool(await asyncio.wait_for(self._client.ping(), self.command_timeout))
        except Exception as exc:
            log.warning("mcp_limiter_probe_failed", extra={"context": {"error": type(exc).__name__}})
            return False

    async def _eval(self, script: str, keys: list[str], args: list[Any], budget: Budget) -> Any:
        """One EVAL, inside both the per-command cap and whatever the caller's budget still allows."""
        wait = budget.wait_seconds(self.command_timeout)
        if wait <= 0:
            raise _unavailable("the admission network budget was already spent")
        if self._permits.locked():
            # Pool exhausted: refuse now rather than queue behind eight connections.
            raise _busy("the admission connection pool is saturated")
        async with self._permits:
            try:
                result = await asyncio.wait_for(self._client.eval(script, len(keys), *keys, *args), wait)
            except asyncio.TimeoutError as exc:
                raise _unavailable("the admission store took too long") from exc
            except Exception as exc:
                raise _unavailable(f"the admission store refused the request: {type(exc).__name__}") from exc
            finally:
                budget.spend()
            return result

    async def take_rate(self, *, subject_key: str, tenant_id: str | None, budget: Budget) -> tuple[bool, int]:
        """Consume one token from the subject bucket and, if set, the selected tenant's bucket."""
        now_ms = int(time.time() * 1000)
        allowed, retry = await self._eval(
            BUCKET_SCRIPT,
            [f"mcp:bucket:{subject_key}", f"mcp:bucket:tenant:{tenant_id}" if tenant_id else ""],
            [self.rate, self.burst, now_ms, self.bucket_ttl_ms],
            budget,
        )
        return bool(int(allowed)), int(retry or 0)

    async def register_lease(self, *, subject_key: str, call_id: str, budget: Budget) -> bool:
        now_ms = int(time.time() * 1000)
        registered = await self._eval(
            LEASE_SCRIPT,
            [f"mcp:lease:{subject_key}"],
            [now_ms, self.lease_ttl_ms, self.per_user, call_id],
            budget,
        )
        return bool(int(registered))

    async def renew_lease(self, *, subject_key: str, call_id: str, budget: Budget) -> bool:
        now_ms = int(time.time() * 1000)
        alive = await self._eval(
            RENEW_SCRIPT,
            [f"mcp:lease:{subject_key}"],
            [now_ms, self.lease_ttl_ms, call_id],
            budget,
        )
        return bool(int(alive))

    async def release_lease(self, *, subject_key: str, call_id: str, budget: Budget) -> bool:
        removed = await self._eval(RELEASE_SCRIPT, [f"mcp:lease:{subject_key}"], [call_id], budget)
        return bool(int(removed))


@dataclass
class Admission:
    """One call's hold on its admission slots, shared with the database thread it starts (§11).

    The reference count is the whole design: the request drops its reference when the handler returns,
    but a thread that outlives a cancelled caller keeps the count above zero, so the global and subject
    slots are only reported as free once nothing is still running.
    """

    subject_key: str
    tenant_id: str | None
    call_id: str
    limiter: McpLimiter
    #: The call's own cumulative admission network budget, drawn on by the bucket and the lease.
    budget: Budget
    #: Set once the lease is confirmed in Redis; an unconfirmed lease must never reach the database.
    lease_registered: bool = False
    _references: int = 1
    _released: bool = False
    release_submitted: bool = False
    lease_lost: bool = False
    renewals: int = 0
    global_slot: GlobalSlot | None = None

    def retain(self) -> None:
        self._references += 1

    def drop(self) -> None:
        self._references -= 1
        if self._references <= 0:
            self.limiter._finish(self)

    @property
    def references(self) -> int:
        return self._references


class GlobalSlot:
    """The one global admission slot a request holds from arrival (§11).

    `hand_to` is the transfer of ownership to the admission, which happens the moment the subject slot
    is taken. Without it a request that started a database thread would free its slot when the handler
    returned, and the count would say the process had capacity while a thread was still committing.
    """

    def __init__(self, limiter: McpLimiter) -> None:
        self._limiter = limiter
        self._handed = False
        self._done = False

    def hand_to(self, admission: Admission) -> None:
        self._handed = True
        admission.global_slot = self

    def finish(self) -> None:
        """Called by the transport when the request unwinds: frees the slot unless an admission owns it."""
        if not self._handed:
            self.free()

    def free(self) -> None:
        if not self._done:
            self._done = True
            self._limiter.release_global()


@dataclass
class McpLimiter:
    """Everything one app instance needs to admit, lease and clean up its MCP calls (§11)."""

    settings: Settings
    admitter: RedisAdmitter | None = None
    local: LocalAdmission = field(init=False)
    live: dict[str, Admission] = field(default_factory=dict)
    _cleanup: asyncio.Queue | None = None
    _cleanup_workers: list[asyncio.Task] = field(default_factory=list)
    _renewer: asyncio.Task | None = None
    _closing: bool = False
    dropped_cleanups: int = 0
    busy_refusals: int = 0
    rate_refusals: int = 0
    lease_lost_count: int = 0

    def __post_init__(self) -> None:
        self.local = LocalAdmission(
            global_limit=self.settings.mcp_max_admission_inflight,
            subject_limit=self.settings.mcp_max_admission_per_user,
        )
        if self.admitter is None and self.settings.resolved_mcp_limiter_url:
            self.admitter = RedisAdmitter(self.settings)

    @property
    def redis_backed(self) -> bool:
        return self.admitter is not None

    @property
    def held(self) -> int:
        return self.local.held

    def open_global(self) -> GlobalSlot:
        """Take one global slot before authentication, or busy-reject (§11).

        Identity is not trusted yet, which is exactly why this step cannot be per subject: a caller
        forging a thousand `sub` values is still one caller for the purposes of this counter.
        """
        if self._closing or not self.local.acquire_global():
            self.busy_refusals += 1
            raise _busy("all MCP admission slots are in use")
        return GlobalSlot(self)

    def release_global(self) -> None:
        self.local.release_global()

    def admit(
        self,
        *,
        subject_key: str,
        tenant_id: str | None,
        call_id: str,
        remaining_seconds: float | None = None,
    ) -> Admission:
        """Take the subject slot for a verified caller; the rate bucket is spent by `admit_async`.

        `remaining_seconds` is the call's own tool deadline, and it clamps the network budget rather
        than the other way round: §11 bounds each admission operation by the smallest of the
        per-command cap, the unspent budget and the time left to the call, so admission is never the
        reason a response arrives late. A call that arrives with no time left is refused by the
        handler's deadline check, so an expired `remaining_seconds` leaves the budget alone instead of
        turning a missed deadline into a dependency failure.
        """
        if self._closing or not self.local.acquire_subject(subject_key):
            self.busy_refusals += 1
            raise _busy("this caller already has its admission allowance in flight")
        left = self.settings.mcp_redis_admission_timeout_seconds
        if remaining_seconds is not None and remaining_seconds > 0:
            left = min(left, remaining_seconds)
        return Admission(
            subject_key=subject_key,
            tenant_id=tenant_id,
            call_id=call_id,
            limiter=self,
            budget=Budget(left=left),
        )

    async def admit_async(self, admission: Admission) -> None:
        """Spend the rate bucket for a call that already holds both local slots (§11 order)."""
        if self.admitter is None:
            return
        allowed, retry_ms = await self.admitter.take_rate(
            subject_key=admission.subject_key,
            tenant_id=admission.tenant_id,
            budget=admission.budget,
        )
        if not allowed:
            self.rate_refusals += 1
            raise ToolFailure(
                AdapterCode.RATE_LIMITED,
                "This caller has exhausted its request rate",
                retryable=True,
                retry_after_ms=max(100, retry_ms),
                next_action=NextAction.retry_same_key_or_query,
            )

    async def register_lease(self, admission: Admission) -> None:
        """Confirm the in-flight lease after the execution slot is held (§11).

        An unconfirmed lease never becomes database work: with Redis down, the call fails here rather
        than adding load to a platform that has already lost its admission state.

        `live` is the renewal loop's worklist, so it only ever gains an entry the store has confirmed.
        Registering it before the command could also fail would leave a permanent entry describing a
        lease that does not exist, and a renewal of a non-existent lease can only ever report `lost`.
        """
        if self.admitter is None:
            admission.lease_registered = True
            return
        registered = await self.admitter.register_lease(
            subject_key=admission.subject_key,
            call_id=admission.call_id,
            budget=admission.budget,
        )
        if not registered:
            raise ToolFailure(
                AdapterCode.COMMAND_BUSY,
                "This caller already has its in-flight allowance",
                retryable=True,
                retry_after_ms=1000,
                next_action=NextAction.retry_same_key_or_query,
            )
        admission.lease_registered = True
        self.live[admission.call_id] = admission

    def submit_release(self, admission: Admission) -> bool:
        """Hand the lease release to the bounded cleanup workers (§11).

        The queue is bounded on purpose and its overflow is logged: a cleanup that never happened is
        recovered by the lease TTL, while an unbounded task per completion event is the leak.
        """
        if self._cleanup is None or admission.release_submitted:
            return False
        admission.release_submitted = True
        try:
            self._cleanup.put_nowait(admission)
            return True
        except asyncio.QueueFull:
            self.dropped_cleanups += 1
            log.warning(
                "mcp_cleanup_dropped",
                extra={"context": {"call_id": admission.call_id, "dropped": self.dropped_cleanups}},
            )
            return False

    def start(self) -> None:
        """Open the cleanup workers and the renewal loop; called from the app lifespan (§4.3)."""
        if self._cleanup is not None:
            return
        # §11: a queue of at most 2 x TOTAL pending ids, sized against the threads that can finish, so
        # a slow Redis overflows visibly instead of accumulating one task per completed call.
        bound = max(1, 2 * self.settings.mcp_max_inflight_total)
        self._cleanup = asyncio.Queue(maxsize=bound)
        for index in range(CLEANUP_WORKERS):
            self._cleanup_workers.append(asyncio.create_task(self._cleanup_loop(index), name=f"mcp-cleanup-{index}"))
        self._renewer = asyncio.create_task(self._renew_loop(), name="mcp-lease-renew")

    async def close(self) -> None:
        """Refuse new admissions, drain the cleanup queue briefly, then stop the loops (§4.3)."""
        self._closing = True
        stopped = [task for task in [self._renewer, *self._cleanup_workers] if task is not None]
        for task in stopped:
            task.cancel()
        # `return_exceptions` is what makes this a join rather than a failure: every one of these tasks
        # is expected to end as a CancelledError, and one of them raising must not abandon the rest.
        await asyncio.gather(*stopped, return_exceptions=True)
        self._renewer = None
        self._cleanup_workers.clear()
        queue = self._cleanup
        self._cleanup = None
        if queue is not None:
            # §11: the drain is capped at one second. Anything still queued is left to its TTL, because
            # waiting on a Redis that is down would turn a shutdown into an outage.
            try:
                await asyncio.wait_for(self._drain(queue), timeout=CLEANUP_DRAIN_SECONDS)
            except asyncio.TimeoutError:
                log.warning(
                    "mcp_cleanup_drain_incomplete",
                    extra={"context": {"pending": queue.qsize(), "dropped": self.dropped_cleanups}},
                )
        if self.admitter is not None:
            await self.admitter.close()

    async def _drain(self, queue: asyncio.Queue) -> None:
        while not queue.empty():
            await self._release_one(await queue.get())

    async def _cleanup_loop(self, _index: int) -> None:
        queue = self._cleanup
        if queue is None:
            return
        while True:
            admission = await queue.get()
            await self._release_one(admission)
            queue.task_done()

    async def _release_one(self, admission: Admission) -> None:
        """Release by internal call id under its own 0.5 s budget; failure never changes a result (§11)."""
        self.live.pop(admission.call_id, None)
        if self.admitter is None:
            return
        try:
            await self.admitter.release_lease(
                subject_key=admission.subject_key,
                call_id=admission.call_id,
                budget=Budget(left=self.settings.mcp_redis_cleanup_timeout_seconds),
            )
        except ToolFailure as failure:
            log.warning(
                "mcp_cleanup_failed",
                extra={"context": {"call_id": admission.call_id, "code": failure.code}},
            )

    async def _renew_loop(self) -> None:
        """One loop for the whole process: bounded work, never a task per lease (§11)."""
        while True:
            await asyncio.sleep(RENEW_INTERVAL_SECONDS)
            await self.renew_once()

    async def renew_once(self) -> int:
        """Renew at most `TOTAL` live leases, two at a time, inside one round deadline."""
        if self.admitter is None or not self.live:
            return 0
        selected = list(self.live.values())[: self.settings.mcp_max_inflight_total]
        live = [item for item in selected if item.references > 0]
        if not live:
            return 0
        deadline = time.monotonic() + RENEW_ROUND_DEADLINE_SECONDS
        semaphore = asyncio.Semaphore(RENEW_MAX_PARALLEL)

        async def renew(admission: Admission) -> bool:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            async with semaphore:
                try:
                    return await self.admitter.renew_lease(
                        subject_key=admission.subject_key,
                        call_id=admission.call_id,
                        budget=Budget(left=min(remaining, self.settings.mcp_redis_cleanup_timeout_seconds)),
                    )
                except ToolFailure:
                    return False

        outcomes = await asyncio.gather(*(renew(item) for item in live))
        renewed = 0
        for admission, alive in zip(live, outcomes, strict=True):
            if alive:
                admission.renewals += 1
                renewed += 1
            else:
                # A lost lease is recorded, never traded for a free local slot: the thread is still
                # running and the per-process bounds hold regardless of what Redis now thinks.
                admission.lease_lost = True
                self.lease_lost_count += 1
                log.warning("mcp_lease_lost", extra={"context": {"call_id": admission.call_id}})
        return renewed

    def _finish(self, admission: Admission) -> None:
        """Release both local slots, then hand the lease to cleanup - only once nothing is running."""
        if admission._released:
            return
        admission._released = True
        self.local.release_subject(admission.subject_key)
        if admission.global_slot is not None:
            admission.global_slot.free()
        if admission.lease_registered and not self.submit_release(admission):
            # No worker exists to take the release (or it already went): the entry is this call's to
            # drop, otherwise a limiter used without its lifespan would keep a worklist of finished
            # calls forever. Redis still recovers an uncleaned lease by TTL.
            self.live.pop(admission.call_id, None)


def _busy(message: str) -> ToolFailure:
    return ToolFailure(
        AdapterCode.COMMAND_BUSY,
        message,
        retryable=True,
        retry_after_ms=50,
        next_action=NextAction.retry_same_key_or_query,
    )


def _unavailable(message: str) -> ToolFailure:
    return ToolFailure(
        AdapterCode.DEPENDENCY_UNAVAILABLE,
        f"The admission store is unavailable: {message}",
        retryable=True,
        retry_after_ms=250,
        next_action=NextAction.retry_same_key_or_query,
    )
