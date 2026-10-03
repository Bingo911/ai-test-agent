"""§13.5 / AC-24 / AC-34: what the readiness probe may claim, and on what evidence.

Every case here is about a distinction the design draws and an implementation easily loses:

* 200/503 is decided by five components and by nothing else - a stopped Worker or a dead broker must not
  make an API that can still persist a submission look broken;
* the three capability signals are separate answers, each from persisted state or from a bounded sample,
  and "no Outbox backlog" is explicitly not evidence that the broker works;
* a dependency nobody has measured recently is *unknown*, which is neither available nor down.

The probe must not become the load it is measuring: one bounded database pass and one Redis command per
sample, no JWKS fetch, no broker ping, and no query of its own in the request path.
"""

from __future__ import annotations

import asyncio
import time
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx2
import pytest
import yaml
from backend.app.config import Settings
from backend.app.db.base import Database, new_id, utcnow
from backend.app.db.models import QUEUES_KEY, Outbox, WorkerLease, WorkerPool
from backend.app.db.schema import current_drift
from backend.app.main import create_app
from backend.app.mcp import readiness
from backend.app.mcp.auth import JwksKeySource
from backend.app.mcp.readiness import (
    AUTH,
    BACKSTORE,
    DATABASE,
    DISPATCH_EVIDENCE_SECONDS,
    DISPATCH_FAILING,
    DISPATCH_STALLED,
    EXECUTION_QUEUE,
    LIMITER,
    NO_EVIDENCE,
    NO_LIVE_WORKER,
    OBSERVATION_TTL_SECONDS,
    READINESS_PATH,
    SAMPLE_BACKOFF_CAP_SECONDS,
    SAMPLE_INTERVAL_SECONDS,
    SCHEMA,
    TRANSPORT,
    UNAVAILABLE,
    Backsample,
    Observation,
    ReadinessSampler,
    capability_state,
    component_state,
    is_ready,
)
from backend.app.orchestrator.queue import QUEUES_FOR_TASK
from backend.app.repositories.outbox import OutboxRepository
from backend.app.repositories.reservations import WorkerLeaseRepository
from sqlalchemy import select, update
from starlette.testclient import TestClient

from .mcp_live import BASE, live_settings

REPO = Path(__file__).resolve().parents[1]

READY_COMPONENTS = {
    TRANSPORT: "running",
    AUTH: "initialised",
    DATABASE: "ok",
    SCHEMA: "ok",
    LIMITER: "ok",
}


def _components(**overrides: str) -> dict[str, str]:
    return {**READY_COMPONENTS, **overrides}


def _sample(**overrides: Any) -> Backsample:
    base: dict[str, Any] = {"schema_ok": True, "live_queues": frozenset(), "failing": 0, "stalled": 0, "published": 0}
    base.update(overrides)
    return Backsample(**base)


class _StubSampler:
    """The cache a real sampler would hold, filled in directly instead of waiting on its threads.

    `observation` and `backstore` mirror the real pair exactly, so a test that passes against this stub
    cannot be passing on a different reading of what an aged-out or failed observation means.
    """

    def __init__(
        self,
        *,
        sample: Backsample | None = None,
        limiter: Observation | None = None,
        backstore: Observation | None = None,
    ) -> None:
        if backstore is None and sample is not None:
            backstore = Observation(ok=True, value=sample)
        self._observations: dict[str, Observation | None] = {BACKSTORE: backstore, LIMITER: limiter}

    def observation(self, target: str) -> Observation | None:
        return self._observations.get(target)

    def backstore(self) -> Backsample | None:
        observation = self._observations.get(BACKSTORE)
        if observation is None or not observation.usable(now=time.monotonic()):
            return None
        return observation.value


