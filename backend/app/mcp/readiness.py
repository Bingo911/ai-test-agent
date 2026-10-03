"""The MCP readiness probe and the bounded sampling behind it (§13.5, §15.4).

`/health` keeps its existing status-code contract and stays about the platform; this probe answers one
question for an internal load balancer: would a new MCP call be served, refused, or answered from a
half-initialised transport.

Two rules shape the implementation. A probe must not become load: it never fetches JWKS, never pings the
broker, and every dependency that costs a network round trip is answered from a bounded cached observation
taken by `ReadinessSampler`. And an observation that has aged out is not available - a dependency nobody has
checked recently is a dependency whose state this process does not know.
"""

from __future__ import annotations

import asyncio
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from ..config import Settings
from ..db.schema import current_drift
from ..observability import get_logger
from ..orchestrator.queue import EXECUTE_TASK, QUEUES_FOR_TASK
from ..repositories.outbox import OutboxRepository
from ..repositories.reservations import WorkerLeaseRepository

log = get_logger(__name__)

READINESS_PATH = "/api/v1/mcp/readiness"

#: §13.5 - the sampling contract, in seconds: a normal pass every 5, at most 1 second of network per
#: sample, and a failing dependency re-checked no sooner than every 30.
SAMPLE_INTERVAL_SECONDS = 5.0
SAMPLE_BUDGET_SECONDS = 1.0
SAMPLE_BACKOFF_CAP_SECONDS = 30.0
#: A positive answer stops being an answer after three missed passes.
OBSERVATION_TTL_SECONDS = 3 * SAMPLE_INTERVAL_SECONDS
#: An unpublished row whose own window has closed by this much means nothing is publishing, whether or not
#: anything ever failed. `mark_retry` backs off to at most 300s, so this sits under that as a floor.
DISPATCH_STALL_SECONDS = 60
#: How far back a successful publish still counts as evidence. It is a window, not a threshold: nothing
#: inside it means this process has no positive signal that the dispatcher is running, and §13.5 forbids
#: substituting "no backlog" for that signal.
DISPATCH_EVIDENCE_SECONDS = 300

#: Which queues a worker must have declared for each capability to be claimed. The routing table in
#: `orchestrator/queue.py` owns the names; deriving them here is what keeps the two from drifting.
EXECUTION_QUEUE = QUEUES_FOR_TASK[EXECUTE_TASK]
BACKGROUND_QUEUES = frozenset(set(QUEUES_FOR_TASK.values()) - {EXECUTION_QUEUE})

# Component names are a fixed, static vocabulary; a probe body must never carry a dependency's own error
# text, which can contain URLs, key ids or credentials.
TRANSPORT = "transport"
AUTH = "auth"
DATABASE = "database"
SCHEMA = "schema"
LIMITER = "limiter"

#: What the sampler caches, as distinct from the components above: one bounded database pass answers for
#: both the database and the schema, and one Redis command answers for the limiter.
BACKSTORE = "backstore"

READY = "ready"
UNAVAILABLE = "unavailable"
DISABLED = "disabled"
RUNNING = "running"
STOPPED = "stopped"
INITIALISED = "initialised"
UNINITIALISED = "uninitialised"
OK = "ok"
STALE = "stale"
DRIFT = "drift"
NOT_REQUIRED = "not_required"

#: The whole vocabulary of capability reasons. `NO_EVIDENCE` is not a synonym for "healthy": §13.5 forbids
#: reading an empty backlog, or a dependency nobody sampled, as proof that something works.
AVAILABLE = "available"
NO_LIVE_WORKER = "no_live_worker"
DISPATCH_FAILING = "dispatch_failing"
DISPATCH_STALLED = "dispatch_stalled"
NO_EVIDENCE = "no_evidence"
DATABASE_UNAVAILABLE = "database_unavailable"
SCHEMA_UNAVAILABLE = "schema_unavailable"
LIMITER_UNAVAILABLE = "limiter_unavailable"


