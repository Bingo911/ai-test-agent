"""The lease keeper (§9.4).

Renewing the lease has to continue while the event loop is inside a Playwright call, so it runs on
its own thread with its own database session. Its only output is two booleans on the shared
`Interruption`: the step loop checks them at the next boundary and stops issuing browser actions.
"""

from __future__ import annotations

import threading
import time
from typing import Any

from ..config import Settings, get_settings
from ..db.base import get_database
from ..observability import get_logger
from ..repositories.executions import ExecutionRepository

log = get_logger(__name__)


class LeaseKeeper:
    """Heartbeat plus cancel watcher for one held execution."""

    def __init__(
        self,
        *,
        execution_id: str,
        tenant_id: str,
        epoch: int,
        context: Any,
        worker_id: str,
        settings: Settings | None = None,
    ) -> None:
        self.execution_id = execution_id
        self.tenant_id = tenant_id
        self.epoch = int(epoch)
        self.context = context
        self.worker_id = worker_id
        self.settings = settings or get_settings()
        self.db = get_database()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._misses = 0
        self.last_error: str | None = None
        self.last_heartbeat_at: float | None = None

    def start(self) -> LeaseKeeper:
        self._thread = threading.Thread(target=self._loop, name=f"lease-{self.execution_id[:8]}", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=max(2.0, self.settings.lease_heartbeat_seconds * 2))
            self._thread = None

    def __enter__(self) -> LeaseKeeper:
        return self.start()

    def __exit__(self, *_: Any) -> None:
        self.stop()

    def _loop(self) -> None:
        interval = float(self.settings.lease_heartbeat_seconds)
        while not self._stop.wait(interval):
            try:
                self._tick()
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
                self._misses += 1
                # The lease expires on its own if we cannot renew it; after that the holder is stale.
                if self._misses * interval >= float(self.settings.lease_ttl_seconds):
                    log.exception(
                        "lease keeper lost the database",
                        extra={"context": {"execution_id": self.execution_id, "error": self.last_error}},
                    )
                    self.context.interruption.lease_lost = True
                    return

    def _tick(self) -> None:
        with self.db.session(self.tenant_id) as session:
            repo = ExecutionRepository(session, self.tenant_id)
            renewed = repo.heartbeat(self.execution_id, epoch=self.epoch)
            live = repo.epoch_is_current(self.execution_id, epoch=self.epoch)
        self.last_heartbeat_at = time.monotonic()
        self._misses = 0
        if not renewed or not live:
            self.last_error = "the lease was revoked or taken over"
            log.warning(
                "lease no longer held", extra={"context": {"execution_id": self.execution_id, "epoch": self.epoch}}
            )
            # `epoch_is_current` is false for a cancel request too; the execution row says which.
            self._classify_lost()

    def _classify_lost(self) -> None:
        try:
            with self.db.session(self.tenant_id) as session:
                execution = ExecutionRepository(session, self.tenant_id).load(self.execution_id)
                cancelled = execution.cancel_requested_at is not None
        except Exception:
            cancelled = False
        if cancelled:
            self.context.interruption.cancel = True
        else:
            self.context.interruption.lease_lost = True


class WorkerAnnouncer:
    """Say which worker process is alive, how many slots it has and whether it is draining (§9.3).

    Pool `reserved_count` is what the scheduler counts; this row is what an operator reads to tell
    "no capacity" apart from "capacity exists but nothing is scheduled". A process that stops
    heartbeating simply disappears from `live_workers` after the ttl.
    """

    def __init__(
        self,
        *,
        worker_id: str,
        settings: Settings | None = None,
        capacity: int | None = None,
        pool_name: str | None = None,
        active_count: Any = None,
        capabilities: dict[str, Any] | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.worker_id = worker_id
        self.capacity = int(capacity if capacity is not None else self.settings.worker_slots)
        self.pool_name = pool_name or self.settings.worker_pool_name
        self._active_count = active_count
        self.capabilities = capabilities or {"browsers": self.settings.browser_channel_list, "actions": "ir-1.0"}
        self.db = get_database()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.pool_id: str | None = None
        self.draining = False

    def start(self) -> WorkerAnnouncer:
        self._thread = threading.Thread(target=self._loop, name=f"announce-{self.worker_id[:12]}", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self.drain()
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=max(2.0, self.settings.lease_heartbeat_seconds * 2))
            self._thread = None

    def __enter__(self) -> WorkerAnnouncer:
        return self.start()

    def __exit__(self, *_: Any) -> None:
        self.stop()

    def drain(self) -> None:
        """Stop accepting new work: the last live heartbeat carries the flag."""
        self.draining = True
        self._tick()

    def _loop(self) -> None:
        interval = float(self.settings.lease_heartbeat_seconds)
        while not self._stop.wait(interval):
            try:
                self._tick()
            except Exception as exc:
                log.warning(
                    "worker announce failed", extra={"context": {"worker_id": self.worker_id, "error": str(exc)}}
                )

    def _tick(self) -> None:
        from ..repositories.reservations import PoolRepository, WorkerLeaseRepository

        active = int(self._active_count() if callable(self._active_count) else (self._active_count or 0))
        with self.db.session() as session:
            pool = PoolRepository(session, "").by_name(self.pool_name)
            if pool is None:
                return
            self.pool_id = pool.id
            WorkerLeaseRepository(session, "").heartbeat(
                self.worker_id,
                pool_id=pool.id,
                capacity=self.capacity,
                active_count=active,
                capabilities=dict(self.capabilities),
                draining=self.draining,
            )
            session.commit()
