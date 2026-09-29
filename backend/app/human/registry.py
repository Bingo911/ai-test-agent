"""The live-session hand-off the control gateway uses (§10.2, §13.4).

A pause exists in the one process that owns the browser page, so an operator's click has to reach that
process. Locally that is this registry; a split deployment uses the persisted `execution_command` rows
instead. Both routes end up in `HumanGate.submit_operator_input`, so no command can skip the whitelist
however it arrived.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ..observability import get_logger

log = get_logger(__name__)

#: Frames are replaced, not queued: an operator must never act on a stale picture (§10.2).
SUBSCRIBER_QUEUE_MAX = 8


@dataclass
class LiveSession:
    execution_id: str
    human_task_id: str
    step_id: str
    epoch: int
    submit: Callable[[dict[str, Any]], None]
    close: Callable[[], None] = field(default=lambda: None)


class ControlPlane:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sessions: dict[str, LiveSession] = {}
        self._frames: dict[str, dict[str, Any]] = {}
        self._subscribers: dict[str, list[tuple[asyncio.AbstractEventLoop, asyncio.Queue]]] = {}

    # ------------------------------------------------------------------ sessions

    def open(self, session: LiveSession) -> None:
        with self._lock:
            previous = self._sessions.get(session.human_task_id)
            if previous is not None and previous is not session:
                previous.close()
            self._sessions[session.human_task_id] = session

    def close(self, human_task_id: str) -> None:
        with self._lock:
            self._sessions.pop(human_task_id, None)
            self._frames.pop(human_task_id, None)
            self._subscribers.pop(human_task_id, None)

    def is_live(self, human_task_id: str) -> bool:
        with self._lock:
            return human_task_id in self._sessions

    def session_for(self, human_task_id: str) -> LiveSession | None:
        with self._lock:
            return self._sessions.get(human_task_id)

    def deliver(self, human_task_id: str, command: dict[str, Any]) -> bool:
        """Hand one already-persisted command to the holder; False when the browser lives elsewhere."""
        with self._lock:
            session = self._sessions.get(human_task_id)
        if session is None:
            return False
        try:
            session.submit(command)
        except Exception as exc:
            log.info(
                "control hand-off refused", extra={"context": {"human_task_id": human_task_id, "error": str(exc)[:200]}}
            )
            return False
        return True

    # -------------------------------------------------------------------- frames

    def publish_frame(self, frame: dict[str, Any]) -> None:
        human_task_id = str(frame.get("human_task_id") or "")
        if not human_task_id:
            return
        with self._lock:
            if human_task_id not in self._sessions:
                return  # the pause already ended; a frame nobody waits for is not evidence
            self._frames[human_task_id] = frame
            subscribers = list(self._subscribers.get(human_task_id, ()))
        for loop, queue in subscribers:
            self._push(loop, queue, frame)

    @staticmethod
    def _push(loop: asyncio.AbstractEventLoop, queue: asyncio.Queue, frame: dict[str, Any]) -> None:
        def enqueue() -> None:
            while queue.qsize() >= SUBSCRIBER_QUEUE_MAX:
                # Drop the oldest frame: catching up on pictures is pointless, the newest is what
                # the operator may act on.
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:  # pragma: no cover - raced with a reader
                    break
            queue.put_nowait(frame)

        # pragma: no cover - the subscriber's loop already closed
        with contextlib.suppress(RuntimeError):
            loop.call_soon_threadsafe(enqueue)

    def latest_frame(self, human_task_id: str) -> dict[str, Any] | None:
        with self._lock:
            return self._frames.get(human_task_id)

    def has_subscribers(self, human_task_id: str) -> bool:
        with self._lock:
            return bool(self._subscribers.get(human_task_id))

    def subscribe(self, human_task_id: str) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=SUBSCRIBER_QUEUE_MAX)
        loop = asyncio.get_running_loop()
        with self._lock:
            self._subscribers.setdefault(human_task_id, []).append((loop, queue))
            frame = self._frames.get(human_task_id)
        if frame is not None:  # start from the current picture, not from the next refresh
            self._push(loop, queue, frame)
        return queue

    def unsubscribe(self, human_task_id: str, queue: asyncio.Queue) -> None:
        with self._lock:
            entries = self._subscribers.get(human_task_id)
            if not entries:
                return
            self._subscribers[human_task_id] = [entry for entry in entries if entry[1] is not queue]
            if not self._subscribers[human_task_id]:
                self._subscribers.pop(human_task_id, None)


_plane: ControlPlane | None = None


def get_control_plane() -> ControlPlane:
    global _plane
    if _plane is None:
        _plane = ControlPlane()
    return _plane