@dataclass(frozen=True)
class Observation:
    """One dependency as the sampler last saw it, with the moment it saw it."""

    ok: bool
    value: Any = None
    taken_at: float = field(default_factory=time.monotonic)

    def usable(self, *, now: float, ttl: float = OBSERVATION_TTL_SECONDS) -> bool:
        return self.ok and (now - self.taken_at) < ttl


@dataclass(frozen=True)
class Backsample:
    """Everything the sampler reads out of the database in one bounded pass."""

    schema_ok: bool
    live_queues: frozenset[str]
    failing: int
    stalled: int
    published: int


class ReadinessSampler:
    """The one bounded health sample this process keeps (§13.5).

    It is a sample and nothing else: no second dispatcher, no reconciler, no business work. Its database
    half runs on its own single-worker thread, so a hung dependency occupies one probe thread and never an
    execution slot an MCP request is waiting for.
    """

    def __init__(self, settings: Settings, *, limiter: Any = None, database: Any = None) -> None:
        self.settings = settings
        self.limiter = limiter
        self.database = database
        self.observations: dict[str, Observation] = {}
        self.failures: dict[str, int] = {}
        self._ready_at: dict[str, float] = {}
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="mcp-probe")
        self._task: asyncio.Task | None = None
        self._closed = False

    # ------------------------------------------------------------------- lifetime

    async def start(self) -> None:
        """Take the first sample before anything can report ready, then keep the loop running."""
        if self._task is not None:
            return
        await self.sample_once()
        self._task = asyncio.create_task(self._loop(), name="mcp-readiness-sample")

    async def stop(self) -> None:
        self._closed = True
        task = self._task
        self._task = None
        if task is not None:
            task.cancel()
            # The sample loop is expected to end as a cancellation; one of them raising must not abandon
            # the other, and a probe whose thread outlived the cache has nothing left to write.
            await asyncio.gather(task, return_exceptions=True)
        self.observations.clear()
        self._pool.shutdown(wait=False, cancel_futures=True)

    async def _loop(self) -> None:
        while not self._closed:
            await asyncio.sleep(SAMPLE_INTERVAL_SECONDS)
            if self._closed:
                return
            try:
                await self.sample_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # A sampler allowed to die would leave the cache ageing toward "unknown" for every
                # dependency at once, which is a worse answer than reporting the one that failed.
                log.warning("mcp_readiness_sample_failed", extra={"context": {"error": type(exc).__name__}})

    # --------------------------------------------------------------------- sample

    async def sample_once(self) -> None:
        await self._sample_limiter()
        await self._sample_backstore()

    async def _sample_limiter(self) -> None:
        if not self._due(LIMITER):
            return
        admitter = getattr(self.limiter, "admitter", None)
        if admitter is None:
            # Only a development deployment can reach this: production MCP configuration is refused at
            # startup without a limiter Redis (§12.2), so "not required" is never a production answer.
            self._record(LIMITER, Observation(ok=True, value=NOT_REQUIRED))
            return
        available = await admitter.probe()
        self._record(LIMITER, Observation(ok=available, value=OK if available else UNAVAILABLE))

    async def _sample_backstore(self) -> None:
        if not self._due(BACKSTORE):
            return
        loop = asyncio.get_running_loop()
        try:
            sample = await asyncio.wait_for(
                loop.run_in_executor(self._pool, self._read_backstore), SAMPLE_BUDGET_SECONDS
            )
        except asyncio.TimeoutError:
            # The budget is the point: a database needing more than a second is not one this probe can
            # promise traffic to, and waiting longer would make the probe the slowest caller.
            self._record(BACKSTORE, Observation(ok=False, value=UNAVAILABLE))
            return
        except Exception as exc:
            log.warning("mcp_readiness_backstore_failed", extra={"context": {"error": type(exc).__name__}})
            self._record(BACKSTORE, Observation(ok=False, value=UNAVAILABLE))
            return
        self._record(BACKSTORE, Observation(ok=True, value=sample))

    def _read_backstore(self) -> Backsample:
        """One thread, one pass: the schema, the worker heartbeats and the dispatcher's own rows.

        There is no separate connectivity query. Every read here goes through the driver, so a database this
        process cannot reach raises from the first of them rather than reporting a shape problem, and one
        statement more would just be more of the load this sample is bounded against (§13.5).
        """
        with self.database.session() as session:
            schema_ok = current_drift(self.database).ok
            queues = WorkerLeaseRepository(session, "").live_queues(ttl_seconds=int(self.settings.lease_ttl_seconds))
            outbox = OutboxRepository(session, "")
            failing = outbox.failing_publishes()
            stalled = outbox.stalled_publishes(older_than_seconds=DISPATCH_STALL_SECONDS)
            published = outbox.recent_publishes(within_seconds=DISPATCH_EVIDENCE_SECONDS)
        return Backsample(
            schema_ok=schema_ok,
            live_queues=frozenset(queues),
            failing=failing,
            stalled=stalled,
            published=published,
        )

    def _due(self, target: str) -> bool:
        """Whether this target is due again: failures back off toward the cap, successes never do."""
        if self.failures.get(target, 0) == 0:
            return True
        return time.monotonic() >= self._ready_at.get(target, 0.0)

    def _record(self, target: str, observation: Observation) -> None:
        self.observations[target] = observation
        if observation.ok:
            self.failures[target] = 0
            self._ready_at.pop(target, None)
            return
        count = self.failures.get(target, 0) + 1
        self.failures[target] = count
        delay = min(SAMPLE_BACKOFF_CAP_SECONDS, SAMPLE_INTERVAL_SECONDS * (2 ** min(count, 5)))
        self._ready_at[target] = time.monotonic() + delay

    # ---------------------------------------------------------------------- reads

    def observation(self, target: str) -> Observation | None:
        return self.observations.get(target)

    def backstore(self) -> Backsample | None:
        observation = self.observations.get(BACKSTORE)
        if observation is None or not observation.usable(now=time.monotonic()):
            return None
        return observation.value


