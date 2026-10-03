"""The opaque page cursor every MCP list tool hands back, and the position it decodes to (§11).

A cursor is a *bookmark*, not a permission: it says where the previous page stopped, and every page is
re-authorised from scratch against the caller's tenant, project and policy. It carries a version, the
resource kind, the tenant/project it was minted for, a digest of the filters that produced the page and
the last keyset position - never a secret, never case content, never a user identity.

Decoding is a match-or-refuse check rather than a parse. A cursor from another query, another project or
another filter set describes a page this call never asked for, and continuing from it would answer page
3 of a question the client did not ask. That is a caller error with a clear fix (start again from the
first page), so it is refused as `VALIDATION_ERROR` instead of silently re-anchoring.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from ..db.base import coerce_utc
from .errors import AdapterCode, NextAction, ToolFailure

CURSOR_VERSION = 1
#: §6.6 - the wire bound on a cursor argument. Encoding past it is this build's fault, not the client's.
MAX_CURSOR_CHARS = 1024

#: Which keyset a page was taken from. A cursor is only ever replayable against the same kind, because
#: two kinds can share a tenant and a filter digest while meaning completely different positions.
KIND_TENANTS = "tenants"
KIND_PROJECTS = "projects"
KIND_ENVIRONMENTS = "environments"
KIND_CASES = "cases"
KIND_STEPS = "steps"
KIND_DIAGNOSTICS = "diagnostics"
CURSOR_KINDS = frozenset(
    {KIND_TENANTS, KIND_PROJECTS, KIND_ENVIRONMENTS, KIND_CASES, KIND_STEPS, KIND_DIAGNOSTICS}
)


@dataclass(frozen=True)
class Position:
    """Where the previous page ended: the sort key and the tiebreaker id, as this query interprets them."""

    key: str
    row_id: str

    def as_timestamp(self) -> datetime:
        """The key as an aware UTC datetime, for the `created_at DESC, id DESC` pages."""
        try:
            moment = coerce_utc(datetime.fromisoformat(self.key))
        except ValueError as exc:
            raise _stale() from exc
        if moment is None:  # pragma: no cover - `coerce_utc` only answers None for None
            raise _stale()
        return moment

    def as_integer(self) -> int:
        """The key as a number, for `step_no ASC` and the diagnostic index."""
        try:
            return int(self.key)
        except ValueError as exc:
            raise _stale() from exc


def filter_digest(**filters: Any) -> str:
    """A short digest of the query's filters, so a changed filter cannot continue an old page.

    The page size is deliberately not a filter: asking for 50 rows after 20 is the same question.
    """
    canonical = json.dumps(filters, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def encode(
    *,
    kind: str,
    tenant_id: str,
    filters: str,
    position: Position,
    project_id: str | None = None,
) -> str:
    """Render a page marker. `filters` is the caller's `filter_digest(...)`, never the raw values."""
    if kind not in CURSOR_KINDS:
        raise ValueError(f"unknown cursor kind: {kind}")
    payload: dict[str, Any] = {"v": CURSOR_VERSION, "k": kind, "t": tenant_id, "f": filters}
    if project_id:
        payload["j"] = project_id
    payload["p"] = [position.key, position.row_id]
    text = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    token = base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii").rstrip("=")
    if len(token) > MAX_CURSOR_CHARS:
        # Impossible with the fixed short fields above, and still worth a clear internal answer rather
        # than sending a cursor the client's own schema bound will reject.
        raise ToolFailure(AdapterCode.INTERNAL, "The page marker this query produced is too long")
    return token


def decode(
    value: str,
    *,
    kind: str,
    tenant_id: str,
    filters: str,
    project_id: str | None = None,
) -> Position:
    """The position a previous page of *this exact query* ended at, or a refusal to continue."""
    if len(value) > MAX_CURSOR_CHARS:
        raise ToolFailure(
            AdapterCode.VALIDATION_ERROR,
            "The cursor is too long",
            next_action=NextAction.narrow_request,
        )
    payload = _read(value)
    if (
        payload.get("v") != CURSOR_VERSION
        or payload.get("k") != kind
        or payload.get("t") != tenant_id
        or payload.get("f") != filters
        or payload.get("j") != (project_id or None)
    ):
        raise _stale()
    point = payload.get("p")
    if not isinstance(point, list) or len(point) != 2 or not all(isinstance(part, str) for part in point):
        raise _stale()
    if not point[0] or not point[1]:
        raise _stale()
    return Position(key=point[0], row_id=point[1])


def _read(value: str) -> dict[str, Any]:
    """Parse the token, answering a refusal rather than whichever exception the bytes happen to raise."""
    try:
        padded = value + "=" * (-len(value) % 4)
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, binascii.Error, ValueError, AttributeError):
        raise _stale() from None
    return parsed if isinstance(parsed, dict) else {}


def _stale() -> ToolFailure:
    return ToolFailure(
        AdapterCode.VALIDATION_ERROR,
        "This cursor is not for this query. Read the first page again.",
        details={"reason": "cursor_does_not_match"},
        next_action=NextAction.narrow_request,
    )
