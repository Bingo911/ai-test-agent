"""Worker-side human assistance (§10.2, §10.3, §10.4).

Only the worker that owns the live browser may run this gate: a pause is a decision about a page
that exists in one process, and the resume check has to read that same page. The gate therefore
blocks the *step loop*, not the event loop — frames for the operator, lease checks and operator
input all keep flowing while it waits.
"""

from __future__ import annotations

import asyncio
import base64
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ..config import Settings, get_settings
from ..db.base import coerce_utc, get_database, monotonic_ms, utcnow
from ..domain.enums import (
    CommandStatus,
    CommandType,
    ExecutionStatus,
    HumanTaskStatus,
    Outcome,
    ResumePhase,
)
from ..domain.errors import ErrorCode
from ..executors.contracts import HumanRequired, StepInterrupted
from ..executors.locator import LocatorBudget
from ..executors.playwright.actions import evaluate_condition
from ..executors.playwright.session import BrowserSession
from ..ir.models import Condition, Step
from ..observability import get_logger
from ..repositories.artifacts import ArtifactRepository
from ..repositories.executions import ExecutionRepository
from ..repositories.human import CommandRepository, HumanTaskRepository
from ..repositories.platform import AuditRepository
from .registry import LiveSession, get_control_plane

log = get_logger(__name__)

#: §10.2 — the only remote operations the control gateway may forward.
OPERATION_WHITELIST = frozenset({"click", "type", "key", "scroll"})
KEY_WHITELIST = frozenset(
    {
        "Enter",
        "Tab",
        "Escape",
        "Backspace",
        "ArrowUp",
        "ArrowDown",
        "ArrowLeft",
        "ArrowRight",
        "Home",
        "End",
        "PageUp",
        "PageDown",
        "Space",
    }
)
#: How often the holder re-reads commands, task state and the deadline (§9.3).
COMMAND_POLL_SECONDS = 1.0
#: The resume-condition check gets its own bounded budget, never the step's remaining time (§10.3).
RESUME_CHECK_BUDGET_MS = 10_000


class HumanGateError(Exception):
    """A pause that cannot be honoured, carrying the outcome the worker must finalise with."""

    def __init__(
        self,
        error_code: str,
        message: str,
        *,
        outcome: str = Outcome.ERROR.value,
        detail: dict[str, Any] | None = None,
    ) -> None:
        self.error_code = error_code
        self.message = message
        self.outcome = outcome
        self.detail = detail or {}
        super().__init__(message)


class LeaseGone(HumanGateError):
    """The holder lost its right to drive the execution; the DB decides cancel versus session loss."""

    def __init__(self, reason: str) -> None:
        super().__init__(
            ErrorCode.SESSION_LOST.value,
            f"the live session stopped being authoritative: {reason}",
            detail={"reason": reason},
        )
        self.reason = reason


class ApiReject(Exception):
    """A gateway request refused because no operator session is open."""


@dataclass
class PauseDecision:
    step_id: str
    mode: str
    reason: str
    resume_phase: str
    resume_condition: dict[str, Any] | None = None


@dataclass
class HumanSessionState:
    """What the control gateway needs to authorise and render one operator session (§10.2)."""

    human_task_id: str
    step_id: str
    mode: str
    reason: str
    pause_token: str
    session_epoch: int
    viewport: tuple[int, int]
    paused_at: float
    deadline_at: Any
    page_url: str = ""
    frame_id: int = 0
    frame_at: float = 0.0
    controller_id: str | None = None
    last_rejection: str | None = None
    sequence: int = 0