def component_state(app: FastAPI, settings: Settings) -> dict[str, str]:
    bundle = getattr(app.state, "mcp", None)
    if bundle is None:
        return {TRANSPORT: DISABLED, AUTH: DISABLED, DATABASE: DISABLED, SCHEMA: DISABLED, LIMITER: DISABLED}
    sampler = getattr(bundle, "sampler", None)
    return {
        TRANSPORT: RUNNING if bundle.running else STOPPED,
        AUTH: INITIALISED if _auth_initialised(settings, bundle) else UNINITIALISED,
        DATABASE: _database_state(sampler),
        SCHEMA: _schema_state(sampler),
        LIMITER: _limiter_state(sampler),
    }


def _database_state(sampler: Any) -> str:
    """Read from the sample, not from a query in the route: a probe that opens its own transaction on every
    hit is the load it was supposed to prevent, and it blocks the loop that is serving it (§13.5)."""
    observation = sampler.observation(BACKSTORE) if sampler is not None else None
    if observation is None:
        return STALE
    if not observation.ok:
        return UNAVAILABLE
    return OK if observation.usable(now=time.monotonic()) else STALE


def _schema_state(sampler: Any) -> str:
    """`drift` is a shape this build cannot run on; `stale` is the sampler having no current answer."""
    sample = sampler.backstore() if sampler is not None else None
    if sample is None:
        return STALE
    return OK if sample.schema_ok else DRIFT


def _limiter_state(sampler: Any) -> str:
    observation = sampler.observation(LIMITER) if sampler is not None else None
    if observation is None:
        return STALE
    if not observation.ok:
        return UNAVAILABLE
    if not observation.usable(now=time.monotonic()):
        return STALE
    return str(observation.value)


