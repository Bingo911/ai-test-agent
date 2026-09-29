"""Task delivery behind the outbox dispatcher (§9.3).

The platform needs two deliveries only: *something should run* and *run it now*. Everything durable
lives in PostgreSQL, so the broker may lose a message and the reconciler converges anyway. That is
why `inprocess` is a first-class backend rather than a test stub: it keeps the same at-least-once,
idempotent-consumer contract without requiring Redis on a laptop.
"""

from __future__ import annotations

import queue as _queue
import socket
import threading
import time
from typing import Protocol

from ..config import Settings, get_settings
from ..observability import get_logger

log = get_logger(__name__)

COMPILE_TASK = "case.compile"
EXECUTE_TASK = "execution.execute"
ANALYZE_TASK = "execution.analyze"
QUEUES_FOR_TASK = {COMPILE_TASK: "compile", EXECUTE_TASK: "execution", ANALYZE_TASK: "analysis"}


class TaskHandler(Protocol):
    def __call__(self, payload: dict) -> object: ...


class Queue(Protocol):
    def publish(self, task: str, payload: dict) -> None: ...

    def close(self) -> None: ...


class InProcessQueue:
    """Thread-pool queue used for development, tests and single-node deployments."""

    def __init__(self, handlers: dict[str, TaskHandler], *, concurrency: int = 4, name: str = "aita-inprocess") -> None:
        self.handlers = handlers
        self._pending: _queue.Queue[tuple[str, dict] | None] = _queue.Queue()
        self._threads: list[threading.Thread] = []
        self._stopping = threading.Event()
        self._outstanding = 0
        self._lock = threading.Lock()
        self._errors: list[tuple[str, str]] = []
        self.concurrency = max(1, concurrency)
        self.name = name

    def publish(self, task: str, payload: dict) -> None:
        if task not in self.handlers:
            raise KeyError(f"no handler registered for task '{task}'")
        # Counted at publish time, not when a thread picks the item up: a reader that sees zero
        # outstanding work can then be sure nothing is between "dequeued" and "running".
        with self._lock:
            self._outstanding += 1
        self._pending.put((task, payload))

    def start(self) -> None:
        while len(self._threads) < self.concurrency:
            thread = threading.Thread(target=self._loop, name=f"{self.name}-{len(self._threads)}", daemon=True)
            self._threads.append(thread)
            thread.start()

    def _loop(self) -> None:
        while not self._stopping.is_set():
            try:
                item = self._pending.get(timeout=0.2)
            except _queue.Empty:
                continue
            if item is None:
                self._pending.task_done()
                break
            task, payload = item
            try:
                self._run(task, payload)
            finally:
                self._pending.task_done()
                with self._lock:
                    self._outstanding = max(0, self._outstanding - 1)

    def _run(self, task: str, payload: dict) -> None:
        try:
            self.handlers[task](payload)
        except Exception as exc:
            self._errors.append((task, f"{type(exc).__name__}: {exc}"))
            log.exception("in-process task failed", extra={"context": {"task": task, "error": str(exc)}})

    def drain(self, *, timeout: float = 30.0) -> bool:
        """Run everything already published and report whether the queue emptied."""
        self.start()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.idle():
                return True
            time.sleep(0.02)
        return False

    def idle(self) -> bool:
        return self._pending.empty() and self.is_idle()

    def is_idle(self) -> bool:
        with self._lock:
            return self._outstanding == 0

    def errors(self) -> list[tuple[str, str]]:
        with self._lock:
            return list(self._errors)

    def close(self) -> None:
        self._stopping.set()
        for _ in self._threads:
            self._pending.put(None)
        for thread in self._threads:
            thread.join(timeout=2.0)
        self._threads.clear()


class CeleryQueue:
    """Production delivery: three queues, one task per kind, no results backend required."""

    def __init__(self, settings: Settings) -> None:
        from ..celery_app import build_celery_app

        self.settings = settings
        self.app = build_celery_app(settings)

    def publish(self, task: str, payload: dict) -> None:
        self.app.send_task(task, args=[payload], queue=QUEUES_FOR_TASK.get(task, "execution"))

    def close(self) -> None:
        return None


def redis_reachable(url: str, *, timeout: float = 1.0) -> bool:
    """Cheap TCP probe: `auto` only chooses Celery when the broker can actually be contacted."""
    try:
        scheme, _, rest = url.partition("://")
        host_port, _, _ = rest.partition("/")
        host, _, port = host_port.partition(":")
        if not host:
            return False
        with socket.create_connection((host, int(port or (6379 if "redis" in scheme else 5672))), timeout=timeout):
            return True
    except (OSError, ValueError):
        return False


#: The process-wide queue, distinct from the `queue` module alias imported above.
_default_queue: Queue | None = None


def build_queue(handlers: dict[str, TaskHandler], settings: Settings | None = None) -> Queue:
    settings = settings or get_settings()
    if settings.queue_backend == "celery":
        return CeleryQueue(settings)
    if settings.queue_backend == "auto" and redis_reachable(settings.redis_url):
        try:
            return CeleryQueue(settings)
        except Exception as exc:  # pragma: no cover - fall back instead of refusing to start
            log.warning("celery unavailable, using the in-process queue", extra={"context": {"error": str(exc)}})
    return InProcessQueue(handlers, concurrency=settings.worker_slots)


def set_queue(q: Queue | None) -> None:
    global _default_queue
    _default_queue = q


def get_queue() -> Queue:
    global _default_queue
    if _default_queue is None:
        from .tasks import task_handlers

        _default_queue = build_queue(task_handlers())
    return _default_queue
