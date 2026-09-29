"""One-process control loop: scheduler, dispatcher, reconciler and in-process workers (§15.3).

On a server these are separate services. Running them together is what lets `make run` and the
acceptance tests finish a real browser run on a laptop, without Redis, a broker or a second process —
and every loop here is the same object the split deployment runs, so nothing is being simulated.
"""

from __future__ import annotations

import argparse
import signal
import threading
import time
from typing import Any

from ..config import Settings, get_settings
from ..observability import get_logger
from .dispatcher import OutboxDispatcher
from .queue import InProcessQueue, Queue, get_queue
from .reconciler import Reconciler
from .scheduler import Scheduler

log = get_logger(__name__)


class Supervisor:
    def __init__(
        self,
        settings: Settings | None = None,
        *,
        queue: Queue | None = None,
        announce: bool | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self._explicit_queue = queue
        self.queue: Queue | None = queue
        self.dispatcher = OutboxDispatcher(self.settings, queue=queue)
        self.scheduler = Scheduler(self.settings)
        self.reconciler = Reconciler(self.settings)
        self.executes_tasks = self._queue_is_inprocess()
        self.announce = announce if announce is not None else self.executes_tasks
        self.announcer: Any = None
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self.ticks = {"scheduler": 0, "dispatch": 0, "reconcile": 0}

    def _queue_is_inprocess(self) -> bool:
        queue = self._explicit_queue
        if queue is None:
            # `auto` may have chosen Celery, so look only once the queue actually exists.
            return self.settings.queue_backend in ("inprocess", "auto") and not _redis_available(self.settings)
        return isinstance(queue, InProcessQueue)

    # ------------------------------------------------------------------ lifetime

    def start(self) -> Supervisor:
        if self.queue is None:
            self.queue = get_queue()
            self.dispatcher = OutboxDispatcher(self.settings, queue=self.queue)
        if isinstance(self.queue, InProcessQueue):
            self.queue.start()
        if self.announce:
            self._start_announcer()
        for name, target, interval in (
            ("scheduler", self.scheduler_tick, self.settings.scheduler_poll_interval_seconds),
            ("dispatch", self.dispatch_tick, self.settings.outbox_poll_interval_seconds),
            ("reconcile", self.reconcile_tick, self.settings.reconciler_interval_seconds),
        ):
            thread = threading.Thread(
                target=self._guarded, args=(name, target, interval), name=f"{name}-loop", daemon=True
            )
            self._threads.append(thread)
            thread.start()
        log.info(
            "supervisor started",
            extra={"context": {"backend": str(self.settings.queue_backend), "announce": self.announce}},
        )
        return self

    def _start_announcer(self) -> None:
        from ..workers.execution import active_run_count, default_worker_id
        from .heartbeat import WorkerAnnouncer

        self.announcer = WorkerAnnouncer(worker_id=default_worker_id(), active_count=active_run_count).start()

    def stop(self, *, timeout: float = 5.0) -> None:
        self._stop.set()
        for thread in self._threads:
            thread.join(timeout=timeout)
        self._threads.clear()
        if self.announcer is not None:
            self.announcer.stop()
            self.announcer = None

    def __enter__(self) -> Supervisor:
        return self.start()

    def __exit__(self, *_: Any) -> None:
        self.stop()

    # ----------------------------------------------------------------- single pass

    def scheduler_tick(self) -> int:
        self.ticks["scheduler"] += 1
        return len(self.scheduler.tick())

    def dispatch_tick(self) -> int:
        self.ticks["dispatch"] += 1
        return self.dispatcher.tick()

    def reconcile_tick(self) -> dict[str, int]:
        self.ticks["reconcile"] += 1
        return self.reconciler.tick()

    def pump_once(self) -> dict[str, Any]:
        """One pass of each loop, in the order that lets a fresh run make progress in one go."""
        scheduled = self.scheduler_tick()
        published = self.dispatch_tick()
        return {"scheduled": scheduled, "published": published, "reconciled": self.reconcile_tick()}

    def drain(self, *, timeout: float = 60.0, pump: bool = True) -> bool:
        """Pump the loops until the in-process queue is idle; the dev equivalent of awaiting a run."""
        if not isinstance(self.queue, InProcessQueue):
            raise TypeError(f"drain() needs the in-process queue, not {type(self.queue).__name__}")
        deadline = time.monotonic() + timeout
        self.queue.start()
        while time.monotonic() < deadline:
            if pump:
                self.scheduler_tick()
                self.dispatch_tick()
            if self.queue.idle():
                # One more pass in case the last task published an analysis event.
                if pump:
                    self.dispatch_tick()
                if self.queue.idle():
                    return True
            time.sleep(0.02)
        return False

    # -------------------------------------------------------------------- threads

    def _guarded(self, name: str, target: Any, interval: float) -> None:
        while not self._stop.wait(interval):
            try:
                target()
            except Exception as exc:
                log.exception(f"{name} loop failed", extra={"context": {"error": type(exc).__name__}})


def _redis_available(settings: Settings) -> bool:
    from .queue import redis_reachable

    return settings.queue_backend == "celery" or (
        settings.queue_backend == "auto" and redis_reachable(settings.redis_url)
    )


def run_forever(settings: Settings | None = None) -> None:
    """Start the database, seed a development workspace and supervise the loops until signalled."""
    from ..db.bootstrap import bootstrap_runtime

    settings = settings or get_settings()
    workspace = bootstrap_runtime(settings)
    supervisor = Supervisor(settings).start()
    stop = threading.Event()

    def _handle(signum: int, _frame: Any) -> None:
        log.info("shutting down", extra={"context": {"signal": signum}})
        stop.set()

    for name in ("SIGINT", "SIGTERM"):
        number = getattr(signal, name, None)
        if number is not None:
            signal.signal(number, _handle)
    try:
        while not stop.wait(1.0):
            pass
    finally:
        supervisor.stop()
        if workspace:
            log.info("supervisor stopped", extra={"context": {"project_id": workspace.get("project_id")}})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the AI Test Agent control loops in one process.")
    parser.add_argument("--once", action="store_true", help="pump each loop one time and exit")
    parser.add_argument("--quiet", action="store_true", help="do not announce this worker to the pool table")
    args = parser.parse_args(argv)
    settings = get_settings()
    if args.once:
        from ..db.bootstrap import bootstrap_runtime

        bootstrap_runtime(settings)
        supervisor = Supervisor(settings, announce=False)
        print(supervisor.pump_once())
        return 0
    run_forever(settings)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
