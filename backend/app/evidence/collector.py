"""Evidence collection for a browser session (§12.1).

Two rules drive the whole module:

* a capture failure must never hide the failure it was trying to document — errors are appended to
  `capture_errors` and the run's verdict stays untouched (`artifact_status=PARTIAL`);
* everything leaving the browser is treated as untrusted target content and passes through
  `redaction` before it reaches a sink, with declared secret values masked on the way out.
"""

from __future__ import annotations

import contextlib
import json
from collections import deque
from collections.abc import Iterable
from datetime import datetime, timezone
from typing import Any

from ..executors.contracts import EvidenceSink
from ..observability import get_logger
from .redaction import IN_PAGE_DOM_SCRUB, mask_text, redact_url, sanitize_dom

log = get_logger(__name__)

#: Painted over password/OTP-ish controls so a screenshot cannot carry the value (§12.1, §14.3).
_FIELD_MASK_JS = """
() => {
  const selector = [
    'input[type=password]', 'input[autocomplete*="one-time-code"]', 'input[inputmode=numeric][maxlength="6"]'
  ].join(',');
  const nodes = [...document.querySelectorAll(selector)];
  window.__aitaMasks = [];
  for (const node of nodes) {
    const rect = node.getBoundingClientRect();
    if (rect.width <= 0 || rect.height <= 0) continue;
    const overlay = document.createElement('div');
    overlay.setAttribute('data-aita-mask-overlay', '1');
    overlay.style.cssText = `position:fixed;left:${Math.floor(rect.left)}px;top:${Math.floor(rect.top)}px;` +
      `width:${Math.ceil(rect.width)}px;height:${Math.ceil(rect.height)}px;background:#111;z-index:2147483647;`;
    document.body.appendChild(overlay);
    window.__aitaMasks.push(overlay);
  }
  return window.__aitaMasks.length;
}
"""

_REMOVE_MASKS_JS = """
() => {
  for (const node of (window.__aitaMasks || [])) node.remove();
  window.__aitaMasks = [];
  return true;
}
"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class BoundedRing:
    """Fixed-capacity event store; overflow is counted and reported instead of silently dropped."""

    def __init__(self, capacity: int) -> None:
        self.capacity = max(1, capacity)
        self._items: deque[dict[str, Any]] = deque(maxlen=self.capacity)
        self.dropped = 0

    def add(self, item: dict[str, Any]) -> None:
        if len(self._items) == self.capacity:
            self.dropped += 1
        self._items.append(item)

    def __len__(self) -> int:
        return len(self._items)

    @property
    def truncated(self) -> bool:
        return self.dropped > 0

    def items(self) -> list[dict[str, Any]]:
        return list(self._items)


class EvidenceCollector:
    """Session-scoped capture: screenshots, DOM, console and network rings, trace and video."""

    def __init__(
        self,
        sink: EvidenceSink | None,
        *,
        console_capacity: int = 2000,
        network_capacity: int = 2000,
        dom_max_bytes: int = 2 * 1024 * 1024,
        disk_budget_bytes: int = 200 * 1024 * 1024,
        screenshot_policy: str = "on_failure",
        full_page_allowed: bool = False,
        mask_fields: bool = True,
        known_secrets: Iterable[str] = (),
        viewport: tuple[int, int] | None = None,
    ) -> None:
        self.sink = sink
        self.console = BoundedRing(console_capacity)
        self.network = BoundedRing(network_capacity)
        self.dialogs: list[dict[str, Any]] = []
        self.dom_max_bytes = dom_max_bytes
        self.disk_budget_bytes = disk_budget_bytes
        self.screenshot_policy = screenshot_policy
        self.full_page_allowed = full_page_allowed
        self.mask_fields = mask_fields
        self.known_secrets = tuple(item for item in known_secrets if item)
        self.viewport = viewport
        #: The bytes of the most recent screenshot, so the visual fallback can send them to a model.
        self.last_screenshot_bytes: bytes | None = None
        #: Set when the password/OTP overlay failed to apply, which blocks sensitive captures.
        self.mask_failed = False
        self.used_bytes = 0
        self.capture_errors: list[str] = []
        self.disk_exceeded = False
        self.artifact_ids: list[str] = []

    # ------------------------------------------------------------------ listeners

    def wire_page(self, page: Any) -> None:
        """Attach console/dialog listeners. Call once, right after the page is created."""
        page.on("console", self._on_console)
        page.on("pageerror", self._on_page_error)
        page.on("dialog", self._on_dialog)

    def wire_context(self, context: Any) -> None:
        context.on("request", self._on_request)
        context.on("requestfailed", self._on_request_failed)
        context.on("response", self._on_response)

    def _on_console(self, message: Any) -> None:
        try:
            self.console.add(
                {
                    "type": getattr(message, "type", "log"),
                    "text": mask_text(str(message.text), known_secrets=self.known_secrets),
                    "at": _now_iso(),
                    "location": _console_location(message),
                }
            )
        except Exception as exc:  # pragma: no cover - listener must never break the page
            self.note_capture_error(f"console listener: {exc}")

    def _on_page_error(self, error: Any) -> None:
        self.console.add(
            {
                "type": "pageerror",
                "text": mask_text(str(error).split("\n")[0], known_secrets=self.known_secrets),
                "at": _now_iso(),
            }
        )

    async def _on_dialog(self, dialog: Any) -> None:
        """V1 supports exactly one page and no scripted dialogs (§2.2). Record, then dismiss.

        Playwright awaits coroutine handlers, which is what `dismiss()` needs — calling it from a
        sync handler would leave the modal open and stall the step.
        """
        entry = {
            "type": getattr(dialog, "type", "alert"),
            "message": mask_text(str(getattr(dialog, "message", "")), known_secrets=self.known_secrets),
            "at": _now_iso(),
            "handled": "dismiss",
        }
        self.dialogs.append(entry)
        try:
            await dialog.dismiss()
        except Exception as exc:  # pragma: no cover - best-effort dismissal
            entry["handled"] = "failed"
            self.note_capture_error(f"dialog dismiss: {exc}")

    def _on_request(self, request: Any) -> None:
        self.network.add(
            {
                "event": "request",
                "method": getattr(request, "method", "GET"),
                "url": redact_url(str(getattr(request, "url", "")), known_secrets=self.known_secrets),
                "resource_type": _resource_type(request),
                "at": _now_iso(),
            }
        )

    def _on_response(self, response: Any) -> None:
        self.network.add(
            {
                "event": "response",
                "status": getattr(response, "status", None),
                "url": redact_url(str(getattr(response, "url", "")), known_secrets=self.known_secrets),
                "at": _now_iso(),
            }
        )

    def _on_request_failed(self, request: Any) -> None:
        self.network.add(
            {
                "event": "failed",
                "method": getattr(request, "method", "GET"),
                "url": redact_url(str(getattr(request, "url", "")), known_secrets=self.known_secrets),
                "error": mask_text(str(getattr(request, "failure", None) or ""), known_secrets=self.known_secrets),
                "at": _now_iso(),
            }
        )

    # -------------------------------------------------------------------- capture

    def budget_available(self, expected_bytes: int = 0) -> bool:
        if self.disk_exceeded:
            return False
        if self.used_bytes + expected_bytes > self.disk_budget_bytes:
            self.disk_exceeded = True
            self.mark_truncated(f"evidence disk budget of {self.disk_budget_bytes} bytes exceeded")
            return False
        return True

    def note_capture_error(self, message: str) -> None:
        """Append, never raise: the original failure must stay the reported cause (§12.1)."""
        self.capture_errors.append(message)
        log.warning("evidence capture failed", extra={"context": {"error": message}})

    def mark_truncated(self, reason: str) -> None:
        if self.sink is not None:
            try:
                self.sink.mark_truncated(reason)
            except Exception as exc:  # pragma: no cover - reporting must not cascade
                self.note_capture_error(f"truncation report: {exc}")

    def _account(self, data: bytes) -> int:
        self.used_bytes += len(data)
        return len(data)

    async def screenshot(
        self,
        page: Any,
        *,
        name: str,
        step_id: str | None = None,
        full_page: bool = False,
        sensitive: bool = False,
    ) -> str | None:
        if self.sink is None or self.screenshot_policy == "off":
            return None
        if not self.budget_available():
            return None
        wanted_full_page = full_page and self.full_page_allowed
        self.mask_failed = False
        masked = await self._apply_field_masks(page) if self.mask_fields else 0
        if masked < 0 and sensitive:
            # Without verified masking a screenshot can carry a password or OTP, and §12.1 says an
            # un-purgeable file must not be stored rather than stored as-is.
            self.note_capture_error(f"screenshot '{name}' skipped: secret fields could not be masked")
            return None
        try:
            try:
                data = await page.screenshot(type="png", full_page=wanted_full_page, timeout=15_000)
            except Exception as exc:
                self.note_capture_error(f"screenshot '{name}': {exc}")
                return None
            self.last_screenshot_bytes = data
            return self._put_bytes(
                kind="screenshot",
                name=name,
                data=data,
                media_type="image/png",
                step_id=step_id,
                sensitive=sensitive,
            )
        finally:
            if masked > 0:
                # pragma: no cover - the page may be navigating away, and the shot is already stored
                with contextlib.suppress(Exception):
                    await page.evaluate(_REMOVE_MASKS_JS)

    async def _apply_field_masks(self, page: Any) -> int:
        try:
            return int(await page.evaluate(_FIELD_MASK_JS) or 0)
        except Exception as exc:
            # Overlaying failed: the caller decides whether an unmasked shot is acceptable.
            self.mask_failed = True
            self.note_capture_error(f"field masking: {exc}")
            return -1

    def vision_capture_allowed(self) -> bool:
        """§8.2: never ship a screenshot to a model when secret fields could not be masked."""
        return not self.mask_failed

    async def dom_snapshot(self, page: Any, *, step_id: str | None = None, name: str = "dom") -> str | None:
        if self.sink is None or not self.budget_available():
            return None
        try:
            html = await page.evaluate(IN_PAGE_DOM_SCRUB)
        except Exception as exc:
            self.note_capture_error(f"dom scrub: {exc}")
            try:
                html = await page.content()
            except Exception as inner:
                self.note_capture_error(f"dom content: {inner}")
                return None
        cleaned = sanitize_dom(str(html), known_secrets=self.known_secrets, max_bytes=self.dom_max_bytes)
        return self._put_bytes(
            kind="dom", name=name, data=cleaned.encode("utf-8"), media_type="text/plain; charset=utf-8", step_id=step_id
        )

    def put_failure_detail(
        self,
        *,
        step_id: str,
        error_code: str,
        message: str,
        locator_attempts: list[dict[str, Any]],
        extra: dict[str, Any] | None = None,
    ) -> str | None:
        """Structured per-step failure record: what was attempted and why it did not resolve."""
        payload = {
            "step_id": step_id,
            "error_code": error_code,
            "message": mask_text(message or "", known_secrets=self.known_secrets),
            "locator_attempts": [
                {
                    **attempt,
                    "selector": redact_url(str(attempt.get("selector", "")), known_secrets=self.known_secrets)
                    if "selector" in attempt
                    else None,
                }
                for attempt in locator_attempts
            ],
            "extra": extra or {},
            "at": _now_iso(),
        }
        data = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        return self._put_bytes(
            kind="locator_diagnostic",
            name=f"locator-{step_id}",
            data=data,
            media_type="application/json",
            step_id=step_id,
        )

    def flush_console_and_network(self) -> list[str]:
        ids: list[str] = []
        if self.sink is None:
            return ids
        for ring, kind, name in ((self.console, "console", "console.log"), (self.network, "network", "network.json")):
            if not len(ring):
                continue
            payload = {
                "truncated": ring.truncated,
                "dropped_entries": ring.dropped,
                "entries": ring.items(),
            }
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            artifact_id = self._put_bytes(kind=kind, name=name, data=data, media_type="application/json")
            if artifact_id:
                ids.append(artifact_id)
        return ids

    def _put_bytes(
        self,
        *,
        kind: str,
        name: str,
        data: bytes,
        media_type: str,
        step_id: str | None = None,
        sensitive: bool = False,
    ) -> str | None:
        if self.sink is None:
            return None
        try:
            artifact_id = self.sink.put_bytes(
                kind=kind, name=name, data=data, media_type=media_type, step_id=step_id, sensitive=sensitive
            )
        except Exception as exc:
            self.note_capture_error(f"store {kind}: {exc}")
            return None
        if artifact_id:
            self._account(data)
            self.artifact_ids.append(artifact_id)
        return artifact_id

    def put_file(
        self,
        *,
        kind: str,
        name: str,
        path: Any,
        media_type: str,
        step_id: str | None = None,
        sensitive: bool = False,
        size_hint: int = 0,
    ) -> str | None:
        if self.sink is None or not self.budget_available(size_hint):
            return None
        try:
            artifact_id = self.sink.put_file(
                kind=kind, name=name, path=path, media_type=media_type, step_id=step_id, sensitive=sensitive
            )
        except Exception as exc:
            self.note_capture_error(f"store {kind} file: {exc}")
            return None
        if artifact_id:
            self.used_bytes += size_hint
            self.artifact_ids.append(artifact_id)
        return artifact_id

    def bundle(self) -> dict[str, Any]:
        """Snapshot handed to the worker so it can register `artifact_status` (§12.2)."""
        return {
            "console_entries": len(self.console),
            "console_truncated": self.console.truncated,
            "network_entries": len(self.network),
            "network_truncated": self.network.truncated,
            "dialogs": self.dialogs,
            "capture_errors": list(self.capture_errors),
            "used_bytes": self.used_bytes,
            "disk_exceeded": self.disk_exceeded,
            "artifact_ids": list(self.artifact_ids),
            "partial": bool(self.capture_errors)
            or self.console.truncated
            or self.network.truncated
            or self.disk_exceeded,
        }


def _console_location(message: Any) -> dict[str, Any] | None:
    location = getattr(message, "location", None)
    if not isinstance(location, dict):
        return None
    return {
        "url": redact_url(str(location.get("url", ""))),
        "line": location.get("lineNumber"),
        "column": location.get("columnNumber"),
    }


def _resource_type(request: Any) -> str | None:
    return getattr(request, "resource_type", None)