class _ProbeClient:
    """A Redis double that answers PING and records anything else the probe was tempted to spend."""

    def __init__(self, *, reachable: bool = True, delay: float = 0.0) -> None:
        self.reachable = reachable
        self.delay = delay
        self.pings = 0
        self.evals = 0

    async def ping(self) -> bool:
        self.pings += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if not self.reachable:
            raise ConnectionError("redis is down")
        return True

    async def eval(self, *_args: Any) -> Any:
        # The admission scripts belong to the request path. A probe that spent one would be adding load and,
        # worse, reading back a bucket it had just written.
        self.evals += 1
        raise AssertionError("the readiness probe must never spend an admission command")

    async def aclose(self) -> None:
        return None


def _sampler(settings: Settings, *, client: _ProbeClient | None = None, database: Any = None) -> ReadinessSampler:
    if client is None:
        # No admitter at all: the development-shaped answer, `not_required`.
        limiter: Any = SimpleNamespace()
    else:
        # Deliberately an object with exactly one attribute: any other dependency call the sampler made
        # would fail here rather than quietly succeed against something real.
        limiter = SimpleNamespace(admitter=SimpleNamespace(probe=client.ping))
    return ReadinessSampler(settings, limiter=limiter, database=database)


def _counts(sampler: ReadinessSampler) -> tuple[int, int, int, frozenset[str]]:
    sample = sampler._read_backstore()
    return (sample.failing, sample.stalled, sample.published, sample.live_queues)


def _app(database: Any, tmp_path: Any, **overrides: Any) -> Any:
    return create_app(live_settings(database, tmp_path, **overrides))


# --------------------------------------------------------------------------------------
# the 200/503 decision: five components, and only five
# --------------------------------------------------------------------------------------


def test_every_component_has_to_answer_before_the_probe_does() -> None:
    assert is_ready(READY_COMPONENTS) is True
    # `not_required` is the development answer for the limiter, and it is allowed to pass (§12.2).
    assert is_ready(_components(LIMITER="not_required")) is True
    for key, value in (
        (TRANSPORT, "stopped"),
        (TRANSPORT, "disabled"),
        (AUTH, "uninitialised"),
        (DATABASE, UNAVAILABLE),
        (DATABASE, "stale"),
        (SCHEMA, "drift"),
        (SCHEMA, "stale"),
        (LIMITER, UNAVAILABLE),
        (LIMITER, "stale"),
    ):
        assert is_ready(_components(**{key: value})) is False, (key, value)


def test_a_broker_outage_never_makes_an_api_that_can_still_persist_unready() -> None:
    """§13.5 splits these on purpose: the row still commits, so the control entrance stays ready."""
    sampler = _StubSampler(sample=_sample(live_queues=frozenset({EXECUTION_QUEUE}), failing=7))
    assert is_ready(_components()) is True
    flags, reasons = capability_state(_components(), sampler)
    assert flags["submission_available"] is True
    assert flags["execution_available"] is False
    assert reasons["execution"] == DISPATCH_FAILING


def test_a_stopped_worker_changes_the_capability_and_not_the_status_code() -> None:
    components = _components()
    flags, reasons = capability_state(components, _StubSampler(sample=_sample(published=3)))
    assert is_ready(components) is True
    assert flags["execution_available"] is False
    assert reasons["execution"] == NO_LIVE_WORKER


# --------------------------------------------------------------------------------------
# the capability evidence, and what does not count as any
# --------------------------------------------------------------------------------------


def test_an_empty_outbox_is_not_evidence_that_the_broker_is_up() -> None:
    """The forbidden inference, in one case: no backlog is not a working dispatcher (§13.5)."""
    live = frozenset({EXECUTION_QUEUE, "compile", "analysis"})
    flags, reasons = capability_state(_components(), _StubSampler(sample=_sample(live_queues=live)))
    assert flags["execution_available"] is False
    assert flags["background_available"] is False
    assert reasons["execution"] == NO_EVIDENCE
    assert reasons["background"] == NO_EVIDENCE


def test_a_live_worker_and_a_publish_that_actually_happened_claim_a_capability() -> None:
    sample = _sample(live_queues=frozenset({EXECUTION_QUEUE}), published=1)
    flags, reasons = capability_state(_components(), _StubSampler(sample=sample))
    assert flags["execution_available"] is True
    assert flags["background_available"] is False
    assert reasons["execution"] == "available"
    # A queue nobody declared is never assumed covered.
    assert reasons["background"] == NO_LIVE_WORKER


def test_a_missing_worker_is_reported_before_the_quality_of_the_dispatch_signal() -> None:
    """Two negatives are not one reason: the Worker being gone is the actionable half."""
    sample = _sample(live_queues=frozenset(), failing=4, published=2)
    flags, reasons = capability_state(_components(), _StubSampler(sample=sample))
    assert flags["execution_available"] is False
    assert reasons["execution"] == NO_LIVE_WORKER


def test_a_dispatcher_that_stopped_leaves_stalled_rows_behind() -> None:
    """Nothing was ever attempted, so a failing-publish count alone would have called this healthy."""
    sample = _sample(live_queues=frozenset({EXECUTION_QUEUE}), stalled=2)
    flags, reasons = capability_state(_components(), _StubSampler(sample=sample))
    assert flags["execution_available"] is False
    assert reasons["execution"] == DISPATCH_STALLED


def test_submission_is_the_database_the_schema_and_the_limiter_never_a_worker() -> None:
    for key, expected in (
        (DATABASE, "database_unavailable"),
        (SCHEMA, "schema_unavailable"),
        (LIMITER, "limiter_unavailable"),
    ):
        flags, reasons = capability_state(_components(**{key: UNAVAILABLE}), _StubSampler())
        assert flags["submission_available"] is False, key
        assert reasons["submission"] == expected, key
        assert flags["execution_available"] is False, key


# --------------------------------------------------------------------------------------
# the bounded sample
# --------------------------------------------------------------------------------------


def test_an_observation_that_aged_out_is_not_availability() -> None:
    now = time.monotonic()
    assert Observation(ok=True, taken_at=now).usable(now=now) is True
    assert Observation(ok=True, taken_at=now - OBSERVATION_TTL_SECONDS - 1).usable(now=now) is False
    assert Observation(ok=False, taken_at=now).usable(now=now) is False


def test_an_aged_out_positive_answer_is_not_an_answer_at_all() -> None:
    """`ok` is what the sampler saw; `usable` is whether that is still an answer.

    A component cache holding a three-weeks-old success would otherwise report ready forever, so every
    read of an observation passes through the age check - the database, the schema and the limiter alike.
    """
    expired = Observation(ok=True, value="ok", taken_at=time.monotonic() - OBSERVATION_TTL_SECONDS - 1)
    sampler = _StubSampler(limiter=expired, backstore=expired)
    components = _components(**{DATABASE: "stale", SCHEMA: "stale", LIMITER: "stale"})
    assert readiness._database_state(sampler) == "stale"
    assert readiness._schema_state(sampler) == "stale"
    assert readiness._limiter_state(sampler) == "stale"
    assert is_ready(components) is False
    flags, reasons = capability_state(components, sampler)
    assert flags["submission_available"] is False
    assert reasons["submission"] == "database_unavailable"
    assert reasons["execution"] == NO_LIVE_WORKER


