"""Celery application for the three task queues (§9.3, §15.3).

Task names are the outbox task strings, so the dispatcher can `send_task` without importing a worker.
Time limits sit *above* the budgets the workers enforce themselves: an execution worker finalizes and
frees its slot inside `task_hard_limit_seconds`, and Celery only pulls the plug on a process that
ignored its own deadline.
"""

from __future__ import annotations

import os
from typing import Any

from celery import Celery
from celery.signals import worker_process_init, worker_process_shutdown
from kombu import Queue as KombuQueue

from .config import Settings, get_settings
from .orchestrator.queue import EXECUTE_TASK, QUEUES_FOR_TASK
from .orchestrator.tasks import task_handlers

QUEUE_NAMES = ("compile", "execution", "analysis")

_app: Celery | None = None
_announcers: dict[int, Any] = {}


def build_celery_app(settings: Settings | None = None) -> Celery:
    """One app per process: registering the same task name twice only produces a warning and a leak."""
    global _app
    if _app is not None:
        return _app
    settings = settings or get_settings()
    app = Celery("ai_test_agent", broker=settings.redis_url, backend=None)
    grace = settings.finalization_timeout_seconds + settings.lease_ttl_seconds
    execution_hard = settings.task_hard_limit_seconds + grace
    short_hard = settings.queue_timeout_seconds + settings.finalization_timeout_seconds
    app.conf.update(
        task_default_queue=QUEUES_FOR_TASK[EXECUTE_TASK],
        task_queues=[KombuQueue(name, routing_key=name) for name in QUEUE_NAMES],
        task_routes={name: {"queue": queue} for name, queue in QUEUES_FOR_TASK.items()},
        task_serializer="json",
        result_serializer="json",
        accept_content=["json"],
        task_track_started=True,
        task_acks_late=True,
        task_reject_on_worker_lost=True,
        task_prefetch_multiplier=1,
        worker_prefetch_multiplier=1,
        worker_concurrency=settings.worker_slots,
        broker_connection_retry_on_startup=True,
        broker_connection_timeout=5.0,
        broker_transport_options={"visibility_timeout": settings.visibility_timeout_seconds},
        task_time_limit=execution_hard,
        task_soft_time_limit=settings.task_hard_limit_seconds,
        timezone="UTC",
        enable_utc=True,
    )
    for name, handler in task_handlers().items():
        hard = execution_hard if name == EXECUTE_TASK else short_hard
        app.task(name=name, acks_late=True, time_limit=hard, soft_time_limit=max(30, hard - grace))(handler)
    _app = app
    return app


@worker_process_init.connect
def _announce(**_: Any) -> None:
    """Each prefork child says it exists; a child holds exactly one browser slot (§9.3)."""
    from .orchestrator.heartbeat import WorkerAnnouncer
    from .workers.execution import active_run_count, default_worker_id

    _announcers[os.getpid()] = WorkerAnnouncer(
        worker_id=default_worker_id(), capacity=1, active_count=active_run_count
    ).start()


@worker_process_shutdown.connect
def _stop_announcing(**_: Any) -> None:
    announcer = _announcers.pop(os.getpid(), None)
    if announcer is not None:
        announcer.stop()


#: `celery -A app.celery_app:app worker -Q compile,execution,analysis`
app = build_celery_app(get_settings())
