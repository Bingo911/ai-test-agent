"""Durable evidence sink: object store payload plus the indexed artifact row (§12.1, §11.4).

Writing evidence must never change the test conclusion, so every method swallows its own failures
and reports them through `note_capture_error` instead of raising into the executor.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from ..config import Settings
from ..db.base import get_database, new_id
from ..domain.enums import ArtifactKind, Sensitivity, UploadStatus
from ..observability import get_logger
from ..services.object_store import ObjectStore, execution_object_key
from .redaction import mask_text

log = get_logger(__name__)

KIND_TO_ARTIFACT: dict[str, ArtifactKind] = {
    "screenshot": ArtifactKind.SCREENSHOT,
    "dom": ArtifactKind.DOM,
    "console": ArtifactKind.CONSOLE,
    "network": ArtifactKind.NETWORK,
    "trace": ArtifactKind.TRACE,
    "video": ArtifactKind.VIDEO,
    "locator_diagnostic": ArtifactKind.LOG,
    "log": ArtifactKind.LOG,
}

EXTENSION: dict[str, str] = {
    "screenshot": ".png",
    "dom": ".txt",
    "console": ".json",
    "network": ".json",
    "trace": ".zip",
    "video": ".webm",
    "locator_diagnostic": ".json",
    "log": ".txt",
}

MEDIA_TYPE: dict[str, str] = {
    "screenshot": "image/png",
    "dom": "text/plain; charset=utf-8",
    "trace": "application/zip",
    "video": "video/webm",
    "log": "text/plain; charset=utf-8",
}

#: Kinds that must never be published straight out of a sensitive run (§10.4).
RAW_SESSION_CAPTURE = ("trace", "video")


class DatabaseEvidenceSink:
    """One sink per execution. It opens its own session so browser work and index writes stay apart."""

    def __init__(
        self,
        *,
        tenant_id: str,
        project_id: str,
        execution_id: str,
        settings: Settings,
        store: ObjectStore,
        evidence_mode: str = "NORMAL",
        known_secrets: Sequence[str] = (),
        event_hook: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> None:
        self.tenant_id = tenant_id
        self.project_id = project_id
        self.execution_id = execution_id
        self.settings = settings
        self.store = store
        self.evidence_mode = evidence_mode.upper()
        self.known_secrets = tuple(known_secrets)
        self.event_hook = event_hook
        self.truncations: list[str] = []
        self.capture_failures: list[str] = []
        self.used_bytes = 0

    @property
    def sensitive_mode(self) -> bool:
        return self.evidence_mode == Sensitivity.SENSITIVE.value

    def note_capture_error(self, message: str) -> None:
        self.capture_failures.append(message)
        log.warning("evidence write failed", extra={"context": {"execution_id": self.execution_id, "error": message}})

    def mark_truncated(self, reason: str) -> None:
        self.truncations.append(reason)
        log.warning("evidence truncated", extra={"context": {"execution_id": self.execution_id, "reason": reason}})

    def budget_exhausted(self, upcoming: int) -> bool:
        """Stop optional capture once this execution's evidence exceeds the disk budget (§12.1)."""
        if self.used_bytes + upcoming <= self.settings.evidence_disk_budget_bytes:
            return False
        self.mark_truncated(f"evidence disk budget of {self.settings.evidence_disk_budget_bytes} bytes exceeded")
        return True

    def put_bytes(
        self,
        *,
        kind: str,
        name: str,
        data: bytes,
        media_type: str,
        step_id: str | None = None,
        sensitive: bool = False,
    ) -> str | None:
        if self.budget_exhausted(len(data)):
            return None
        return self._write(
            kind=kind,
            name=name,
            step_id=step_id,
            sensitive=sensitive or self.sensitive_mode,
            media_type=media_type,
            write=lambda key: self.store.put_bytes(key, data, media_type),
        )

    def put_file(
        self,
        *,
        kind: str,
        name: str,
        path: Any,
        media_type: str,
        step_id: str | None = None,
        sensitive: bool = False,
    ) -> str | None:
        source = Path(path)
        if not source.is_file():
            self.note_capture_error(f"{kind}: '{name}' source file missing")
            return None
        if self.budget_exhausted(source.stat().st_size):
            return None
        return self._write(
            kind=kind,
            name=name,
            step_id=step_id,
            sensitive=sensitive or self.sensitive_mode,
            media_type=media_type,
            write=lambda key: self.store.put_file(key, source, media_type),
        )

    def record_console(self, entries: Sequence[dict[str, Any]], *, step_id: str | None = None) -> str | None:
        return self._record_ring("console", entries, step_id=step_id)

    def record_network(self, entries: Sequence[dict[str, Any]], *, step_id: str | None = None) -> str | None:
        return self._record_ring("network", entries, step_id=step_id)

    def _record_ring(self, kind: str, entries: Sequence[dict[str, Any]], *, step_id: str | None) -> str | None:
        if not entries:
            return None
        limit = self.settings.dom_evidence_max_bytes
        encoded = [
            json.dumps(_scrub(entry, self.known_secrets), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            for entry in entries
        ]
        selected: list[bytes] = []
        size = 2
        for entry in reversed(encoded):
            upcoming = len(entry) + bool(selected)
            if size + upcoming <= limit:
                selected.append(entry)
                size += upcoming
        if len(selected) != len(encoded):
            self.mark_truncated(f"{kind} index exceeded {limit} bytes")
        payload = b"[" + b",".join(reversed(selected)) + b"]"
        return self.put_bytes(
            kind=kind,
            name=f"{kind}.json",
            data=payload,
            media_type="application/json",
            step_id=step_id,
            sensitive=self.sensitive_mode,
        )

    def _write(
        self,
        *,
        kind: str,
        name: str,
        step_id: str | None,
        sensitive: bool,
        media_type: str,
        write: Callable[[str], Any],
    ) -> str | None:
        from ..repositories.artifacts import ArtifactRepository

        object_key = self._object_key(kind)
        try:
            stored = write(object_key)
        except Exception as exc:
            self.note_capture_error(f"{kind}: {exc}")
            return None
        sensitivity = Sensitivity.SENSITIVE.value if sensitive else Sensitivity.NORMAL.value
        with get_database().session(self.tenant_id) as session:
            row = ArtifactRepository(session, self.tenant_id).record(
                project_id=self.project_id,
                execution_id=self.execution_id,
                step_id=step_id,
                kind=KIND_TO_ARTIFACT.get(kind, ArtifactKind.LOG),
                name=name or f"{kind}{EXTENSION.get(kind, '')}",
                object_key=object_key,
                media_type=media_type or MEDIA_TYPE.get(kind, "application/json"),
                sensitivity=sensitivity,
                size=int(stored.size or 0),
                sha256=stored.sha256,
                metadata={"original_name": name} if name else {},
                publish_allowed=not (sensitive and kind in RAW_SESSION_CAPTURE),
                upload_status=UploadStatus.READY,
                retention_days=self._retention_days(sensitivity),
            )
            artifact_id = row.id
        self.used_bytes += int(stored.size or 0)
        self._publish(artifact_id=artifact_id, kind=kind, step_id=step_id, size=int(stored.size or 0))
        return artifact_id

    def _retention_days(self, sensitivity: str) -> int:
        if sensitivity == Sensitivity.SENSITIVE.value:
            return self.settings.retention_audit_days
        return self.settings.retention_failure_days

    def _object_key(self, kind: str) -> str:
        return execution_object_key(
            self.tenant_id, self.project_id, self.execution_id, new_id(), EXTENSION.get(kind, ".bin")
        )

    def _publish(self, *, artifact_id: str, kind: str, step_id: str | None, size: int) -> None:
        if self.event_hook is None:
            return
        try:
            self.event_hook(
                "artifact.ready",
                {"artifact_id": artifact_id, "kind": kind, "step_id": step_id, "size": size},
            )
        except Exception as exc:  # pragma: no cover - reporting must not cascade into capture
            log.info("artifact event suppressed", extra={"context": {"error": str(exc)}})


def _scrub(entry: dict[str, Any], known_secrets: Sequence[str]) -> dict[str, Any]:
    cleaned = dict(entry)
    for key, value in list(cleaned.items()):
        if isinstance(value, str):
            cleaned[key] = mask_text(value, known_secrets=known_secrets)
    return cleaned
