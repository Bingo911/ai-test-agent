"""Attachment intake: store, then scan, then allow (§14.3).

A test case can only upload a file the platform has already accepted, so an attachment row is the
single gate between "someone posted bytes" and "the executor may read a path". The original filename
is never used as a path: the object key is generated, and the executor sees a normalised temporary
file path it derives from the id.

The scanner here is signature-based and local. It is deliberately a seam: a deployment with an
antivirus service replaces `scan_bytes` and keeps every other rule.
"""

from __future__ import annotations

import hashlib
from typing import Any

from sqlalchemy import select

from ..config import Settings, get_settings
from ..db.base import Database, get_database
from ..db.models import Attachment
from ..domain.enums import ScanStatus
from ..domain.errors import ApiError, ErrorCode
from ..observability import get_logger
from ..repositories.resources import AttachmentRepository
from .object_store import attachment_object_key, get_object_store, safe_filename

log = get_logger(__name__)

#: What a browser test legitimately needs to hand to a file input.
ALLOWED_MEDIA_TYPES = {
    "text/plain",
    "text/csv",
    "text/xml",
    "text/html",
    "application/json",
    "application/xml",
    "application/pdf",
    "application/rtf",
    "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "image/png",
    "image/jpeg",
    "image/gif",
    "image/webp",
    "application/zip",
}

#: Macros can execute on the target site, so an office container is only accepted without them.
FORBIDDEN_SUFFIXES = (
    ".docm",
    ".xlsm",
    ".xltm",
    ".pptm",
    ".exe",
    ".dll",
    ".so",
    ".jar",
    ".js",
    ".vbs",
    ".ps1",
    ".bat",
    ".cmd",
    ".scr",
)

_SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"EICAR-STANDARD-ANTIVIRUS-TEST-FILE", "EICAR test signature"),
    (b"%!PS-ADMIN", "PostScript operator stream"),
)


def scan_bytes(data: bytes) -> tuple[ScanStatus, str | None]:
    """Return CLEAN or INFECTED; an unavailable scanner is an ERROR, never a silent pass."""
    for needle, label in _SIGNATURES:
        if needle in data:
            return ScanStatus.INFECTED.value, label
    return ScanStatus.CLEAN.value, None


class AttachmentService:
    def __init__(self, settings: Settings | None = None, *, database: Database | None = None) -> None:
        self.settings = settings or get_settings()
        self._database = database

    @property
    def database(self) -> Database:
        """The pool this service was handed; the process default only serves standalone callers."""
        return self._database if self._database is not None else get_database()

    def store(
        self,
        *,
        tenant_id: str,
        project_id: str,
        filename: str,
        media_type: str,
        data: bytes,
        created_by: str | None,
    ) -> dict[str, Any]:
        """Everything up front: size, MIME, name, digest — the scan result is stored with the row."""
        if len(data) == 0:
            raise ApiError(ErrorCode.VALIDATION_ERROR, "An attachment cannot be empty")
        if len(data) > self.settings.max_attachment_bytes:
            raise ApiError(
                ErrorCode.PAYLOAD_TOO_LARGE,
                f"Attachments are limited to {self.settings.max_attachment_bytes} bytes",
                details={"size": len(data)},
            )
        declared = (media_type or "application/octet-stream").split(";")[0].strip().lower()
        if declared not in ALLOWED_MEDIA_TYPES:
            raise ApiError(
                ErrorCode.SEMANTIC_ERROR,
                f"Media type '{declared}' is not accepted for attachments",
                details={"allowed": sorted(ALLOWED_MEDIA_TYPES)},
            )
        name = safe_filename(filename)
        if any(name.lower().endswith(suffix) for suffix in FORBIDDEN_SUFFIXES):
            raise ApiError(
                ErrorCode.SEMANTIC_ERROR,
                "That file type is never accepted, because it can execute on the target site",
                details={"filename": name[:120]},
            )

        digest = "sha256:" + hashlib.sha256(data).hexdigest()
        status, detail = scan_bytes(data)
        if status == ScanStatus.INFECTED.value:
            # The bytes are refused before they reach the store: quarantine is not a place to keep it.
            raise ApiError(ErrorCode.SEMANTIC_ERROR, f"The attachment failed the malware scan: {detail}")

        with self.database.session(tenant_id) as session:
            repos = AttachmentRepository(session, tenant_id)
            duplicate = session.scalar(
                select(Attachment).where(
                    Attachment.tenant_id == tenant_id, Attachment.project_id == project_id, Attachment.digest == digest
                )
            )
            if duplicate is not None:
                # Same bytes in the same project: one stored object, one id, one scan verdict.
                return attachment_payload(duplicate)

            row = repos.record(
                project_id=project_id,
                filename=name,
                media_type=declared,
                size=len(data),
                digest=digest,
                object_key=attachment_object_key(tenant_id, project_id, "staged", name),
                created_by=created_by,
            )
            object_key = attachment_object_key(tenant_id, project_id, row.id, name)
            get_object_store(self.settings).put_bytes(object_key, data, declared)
            # The key is derived from the row id, so it is only final once the id exists (§14.3).
            row.object_key = object_key
            repos.set_scan(row, status=ScanStatus(status), detail=detail)
            session.flush()
            session.commit()
            return attachment_payload(row)

    def get(self, *, tenant_id: str, attachment_id: str) -> dict[str, Any]:
        with self.database.session(tenant_id) as session:
            row = AttachmentRepository(session, tenant_id).by_id(attachment_id)
            if row is None:
                raise ApiError(ErrorCode.NOT_FOUND, "Attachment not found in this tenant")
            return attachment_payload(row)

    def list(self, *, tenant_id: str, project_id: str) -> list[dict[str, Any]]:
        with self.database.session(tenant_id) as session:
            return [attachment_payload(row) for row in AttachmentRepository(session, tenant_id).list(project_id)]


def attachment_payload(row: Any) -> dict[str, Any]:
    return {
        "attachment_id": row.id,
        "project_id": row.project_id,
        "filename": row.filename,
        "media_type": row.media_type,
        "size": int(row.size),
        "digest": row.digest,
        "scan_status": row.scan_status,
        "scan_detail": row.scan_detail,
        "ready": row.scan_status == ScanStatus.CLEAN.value,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }
