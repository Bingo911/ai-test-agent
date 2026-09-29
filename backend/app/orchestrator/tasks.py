"""Task name to handler: the registry the in-process queue and Celery both build from (§9.3).

Handlers import their worker module inside the call. The API process publishes `case.compile` without
ever importing Playwright, and a worker process imports the workers without importing the HTTP layer.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .queue import ANALYZE_TASK, COMPILE_TASK, EXECUTE_TASK, QUEUES_FOR_TASK

TaskFn = Callable[[dict[str, Any]], Any]


def run_compile(payload: dict[str, Any]) -> Any:
    from ..workers.compile import compile_task

    return compile_task(payload)


def run_execute(payload: dict[str, Any]) -> Any:
    from ..workers.execution import execute_task

    return execute_task(payload)


def run_analyze(payload: dict[str, Any]) -> Any:
    from ..workers.analysis import analyze_task

    return analyze_task(payload)


HANDLERS: dict[str, TaskFn] = {
    COMPILE_TASK: run_compile,
    EXECUTE_TASK: run_execute,
    ANALYZE_TASK: run_analyze,
}


def task_handlers() -> dict[str, TaskFn]:
    return dict(HANDLERS)


def handler_for(task: str) -> TaskFn | None:
    return HANDLERS.get(task)


def queue_for(task: str) -> str:
    return QUEUES_FOR_TASK.get(task, "execution")