async def test_the_database_sample_is_cut_off_at_its_budget(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dependency needing more than the budget is not one this probe can promise traffic to."""
    monkeypatch.setattr(readiness, "SAMPLE_BUDGET_SECONDS", 0.01)
    sampler = _sampler(settings)

    def slow_read() -> Backsample:
        time.sleep(0.2)
        return _sample()

    sampler._read_backstore = slow_read  # type: ignore[method-assign]
    await sampler._sample_backstore()
    observation = sampler.observation(BACKSTORE)
    await sampler.stop()
    assert observation is not None
    assert observation.ok is False
    # `stop()` also clears the cache, so the answer had to be taken before the shutdown - and after it the
    # probe must say "unknown", never the last thing it knew while the process was healthy.
    assert sampler.observation(BACKSTORE) is None


async def test_a_failing_target_backs_off_and_a_healthy_one_never_does(settings: Settings) -> None:
    """正常 5 秒、失败退避、上限 30 秒 (§13.5): three numbers a bare `sleep` would silently drop."""
    sampler = _sampler(settings)
    assert sampler._due(LIMITER) is True
    delays: list[float] = []
    for _ in range(5):
        before = time.monotonic()
        sampler._record(LIMITER, Observation(ok=False))
        assert sampler._due(LIMITER) is False, "a target that just failed may not be retried immediately"
        delays.append(sampler._ready_at[LIMITER] - before)
    assert delays == sorted(delays), delays
    assert delays[0] == pytest.approx(2 * SAMPLE_INTERVAL_SECONDS, abs=0.5)
    assert max(delays) <= SAMPLE_BACKOFF_CAP_SECONDS + 0.5

    sampler._record(LIMITER, Observation(ok=True, value="ok"))
    assert sampler._due(LIMITER) is True
    assert LIMITER not in sampler._ready_at
    await sampler.stop()


async def test_the_probe_spends_one_command_per_dependency_and_nothing_else(
    settings: Settings, database: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No JWKS fetch, no admission command, no broker ping: a PING and one database read per pass."""
    for name in ("_fetch", "key_for"):
        monkeypatch.setattr(JwksKeySource, name, _forbidden, raising=True)
    client = _ProbeClient()
    sampler = _sampler(settings, client=client, database=database)
    await sampler.sample_once()
    await sampler.sample_once()
    pings = client.pings
    targets = set(sampler.observations)
    limiter_observation = sampler.observation(LIMITER)
    await sampler.stop()
    assert pings == 2
    assert targets == {LIMITER, BACKSTORE}
    assert limiter_observation is not None
    assert limiter_observation.ok is True
    assert limiter_observation.value == "ok"


def _forbidden(*_args: Any, **_kwargs: Any) -> Any:
    raise AssertionError("the readiness probe must never reach the identity provider")


async def test_the_probe_never_touches_the_queue_layer(
    settings: Settings, database: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`orchestrator.queue` is where a broker ping would come from, so it must stay uncalled."""
    import backend.app.orchestrator.queue as queue_module

    monkeypatch.setattr(queue_module, "redis_reachable", _forbidden)
    monkeypatch.setattr(queue_module, "get_queue", _forbidden)
    sampler = _sampler(settings, client=_ProbeClient(), database=database)
    await sampler.sample_once()
    sample = sampler.backstore()
    await sampler.stop()
    assert sample is not None
    assert sample.schema_ok is True


async def test_a_sample_never_publishes_or_claims_an_outbox_row(settings: Settings, database: Any) -> None:
    """This is a health sample, not a second dispatcher (§13.5): reading leaves the row untouched."""
    with database.session() as session:
        row = OutboxRepository(session, "t").enqueue(
            aggregate_id="a", event_type="execution.queued", payload={}, discriminator="d"
        )
        row_id = row.id
        session.commit()

    def state() -> tuple[int, Any, Any, Any]:
        with database.session() as session:
            fresh = session.get(Outbox, row_id)
            return (fresh.attempts, fresh.published_at, fresh.next_attempt_at, fresh.last_error)

    before = state()
    sampler = _sampler(settings, database=database)
    sample = sampler._read_backstore()
    await sampler.sample_once()
    assert state() == before
    assert (sample.failing, sample.stalled, sample.published) == (0, 0, 0)
    await sampler.stop()


def test_the_sample_reads_the_evidence_the_platform_already_persists(settings: Settings, database: Any) -> None:
    """The end of the chain: the probe answers from the rows and heartbeats real work leaves behind."""
    with database.session() as session:
        leases = WorkerLeaseRepository(session, "")
        leases.heartbeat(
            "worker-exec",
            pool_id="pool",
            capacity=2,
            active_count=0,
            capabilities={QUEUES_KEY: [EXECUTION_QUEUE]},
        )
        # A worker that never said which queues it serves.
        leases.heartbeat("worker-quiet", pool_id="pool", capacity=1, active_count=0, capabilities={})
        outbox = OutboxRepository(session, "")
        pending = outbox.enqueue(aggregate_id="a", event_type="e", payload={}, discriminator="pending")
        published = outbox.enqueue(aggregate_id="b", event_type="e", payload={}, discriminator="published")
        session.commit()
        outbox.mark_published(published.id)
        # A row whose own window closed two minutes ago with nothing ever attempted: stalled, not failing.
        session.execute(
            update(Outbox).where(Outbox.id == pending.id).values(next_attempt_at=utcnow() - timedelta(seconds=120))
        )
        session.commit()

    assert _counts(_sampler(settings, database=database)) == (0, 1, 1, frozenset({EXECUTION_QUEUE}))


def test_a_heartbeat_past_the_lease_ttl_is_no_longer_live(settings: Settings, database: Any) -> None:
    with database.session() as session:
        WorkerLeaseRepository(session, "").heartbeat(
            "worker-exec",
            pool_id="pool",
            capacity=2,
            active_count=0,
            capabilities={QUEUES_KEY: [EXECUTION_QUEUE]},
        )
        session.commit()
    sampler = _sampler(settings, database=database)
    assert _counts(sampler)[3] == frozenset({EXECUTION_QUEUE})

    with database.session() as session:
        session.execute(
            update(WorkerLease)
            .where(WorkerLease.worker_id == "worker-exec")
            .values(heartbeat_at=utcnow() - timedelta(seconds=int(settings.lease_ttl_seconds) + 1))
        )
        session.commit()
    assert _counts(_sampler(settings, database=database))[3] == frozenset()


def test_a_recent_publish_stops_being_evidence_once_the_window_closes(settings: Settings, database: Any) -> None:
    with database.session() as session:
        outbox = OutboxRepository(session, "")
        row = outbox.enqueue(aggregate_id="a", event_type="e", payload={}, discriminator="d")
        session.commit()
        outbox.mark_published(row.id)
        row_id = row.id
        session.execute(
            update(Outbox)
            .where(Outbox.id == row_id)
            .values(published_at=utcnow() - timedelta(seconds=DISPATCH_EVIDENCE_SECONDS + 30))
        )
        session.commit()
    assert _counts(_sampler(settings, database=database))[2] == 0


def test_a_failing_publish_is_counted_only_while_it_is_still_unpublished(settings: Settings, database: Any) -> None:
    """`mark_published` clears the error, so a non-zero count means the path is failing *now*."""
    with database.session() as session:
        outbox = OutboxRepository(session, "")
        row = outbox.enqueue(aggregate_id="a", event_type="e", payload={}, discriminator="d")
        session.commit()
        outbox.mark_retry(row.id, error="broker refused", attempts=1)
        row_id = row.id
    sampler = _sampler(settings, database=database)
    assert _counts(sampler)[0] == 1
    with database.session() as session:
        OutboxRepository(session, "").mark_published(row_id)
    assert _counts(_sampler(settings, database=database))[0] == 0


# --------------------------------------------------------------------------------------
# the route, against a running stack
# --------------------------------------------------------------------------------------


def test_the_database_the_tests_run_on_is_one_the_probe_calls_runnable(database: Any) -> None:
    """The fixture applies the structure the way the deploy job does, so drift here means real drift.

    A recorded version is part of `ok`: a fixture that only created the tables would leave every probe
    answer saying "drift" for a reason that has nothing to do with the code under test (§13.6).
    """
    assert current_drift(database).ok is True


def test_the_ready_answer_carries_five_components_and_three_signals(database: Any, tmp_path: Any) -> None:
    app = _app(database, tmp_path)
    with TestClient(app, base_url=BASE) as client:
        response = client.get(READINESS_PATH)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["status"] == "ready"
        assert body["components"] == {
            TRANSPORT: "running",
            AUTH: "initialised",
            DATABASE: "ok",
            SCHEMA: "ok",
            # No limiter Redis in this development-shaped deployment, and production could not start without
            # one - so `not_required` is only ever a development answer (§12.2).
            LIMITER: "not_required",
        }
        assert body["capabilities"] == {
            "submission_available": True,
            "execution_available": False,
            "background_available": False,
        }
        # Nothing has been published by this process and no worker has announced itself; the reason names
        # the half that is definitely missing rather than the evidence that is merely absent.
        assert body["capability_reasons"] == {
            "submission": "available",
            "execution": NO_LIVE_WORKER,
            "background": NO_LIVE_WORKER,
        }


async def test_an_unreachable_limiter_is_refused_without_killing_the_process(database: Any, tmp_path: Any) -> None:
    """AC-24's cold start and its recovery: one sample sees the outage, the next sees it gone."""
    app = _app(database, tmp_path, mcp_limiter_redis_url="redis://127.0.0.1:1/0")
    bundle = app.state.mcp
    admitter = bundle.services.limiter.admitter
    assert admitter is not None
    client = _ProbeClient(reachable=False)
    admitter._client = client
    await bundle.start()
    try:
        async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url=BASE) as http:
            response = await http.get(READINESS_PATH)
            assert response.status_code == 503
            body = response.json()
            assert body["components"][LIMITER] == UNAVAILABLE
            # The database and the manager are fine, which is exactly why this is a component answer and not
            # a process answer: nothing exited, nothing restarted.
            assert body["components"][DATABASE] == "ok"
            assert body["components"][SCHEMA] == "ok"
            assert body["capabilities"]["submission_available"] is False
            assert body["capability_reasons"]["submission"] == "limiter_unavailable"
            # One command for this dependency, and it was a PING: the probe never spent an admission script
            # and never read back a bucket it had just written.
            assert client.pings == 1
            assert client.evals == 0

            client.reachable = True
            # Stand in for the backoff having elapsed: the same sampler, the next pass, a 200.
            bundle.sampler.failures.pop(LIMITER, None)
            bundle.sampler._ready_at.pop(LIMITER, None)
            await bundle.sampler.sample_once()
            recovered = await http.get(READINESS_PATH)
            assert recovered.status_code == 200, recovered.text
            assert recovered.json()["components"][LIMITER] == "ok"
    finally:
        await bundle.stop()


async def test_a_limiter_that_answers_late_is_refused_within_the_probe_budget(
    database: Any, tmp_path: Any
) -> None:
    """A dependency that answers late is not one this probe waits for: §13.5 bounds each sample's network."""
    app = _app(database, tmp_path, mcp_limiter_redis_url="redis://127.0.0.1:1/0")
    admitter = app.state.mcp.services.limiter.admitter
    client = _ProbeClient(delay=0.5)
    admitter._client = client
    admitter.command_timeout = 0.05
    started = time.monotonic()
    available = await admitter.probe()
    elapsed = time.monotonic() - started
    await admitter.close()
    assert available is False
    assert elapsed < 0.4, elapsed
    # The command was issued and given up on, not skipped: a probe that never tried would have no evidence.
    assert client.pings == 1


async def test_a_probe_never_queues_behind_admission_work(database: Any, tmp_path: Any) -> None:
    """Every limiter connection is busy: the sample says unavailable, and does not add a waiter (§13.5)."""
    from backend.app.mcp.limits import POOL_MAX_CONNECTIONS

    app = _app(database, tmp_path, mcp_limiter_redis_url="redis://127.0.0.1:1/0")
    admitter = app.state.mcp.services.limiter.admitter
    client = _ProbeClient()
    admitter._client = client
    for _ in range(POOL_MAX_CONNECTIONS):
        await admitter._permits.acquire()
    assert admitter._permits.locked() is True
    try:
        available = await asyncio.wait_for(admitter.probe(), 0.5)
    finally:
        for _ in range(POOL_MAX_CONNECTIONS):
            admitter._permits.release()
        await admitter.close()
    assert available is False
    assert client.pings == 0


def test_the_probe_tells_a_queryable_database_from_a_runnable_schema(database: Any, tmp_path: Any) -> None:
    """`database ok` with `schema drift` is the pair AC-25 and AC-24 share."""
    app = _app(database, tmp_path)
    bundle = app.state.mcp
    with TestClient(app, base_url=BASE) as client:
        bundle.sampler.observations[BACKSTORE] = Observation(ok=True, value=_sample(schema_ok=False))
        response = client.get(READINESS_PATH)
        assert response.status_code == 503
        assert response.json()["components"] == {
            TRANSPORT: "running",
            AUTH: "initialised",
            DATABASE: "ok",
            SCHEMA: "drift",
            LIMITER: "not_required",
        }
        assert response.json()["capability_reasons"]["submission"] == "schema_unavailable"

        # "Not this shape" and "not there" are different codes, and the second one stops being an answer
        # rather than becoming a drift report (§13.6).
        bundle.sampler.observations[BACKSTORE] = Observation(ok=False, value=UNAVAILABLE)
        assert client.get(READINESS_PATH).json()["components"] == {
            TRANSPORT: "running",
            AUTH: "initialised",
            DATABASE: UNAVAILABLE,
            SCHEMA: "stale",
            LIMITER: "not_required",
        }


async def test_a_database_this_process_cannot_reach_is_not_called_a_shape_problem(
    settings: Settings, tmp_path: Any
) -> None:
    """The driver-error half of the pair, against an engine that really will not open.

    This is why there is no `SELECT 1`: the sample's first real read already raises for an unreachable
    database, and a probe that reported "schema drift" for a connection failure would send an operator to
    the migration job instead of to the network (§13.5, §13.6).
    """
    dead = Database(f"sqlite:///{tmp_path / 'absent' / 'gone.db'}")
    sampler = ReadinessSampler(settings, limiter=SimpleNamespace(), database=dead)
    await sampler._sample_backstore()
    observation = sampler.observation(BACKSTORE)
    states = (readiness._database_state(sampler), readiness._schema_state(sampler))
    await sampler.stop()
    assert observation is not None
    assert observation.ok is False
    assert states == (UNAVAILABLE, "stale")


def test_the_probe_stops_claiming_what_it_stopped_measuring(database: Any, tmp_path: Any) -> None:
    """关闭时排空采样: the cache unwinds with the threads, so a stopped process answers "unknown"."""
    app = _app(database, tmp_path)
    bundle = app.state.mcp
    with TestClient(app, base_url=BASE) as client:
        assert client.get(READINESS_PATH).status_code == 200
    assert bundle.sampler.observations == {}
    assert bundle.sampler._task is None
    assert TestClient(app, base_url=BASE).get(READINESS_PATH).json()["components"][DATABASE] == "stale"


def test_an_oidc_deployment_is_initialised_without_the_probe_fetching_a_key(
    database: Any, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§13.5: readiness=200 neither proves a token's key is reachable nor goes and looking."""
    for name in ("_fetch", "key_for"):
        monkeypatch.setattr(JwksKeySource, name, _forbidden, raising=True)
    app = _app(
        database,
        tmp_path,
        auth_mode="oidc",
        oidc_issuer="https://idp.example.com/realms/aita",
        oidc_jwks_uri="https://idp.example.com/realms/aita/protocol/openid-connect/certs",
        mcp_oidc_audience="aita-mcp",
    )
    with TestClient(app, base_url=BASE) as client:
        assert client.get(READINESS_PATH).status_code == 200
        assert component_state(app, app.state.settings)[AUTH] == "initialised"

    # An audience is what makes the key source usable: without it the component was never configured, and
    # the probe says so instead of calling a broken deployment ready.
    without_audience = app.state.settings.model_copy(update={"mcp_oidc_audience": ""})
    assert component_state(app, without_audience)[AUTH] == "uninitialised"
    assert is_ready(component_state(app, without_audience)) is False


# --------------------------------------------------------------------------------------
# the producer half: what a worker writes, the probe can read
# --------------------------------------------------------------------------------------


def test_a_worker_heartbeat_carries_the_queues_it_serves(settings: Settings, database: Any) -> None:
    """`celery -Q` is invisible to the probe, so the declaration has to live in the heartbeat (§13.5)."""
    from backend.app.orchestrator.heartbeat import WorkerAnnouncer

    with database.session() as session:
        session.add(WorkerPool(id=new_id(), name=settings.worker_pool_name, capacity=2))
        session.commit()

    WorkerAnnouncer(worker_id="w-declared", settings=settings, roles=[EXECUTION_QUEUE])._tick()
    WorkerAnnouncer(worker_id="w-quiet", settings=settings)._tick()

    with database.session() as session:
        rows = {
            lease.worker_id: lease.capabilities
            for lease in session.scalars(select(WorkerLease).where(WorkerLease.worker_id.like("w-%")))
        }
    assert rows["w-declared"][QUEUES_KEY] == [EXECUTION_QUEUE]
    # A worker that named no queue writes no declaration, which the probe reads as "claims nothing".
    assert QUEUES_KEY not in rows["w-quiet"]
    assert _counts(_sampler(settings, database=database))[3] == frozenset({EXECUTION_QUEUE})


def test_a_worker_is_counted_from_the_moment_it_starts(settings: Settings, database: Any) -> None:
    """Starting is the fact an operator acts on, so the first beat cannot be one interval away (§9.3).

    `worker_available` on a run receipt and the readiness probe's live-worker component both read the same
    `live_workers` query. A worker that announces only after its first sleep reports "no capacity" while it
    is standing in the room, and the two ways that reads wrong - a run refused for nothing, a probe that
    says NO_LIVE_WORKER about a healthy fleet - are both decided before the thread has beaten once.
    """
    from backend.app.orchestrator.heartbeat import WorkerAnnouncer

    with database.session() as session:
        session.add(WorkerPool(id=new_id(), name=settings.worker_pool_name, capacity=2))
        session.commit()

    announcer = WorkerAnnouncer(worker_id="w-first-beat", settings=settings, roles=[EXECUTION_QUEUE])
    announcer.start()
    try:
        with database.session() as session:
            live = WorkerLeaseRepository(session, "").live_workers(ttl_seconds=int(settings.lease_ttl_seconds))
        assert [lease.worker_id for lease in live] == ["w-first-beat"]
        assert live[0].capabilities[QUEUES_KEY] == [EXECUTION_QUEUE]
    finally:
        announcer.stop()

    with database.session() as session:
        gone = WorkerLeaseRepository(session, "").live_workers(ttl_seconds=int(settings.lease_ttl_seconds))
        kept = session.scalar(select(WorkerLease).where(WorkerLease.worker_id == "w-first-beat"))
    # Stopping is a drain flag on the row, never a delete: the fleet is described by heartbeats going stale.
    assert [lease.worker_id for lease in gone] == []
    assert kept is not None
    assert kept.draining is True


def test_a_celery_worker_declares_only_what_worker_roles_names(settings: Settings) -> None:
    """The empty default is the safe one: it means no capacity, and never "assume the operator meant me"."""
    from backend.app.orchestrator.runtime import Supervisor

    celery = settings.model_copy(update={"queue_backend": "celery", "worker_roles": ""})
    supervisor = Supervisor(celery, announce=False)
    assert supervisor.executes_tasks is False
    assert supervisor._declared_roles() == []

    labelled = Supervisor(celery.model_copy(update={"worker_roles": "compile, analysis"}), announce=False)
    assert labelled._declared_roles() == ["compile", "analysis"]

    # The single-process development worker runs the work itself, so it covers every queue.
    inprocess = Supervisor(settings.model_copy(update={"queue_backend": "inprocess"}), announce=False)
    assert sorted(inprocess._declared_roles()) == sorted(set(QUEUES_FOR_TASK.values()))


def test_the_shipped_compose_declares_each_worker_s_queues() -> None:
    """A `-Q` and a `WORKER_ROLES` that drift apart would report capacity the process does not have."""
    services = yaml.safe_load((REPO / "compose.yaml").read_text(encoding="utf-8"))["services"]
    for name in ("worker-execution", "worker-background"):
        command = services[name]["command"]
        consumed = command[command.index("-Q") + 1]
        declared = services[name]["environment"]["WORKER_ROLES"]
        assert sorted(consumed.split(",")) == sorted(declared.split(",")), name