@dataclass
class HumanGate:
    execution_id: str
    tenant_id: str
    project_id: str
    worker_id: str
    session: BrowserSession
    context: Any
    executor: Any
    settings: Settings = field(default_factory=get_settings)
    frame_sink: Callable[[dict[str, Any]], None] | None = None

    def __post_init__(self) -> None:
        self.db = get_database()
        self.state: HumanSessionState | None = None
        self._pending_input: deque[dict[str, Any]] = deque()
        if self.frame_sink is None:
            self.frame_sink = get_control_plane().publish_frame

    # ------------------------------------------------------------------ triggers

    async def hook(self, step: Step) -> None:
        """`RunContext.human_hook`: the §7.3 gate that runs before any locator attempt."""
        decision = self._plan_before(step)
        if decision is not None:
            await self.pause(step, decision)

    def _plan_before(self, step: Step) -> PauseDecision | None:
        policy = getattr(step, "human_policy", None)
        if policy is not None and policy.mode == "before":
            return PauseDecision(
                step_id=step.id,
                mode="before",
                reason=policy.reason or "the case requires a human confirmation before this step",
                resume_phase=ResumePhase.BEFORE_ACTION.value,
            )
        command = self._pause_command(step.id)
        if command is not None:
            payload = dict(command.payload or {})
            self._mark_command(command, result={"accepted": True})
            return PauseDecision(
                step_id=step.id,
                mode="operator",
                reason=str(payload.get("reason") or "an operator requested a pause"),
                resume_phase=ResumePhase.BEFORE_ACTION.value,
            )
        return None

    async def after_step(self, step: Step, result: Any) -> None:
        """A challenge that appeared *because of* the action pauses after it, not before (§10.3)."""
        policy = getattr(step, "human_policy", None)
        if policy is None or policy.mode != "on_challenge" or policy.resume_condition is None:
            return
        if not getattr(result, "ok", False):
            return
        condition = policy.resume_condition.model_dump(mode="json")
        holds, _ = await self._evaluate_condition(condition)
        if holds:
            return
        await self.pause(
            step,
            PauseDecision(
                step_id=step.id,
                mode="on_challenge",
                reason=policy.reason or "the target presented a challenge after this step",
                resume_phase=ResumePhase.AFTER_ACTION.value,
                resume_condition=condition,
            ),
        )

    # --------------------------------------------------------------------- pausing

    async def pause(self, step: Step, decision: PauseDecision) -> None:
        self._check_allowance()
        task = self._open_task(step, decision)
        await self._suspend_sensitive_capture()
        state = HumanSessionState(
            human_task_id=task.id,
            step_id=step.id,
            mode=decision.mode,
            reason=decision.reason,
            pause_token=task.pause_token,
            session_epoch=int(task.session_epoch or 0),
            viewport=(int(self.session.config.viewport[0]), int(self.session.config.viewport[1])),
            paused_at=time.monotonic(),
            deadline_at=coerce_utc(task.deadline),
            page_url=str(self.session.page.url or ""),
        )
        self.state = state
        plane = get_control_plane()
        plane.open(
            LiveSession(
                execution_id=self.execution_id,
                human_task_id=task.id,
                step_id=step.id,
                epoch=int(state.session_epoch),
                submit=self.submit_operator_input,
                close=lambda: plane.close(task.id),
            )
        )
        try:
            resolution = await self._wait_for_resume(state, decision)
        except HumanGateError:
            self._accumulate_wait(state, self._waited_ms(state))
            self.state = None
            raise
        finally:
            plane.close(task.id)
            self.state = None
        waited_ms = self._waited_ms(state)
        self._accumulate_wait(state, waited_ms)
        # time spent waiting for a person is never charged to the step (§10.3)
        self.context.interruption.detail["human_waited_ms"] = int(waited_ms)
        if resolution == "cancelled":
            self.context.interruption.cancel = True
            return
        if resolution == "step_completed_by_human":
            raise HumanRequired(decision.mode, decision.reason, decision.resume_condition)

    @staticmethod
    def _waited_ms(state: HumanSessionState) -> int:
        return max(0, int((time.monotonic() - state.paused_at) * 1000))

    async def _wait_for_resume(self, state: HumanSessionState, decision: PauseDecision) -> str:
        """The hold: sample frames, honour operator input, watch the lease and the deadline."""
        interval_ms = max(100, int(self.settings.human_frame_interval_ms))
        last_frame = 0.0
        last_check = 0.0
        while True:
            now = time.monotonic()
            deadline = self._task_deadline(state.human_task_id)
            if deadline is not None and coerce_utc(deadline) <= utcnow():
                self._expire_task(state)
                return "expired"
            self._assert_holder(state)
            if self.context.interruption.cancel:
                self._close_task(state, status=HumanTaskStatus.CANCELLED.value, note="cancelled while awaiting a human")
                return "cancelled"
            if now - last_check >= COMMAND_POLL_SECONDS:
                last_check = now
                self._drain_persisted_commands(state)
                resolution = await self._check_resume(state, decision)
                if resolution is not None:
                    return resolution
            if self.frame_sink is not None and (now - last_frame) * 1000 >= interval_ms and self._wants_frames(state):
                last_frame = now
                await self._emit_frame(state)
            await self._drain_operator_input()
            await asyncio.sleep(0.05)

    def _wants_frames(self, state: HumanSessionState) -> bool:
        """A frame costs a screenshot, so sample only while somebody is actually watching (§12.1)."""
        return state.controller_id is not None or get_control_plane().has_subscribers(state.human_task_id)

    def _drain_persisted_commands(self, state: HumanSessionState) -> None:
        """The cross-process half of the control channel: commands another API node persisted (§13.4).

        Delivery is at-most-once by design — a command that is already in flight in this process is
        claimed here with `PROCESSING`, and a worker that dies mid-pause simply lets it expire rather
        than replaying a click onto a page that has moved on.
        """
        with self.db.session(self.tenant_id) as session:
            repo = CommandRepository(session, self.tenant_id)
            claimed: list[tuple[str, dict[str, Any]]] = []
            for row in repo.pending(self.execution_id):
                if row.command_type != CommandType.CONTROL.value or row.human_task_id != state.human_task_id:
                    continue
                if row.status != CommandStatus.PENDING.value:
                    continue
                repo.mark(row.id, status=CommandStatus.PROCESSING.value)
                claimed.append((row.id, dict(row.payload or {})))
            session.commit()
        for command_id, payload in claimed:
            payload.setdefault("command_id", command_id)
            self._pending_input.append(payload)

    # ------------------------------------------------------------- resume handling

    async def _check_resume(self, state: HumanSessionState, decision: PauseDecision) -> str | None:
        """Return a resolution once the operator asked to resume and the resume was verified."""
        status, _requested_at, assignee = self._task_state(state.human_task_id)
        state.controller_id = assignee
        if status == HumanTaskStatus.CANCELLED.value:
            self._mark_task_commands(state, result={"rejected": "task cancelled"})
            return "cancelled"
        if status != HumanTaskStatus.RESUME_REQUESTED.value:
            self._expire_stale_commands()
            return None
        verified, why, step_completed = await self._verify_resume(state, decision)
        if not verified:
            self._return_to_claimed(state, why or "the resume condition is not met yet")
            return None
        if step_completed:
            resolution = "step_completed_by_human"
        elif decision.resume_phase == ResumePhase.AFTER_ACTION.value:
            resolution = "condition_verified"
        else:
            resolution = "confirmed"
        self._complete(state, resolution=resolution, note=why)
        return resolution

    async def _verify_resume(self, state: HumanSessionState, decision: PauseDecision) -> tuple[bool, str | None, bool]:
        payload = self._resume_payload(state.human_task_id)
        step_completed = bool(payload.get("step_completed"))
        if decision.resume_condition is None or step_completed:
            # `mode=before` needs no condition: the operator's confirmation *is* the resume signal (§10.3).
            return True, None, step_completed
        return await self._evaluate_condition(decision.resume_condition)

    # ---------------------------------------------------------------- operator input

    def submit_operator_input(self, command: dict[str, Any]) -> None:
        """Called by the control gateway; validated here because only this loop may touch the page."""
        if self.state is None:
            raise ApiReject("no human session is open for this execution")
        self._pending_input.append(command)

    async def _drain_operator_input(self) -> None:
        while self._pending_input:
            await self._run_operator_input(self._pending_input.popleft())

    async def _run_operator_input(self, command: dict[str, Any]) -> None:
        """Validate against the §10.2 whitelist, forward it, and record the verdict for the sender."""
        state = self.state
        rejection = self._validate_operator_command(state, command)
        if rejection is None:
            try:
                await self._apply_operator_command(command)
            except (KeyError, TypeError, ValueError) as exc:
                rejection = f"malformed command: {exc}"
            except Exception as exc:
                rejection = f"the browser rejected the command: {exc}"
        if rejection is not None:
            self._reject(state, rejection)
        else:
            state.sequence = int(command["sequence"])
            self._audit(state, str(command.get("operation")))
        command_id = command.get("command_id")
        if command_id is not None:
            self._mark_command_status(
                str(command_id),
                status=CommandStatus.REJECTED.value if rejection else CommandStatus.PROCESSED.value,
                result={"reason": rejection[:300]} if rejection else {"forwarded": True},
            )

    @staticmethod
    def _validate_operator_command(state: HumanSessionState | None, command: dict[str, Any]) -> str | None:
        """Every refusal reason the gateway may answer with, decided before the page is touched."""
        if state is None:
            return "the human session already ended"
        operation = str(command.get("operation") or "")
        if operation not in OPERATION_WHITELIST:
            return f"operation '{operation}' is not allowed"
        sequence = command.get("sequence")
        if not isinstance(sequence, int) or sequence <= state.sequence:
            return "the command sequence is stale or missing"
        frame_id = command.get("frame_id")
        if not isinstance(frame_id, int) or frame_id != state.frame_id:
            # Acting on an out-of-date picture is how a human clicks the wrong button (§10.2).
            return "the frame reference is out of date; refresh and retry"
        if operation == "click":
            try:
                x, y = float(command["x"]), float(command["y"])
            except (KeyError, TypeError, ValueError):
                return "a click needs numeric x and y"
            width, height = state.viewport
            if not (0 <= x <= width and 0 <= y <= height):
                return "the coordinates fall outside the recorded viewport"
        elif operation == "key":
            key = str(command.get("key") or "")
            if key not in KEY_WHITELIST:
                return f"key '{key}' is not allowed"
        elif operation == "type" and not str(command.get("text") or ""):
            return "typing needs text"
        return None

    async def _apply_operator_command(self, command: dict[str, Any]) -> None:
        operation = str(command.get("operation"))
        page = self.session.page
        if operation == "click":
            await page.mouse.click(float(command["x"]), float(command["y"]))
        elif operation == "type":
            await page.keyboard.type(str(command.get("text") or "")[:4096])
        elif operation == "key":
            await page.keyboard.press(str(command["key"]))
        else:
            await page.mouse.wheel(float(command.get("dx") or 0.0), float(command.get("dy") or 0.0))

    def _reject(self, state: HumanSessionState | None, reason: str) -> None:
        if state is not None:
            state.last_rejection = reason[:300]
        log.warning("human command rejected", extra={"context": {"reason": reason, "execution_id": self.execution_id}})

    def _audit(self, state: HumanSessionState, operation: str) -> None:
        """Only the operation type, operator and time are kept — never typed content (§10.2)."""
        with self.db.session(self.tenant_id) as session:
            AuditRepository(session, self.tenant_id).append(
                operation="human.command",
                resource_type="human_task",
                resource_id=state.human_task_id,
                actor_id=state.controller_id or "unassigned",
                project_id=self.project_id,
                detail={"operation": operation, "execution_id": self.execution_id},
            )
            session.commit()

    # -------------------------------------------------------------------- frames

    async def _emit_frame(self, state: HumanSessionState) -> None:
        """Viewport sampling for the gateway; a capture fault is never a test failure (§12.1)."""
        if self.session.page.is_closed():
            raise LeaseGone("the page closed during human assistance")
        try:
            data = await self.session.page.screenshot(type="png", timeout=5_000)
        except Exception as exc:
            self._reject(state, f"frame capture failed: {exc}")
            return
        state.frame_id += 1
        state.frame_at = time.monotonic()
        state.page_url = str(self.session.page.url or "")
        deadline = self._task_deadline(state.human_task_id)
        remaining = 0 if deadline is None else max(0, int((coerce_utc(deadline) - utcnow()).total_seconds()))
        try:
            self.frame_sink(
                {
                    "execution_id": self.execution_id,
                    "human_task_id": state.human_task_id,
                    "frame_id": state.frame_id,
                    "page_url": state.page_url,
                    "viewport": list(state.viewport),
                    "remaining_seconds": remaining,
                    "image_base64": base64.b64encode(data).decode("ascii"),
                }
            )
        except Exception as exc:
            log.warning("frame sink failed", extra={"context": {"error": str(exc)}})

    # ----------------------------------------------------------------- conditions

    async def _evaluate_condition(self, condition: dict[str, Any]) -> tuple[bool, str | None]:
        parsed = Condition(**condition)
        action_context = self.executor.action_context(self.session, self.context)
        budget = LocatorBudget.for_step(step_timeout_ms=RESUME_CHECK_BUDGET_MS, vision_enabled=False)
        deadline = monotonic_ms() + RESUME_CHECK_BUDGET_MS
        try:
            satisfied, why, _ = await evaluate_condition(
                parsed, action_context, deadline_monotonic_ms=deadline, budget=budget
            )
        except StepInterrupted as exc:
            if exc.kind in ("cancelled", "lease_lost"):
                raise LeaseGone(exc.message) from exc
            return False, exc.message
        except Exception as exc:
            return False, f"the resume condition could not be evaluated: {exc}"
        return bool(satisfied), why

    # -------------------------------------------------------------------- quotas

    def _check_allowance(self) -> None:
        """§9.5 and §10.4: per-run budgets plus the share of worker slots humans may hold."""
        with self.db.session(self.tenant_id) as session:
            repo = ExecutionRepository(session, self.tenant_id)
            execution = repo.load(self.execution_id)
            if int(execution.human_tasks_used or 0) >= int(self.settings.max_human_tasks_per_run):
                raise HumanGateError(
                    ErrorCode.HUMAN_WAIT_TIMEOUT.value,
                    "this run already used its human assistance allowance",
                    detail={
                        "human_tasks_used": execution.human_tasks_used,
                        "limit": self.settings.max_human_tasks_per_run,
                    },
                )
            used_seconds = int(execution.human_ms or 0) / 1000
            if used_seconds >= float(self.settings.max_human_wait_seconds):
                raise HumanGateError(
                    ErrorCode.HUMAN_WAIT_TIMEOUT.value,
                    "the cumulative human wait budget for this run is exhausted",
                    detail={"human_wait_seconds_used": used_seconds, "limit": self.settings.max_human_wait_seconds},
                )
            slots = max(1, int(int(self.settings.worker_slots) * float(self.settings.human_slot_ratio)))
            open_tasks = HumanTaskRepository(session, self.tenant_id).open(limit=slots + 1)
            if len(open_tasks) >= slots:
                raise HumanGateError(
                    ErrorCode.QUOTA_EXCEEDED.value,
                    "all human assistance slots are busy; re-run this case once one frees",
                    detail={"human_slots": slots, "open_tasks": len(open_tasks)},
                )

    # -------------------------------------------------------------- task lifecycle

    def _open_task(self, step: Step, decision: PauseDecision):
        """One transaction: the human task row plus RUNNING -> WAIT_HUMAN (§10.3 point 1)."""
        with self.db.session(self.tenant_id) as session:
            humans = HumanTaskRepository(session, self.tenant_id)
            existing = humans.active_for(self.execution_id)
            if existing is not None and existing.step_id == step.id:
                # a re-entered step re-uses its task instead of asking a second time (§10.3)
                return existing
            task = humans.create(
                project_id=self.project_id,
                execution_id=self.execution_id,
                step_id=step.id,
                reason=decision.reason[:500],
                detail=decision.mode,
                session_epoch=int(self.context.lease_epoch),
                resume_phase=decision.resume_phase,
                resume_condition=decision.resume_condition,
                timeout_seconds=int(self.settings.human_wait_timeout_seconds),
            )
            repo = ExecutionRepository(session, self.tenant_id)
            execution = repo.load(self.execution_id, for_update=True)
            if execution.status == ExecutionStatus.RUNNING.value:
                execution = repo.transition(
                    self.execution_id,
                    to_status=ExecutionStatus.WAIT_HUMAN.value,
                    expected_epoch=int(self.context.lease_epoch),
                    event_payload={"human_task_id": task.id, "step_id": step.id, "mode": decision.mode},
                )
            repo.append_event(
                execution,
                "human.created",
                {
                    "human_task_id": task.id,
                    "step_id": step.id,
                    "mode": decision.mode,
                    "reason": decision.reason[:300],
                    "resume_phase": decision.resume_phase,
                    "deadline": task.deadline.isoformat(),
                },
            )
            repo.update_step_projection(self.execution_id, step.id, resume_phase=decision.resume_phase)
            repo.countersigned_update(
                self.execution_id,
                epoch=int(self.context.lease_epoch),
                values={"human_tasks_used": int(execution.human_tasks_used or 0) + 1},
            )
            session.commit()
            return task

    def _task_state(self, human_task_id: str) -> tuple[str, Any, str | None]:
        with self.db.session(self.tenant_id) as session:
            task = HumanTaskRepository(session, self.tenant_id).by_id(human_task_id)
            if task is None:
                raise LeaseGone("the human task disappeared")
            return str(task.status), coerce_utc(task.resume_requested_at), task.assignee_id

    def _task_deadline(self, human_task_id: str):
        with self.db.session(self.tenant_id) as session:
            task = HumanTaskRepository(session, self.tenant_id).by_id(human_task_id)
            return None if task is None else task.deadline

    def _resume_payload(self, human_task_id: str) -> dict[str, Any]:
        with self.db.session(self.tenant_id) as session:
            repo = CommandRepository(session, self.tenant_id)
            for command in repo.pending(self.execution_id):
                if command.command_type == CommandType.RESUME.value and command.human_task_id == human_task_id:
                    return dict(command.payload or {})
            return {}

    def _pause_command(self, step_id: str):
        with self.db.session(self.tenant_id) as session:
            repo = CommandRepository(session, self.tenant_id)
            for command in repo.pending(self.execution_id):
                if command.command_type != CommandType.PAUSE.value:
                    continue
                payload = dict(command.payload or {})
                if payload.get("for_step") in (None, "", step_id):
                    return command
            return None

    def _mark_command(self, command, *, result: dict[str, Any]) -> None:
        with self.db.session(self.tenant_id) as session:
            CommandRepository(session, self.tenant_id).mark(
                command.id, status=CommandStatus.PROCESSED.value, result=result
            )
            session.commit()

    def _mark_command_status(self, command_id: str, *, status: str, result: dict[str, Any]) -> None:
        """Write the verdict back so `GET /commands/{id}` can answer for both delivery paths (§13.2)."""
        with self.db.session(self.tenant_id) as session:
            CommandRepository(session, self.tenant_id).mark(command_id, status=status, result=result)
            session.commit()

    def _mark_task_commands(self, state: HumanSessionState, *, result: dict[str, Any]) -> None:
        with self.db.session(self.tenant_id) as session:
            repo = CommandRepository(session, self.tenant_id)
            for command in repo.pending(self.execution_id):
                if command.human_task_id == state.human_task_id:
                    repo.mark(command.id, status=CommandStatus.PROCESSED.value, result=result)
            session.commit()

    def _expire_stale_commands(self) -> None:
        with self.db.session(self.tenant_id) as session:
            CommandRepository(session, self.tenant_id).expire_stale()
            session.commit()

    def _return_to_claimed(self, state: HumanSessionState, why: str) -> None:
        """Resume refused: the operator keeps control and the deadline does not move (§10.3 point 5)."""
        with self.db.session(self.tenant_id) as session:
            humans = HumanTaskRepository(session, self.tenant_id)
            humans.return_to_claimed(state.human_task_id, note=why[:500])
            repo = ExecutionRepository(session, self.tenant_id)
            execution = repo.load(self.execution_id)
            repo.append_event(
                execution,
                "human.resume_rejected",
                {"human_task_id": state.human_task_id, "step_id": state.step_id, "reason": why[:300]},
            )
            session.commit()

    def _complete(self, state: HumanSessionState, *, resolution: str, note: str | None) -> None:
        """Resume transaction: task COMPLETED and the execution back to RUNNING (§10.3 point 5)."""
        with self.db.session(self.tenant_id) as session:
            humans = HumanTaskRepository(session, self.tenant_id)
            task = humans.by_id(state.human_task_id, for_update=True)
            if task is not None:
                humans.finish(task.id, status=HumanTaskStatus.COMPLETED.value, note=(note or resolution)[:500])
            commands = CommandRepository(session, self.tenant_id)
            for command in commands.pending(self.execution_id):
                if command.human_task_id == state.human_task_id:
                    commands.mark(command.id, status=CommandStatus.PROCESSED.value, result={"resolution": resolution})
            repo = ExecutionRepository(session, self.tenant_id)
            execution = repo.load(self.execution_id, for_update=True)
            if execution.status == ExecutionStatus.WAIT_HUMAN.value:
                execution = repo.transition(
                    self.execution_id,
                    to_status=ExecutionStatus.RUNNING.value,
                    expected_epoch=int(self.context.lease_epoch),
                    event_payload={"human_task_id": state.human_task_id, "resolution": resolution},
                )
            repo.append_event(
                execution,
                "human.resumed",
                {
                    "human_task_id": state.human_task_id,
                    "step_id": state.step_id,
                    "resolution": resolution,
                    "human_ms": self._waited_ms(state),
                    "controller_id": state.controller_id,
                },
            )
            session.commit()

    def _close_task(self, state: HumanSessionState, *, status: str, note: str) -> None:
        with self.db.session(self.tenant_id) as session:
            humans = HumanTaskRepository(session, self.tenant_id)
            task = humans.by_id(state.human_task_id, for_update=True)
            if task is not None:
                humans.finish(task.id, status=status, note=note)
            session.commit()

    def _expire_task(self, state: HumanSessionState) -> None:
        """Deadline with nobody finishing: the run ends TIMED_OUT/HUMAN_WAIT_TIMEOUT (§10.3 point 6)."""
        self._close_task(state, status=HumanTaskStatus.EXPIRED.value, note="the worker observed the deadline elapse")
        raise HumanGateError(
            ErrorCode.HUMAN_WAIT_TIMEOUT.value,
            "the human task deadline elapsed",
            outcome=Outcome.TIMED_OUT.value,
            detail={"human_task_id": state.human_task_id, "step_id": state.step_id},
        )

    def _accumulate_wait(self, state: HumanSessionState, waited_ms: int) -> None:
        if waited_ms <= 0:
            return
        with self.db.session(self.tenant_id) as session:
            repo = ExecutionRepository(session, self.tenant_id)
            execution = repo.load(self.execution_id)
            repo.countersigned_update(
                self.execution_id,
                epoch=int(self.context.lease_epoch),
                values={"human_ms": int(execution.human_ms or 0) + int(waited_ms)},
            )

    def _assert_holder(self, state: HumanSessionState) -> None:
        """The holder must still hold: same epoch, live status, no cancel intent (§9.4)."""
        if self.context.interruption.lease_lost:
            raise LeaseGone("the lease heartbeat reported this worker stale")
        with self.db.session(self.tenant_id) as session:
            repo = ExecutionRepository(session, self.tenant_id)
            if repo.epoch_is_current(self.execution_id, epoch=int(self.context.lease_epoch)):
                return
            execution = repo.load(self.execution_id)
        if execution.cancel_requested_at is not None:
            # §9.5: cancel wins; the step loop turns this into a CANCELLED conclusion.
            self.context.interruption.cancel = True
            return
        raise LeaseGone("the execution lease is no longer current")

    async def _suspend_sensitive_capture(self) -> None:
        """§10.4: stop raw tracing and revoke video before any control ticket is issued."""
        session = self.session
        if session.trace_started:
            try:
                await session.context.tracing.stop()
                session.trace_started = False
            except Exception as exc:
                raise HumanGateError(
                    ErrorCode.HUMAN_SESSION_LOST.value,
                    "raw tracing could not be stopped before human input, so the pause was refused",
                    detail={"error": str(exc)[:200]},
                ) from exc
        if session.config.record_video:
            with self.db.session(self.tenant_id) as scope:
                ArtifactRepository(scope, self.tenant_id).forbid_publish_for(
                    execution_id=self.execution_id,
                    kinds=("VIDEO",),
                    reason="human assistance began (§10.4)",
                )
                scope.commit()