def _auth_initialised(settings: Settings, bundle: Any) -> bool:
    """Whether the auth component was built with what its mode requires, not whether a token works.

    A cached key set can be momentarily stale and a valid token can still be verified offline, so this is
    deliberately an initialisation check and never a statement about the next authentication. The probe does
    not fetch a key: that would turn every health check into load on the identity provider (§13.5).
    """
    authenticator = bundle.authenticator
    if settings.auth_mode != "oidc":
        return True
    return getattr(authenticator, "key_source", None) is not None and bool(settings.mcp_oidc_audience)


def is_ready(components: dict[str, str]) -> bool:
    """The five states §13.5 lists, and only them: capability signals never change the status code."""
    return (
        components[TRANSPORT] == RUNNING
        and components[AUTH] == INITIALISED
        and components[DATABASE] == OK
        and components[SCHEMA] == OK
        and components[LIMITER] in (OK, NOT_REQUIRED)
    )


def capability_state(components: dict[str, str], sampler: Any) -> tuple[dict[str, bool], dict[str, str]]:
    """Submission, execution and background capacity, each from evidence this process actually holds.

    Three independent answers, because they fail independently: a broker outage must not read as "cannot
    submit" (the row still commits), and a stopped worker must not read as "the API is down" (§13.5).
    """
    reasons: dict[str, str] = {}
    submission = components[DATABASE] == OK and components[SCHEMA] == OK and components[LIMITER] in (OK, NOT_REQUIRED)
    if components[DATABASE] != OK:
        reasons["submission"] = DATABASE_UNAVAILABLE
    elif components[SCHEMA] != OK:
        reasons["submission"] = SCHEMA_UNAVAILABLE
    elif components[LIMITER] not in (OK, NOT_REQUIRED):
        reasons["submission"] = LIMITER_UNAVAILABLE
    else:
        reasons["submission"] = AVAILABLE

    sample = sampler.backstore() if sampler is not None else None
    dispatch = _dispatch_reason(sample)
    live = sample.live_queues if sample is not None else frozenset()
    # `claimed` is the worker evidence on its own, never the combined answer: a queue with no live worker has
    # a definite reason, and reporting "no evidence" for it would hide the one thing the operator can fix.
    execution_claimed = EXECUTION_QUEUE in live
    background_claimed = bool(live & BACKGROUND_QUEUES)
    execution = dispatch == AVAILABLE and execution_claimed
    background = dispatch == AVAILABLE and background_claimed
    reasons["execution"] = _queue_reason(dispatch, execution_claimed)
    reasons["background"] = _queue_reason(dispatch, background_claimed)
    return (
        {
            "submission_available": submission,
            "execution_available": execution,
            "background_available": background,
        },
        reasons,
    )


def _dispatch_reason(sample: Backsample | None) -> str:
    if sample is None:
        return NO_EVIDENCE
    if sample.failing:
        return DISPATCH_FAILING
    if sample.stalled:
        return DISPATCH_STALLED
    if not sample.published:
        # Nothing has been published recently. "No backlog" is not evidence (§13.5): an idle system and a
        # dispatcher that stopped an hour ago look identical from the rows still waiting.
        return NO_EVIDENCE
    return AVAILABLE


def _queue_reason(dispatch: str, claimed: bool) -> str:
    """A missing worker is reported first: it is a definite negative, unlike the quality of the dispatch signal."""
    if not claimed:
        return NO_LIVE_WORKER
    return dispatch


def add_readiness_route(application: FastAPI, settings: Settings) -> None:
    """Register the probe only when MCP is enabled, so a disabled deployment 404s like any unknown path."""

    async def readiness() -> JSONResponse:
        components = component_state(application, settings)
        bundle = getattr(application.state, "mcp", None)
        sampler = getattr(bundle, "sampler", None) if bundle is not None else None
        capabilities, reasons = capability_state(components, sampler)
        ready = is_ready(components)
        payload = {
            "status": READY if ready else UNAVAILABLE,
            "components": components,
            "capabilities": capabilities,
            "capability_reasons": reasons,
        }
        return JSONResponse(payload, status_code=200 if ready else 503)

    application.add_api_route(
        READINESS_PATH, readiness, methods=["GET"], include_in_schema=False, name="mcp_readiness"
    )
