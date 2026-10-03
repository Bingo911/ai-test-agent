"""Atomic business commands: one key, one transaction, one answer to replay (§9.2, §9.3).

`api/deps.idempotent` stays exactly as it is for the five legacy routes (§9.2.3). This module is the
other half of the split: the commands REST and MCP share, where the reservation, the business writes,
the Outbox row and the audit entry are one transaction that either all exist or none do. That is what
lets a replay be a read instead of a second execution, and it is why the helper takes a unit of work
rather than opening a session of its own.

Two consequences are worth stating plainly:

* A command that is still running has no visible record, because nothing commits until it finishes. A
  duplicate that arrives meanwhile waits on the advisory lock and then replays the winner, so the
  protection does not depend on seeing a half-written row.
* A stored answer is the business DTO, never a protocol envelope, a download ticket or a request id
  (§9.4). Each adapter projects it again with today's whitelist and its own correlation id.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db.base import coerce_utc, new_id, utcnow
from ..db.models import IdempotencyRecord
from ..domain.enums import Permission
from ..domain.errors import ApiError, ErrorCode
from ..repositories.platform import request_digest
from . import authorization
from .unit_of_work import UnitOfWork

#: Atomic digests are namespaced so an old row can never be mistaken for a new one (§9.2.2).
ATOMIC_PREFIX = "sha256:v2:"
#: `idempotency_record.request_digest` is a String(80) column; `sha256:v2:` plus 64 hex is 74.
MAX_KEY_CHARS = 120
#: Versioned wrapper around the stored business result (§9.2.2).
INTERNAL_FORMAT = "atomic.v1"

_CONFLICT = "The same Idempotency-Key was reused with a different request body"


def atomic_request_digest(command: dict[str, Any]) -> str:
    """Digest the caller's *intent*, not what the server resolved at runtime (§9.2).

    Entrypoint, request id, scopes and the key are excluded; declared defaults, resource ids, expected
    versions and digests are included. An omitted optional and an explicit null hash differently, so
    "run the current case" can never replay as "run this exact artifact".
    """
    return ATOMIC_PREFIX + hashlib.sha256(_canonical(command).encode("utf-8")).hexdigest()


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)


def execute_atomic_command(
    uow: UnitOfWork,
    *,
    route: str,
    key: str | None,
    command: dict[str, Any],
    action: Callable[[], tuple[str, dict[str, Any]]],
    project_id: str | None = None,
    permission: Permission | None = None,
    recheck: Callable[[], None] | None = None,
    gate: Callable[[], None] | None = None,
    legacy_payload: Any = None,
) -> dict[str, Any]:
    """Run `action` at most once for `key`, or return the answer an earlier call stored.

    `action` must work in `uow.scope` and flush only; it is invoked after the lock is held, and it
    re-reads whatever mutable state it acts on so a lock wait cannot be answered from a stale ORM
    snapshot (§9.3.2). A command without a key still runs in the caller's unit of work and still gets
    exactly one committer - it simply makes no deduplication promise (§9.2.1).

    Authority is re-read once the wait ends (§9.3.1). `project_id` plus `permission` is the ordinary
    shape; `recheck` replaces it for a command whose authority is not one permission on one project,
    such as cancelling a run that belongs to the caller.

    `gate` is for a precondition that may refuse a *new* action and must never refuse a replay: §9.3.1
    puts version, availability and policy checks after the replay decision precisely so that an intent
    which already succeeded stays readable with its own key. It runs after that decision and before
    `action`, so a refusal here rolls the reservation back with it and leaves the key unspent.
    """
    session = uow.scope
    call = uow.call
    if not key:
        if gate is not None:
            gate()
        return action()[1]
    if len(key) > MAX_KEY_CHARS:
        raise ApiError(ErrorCode.VALIDATION_ERROR, f"Idempotency-Key must be at most {MAX_KEY_CHARS} characters")

    uow.lock_command(route=route, key=key)
    session.expire_all()
    if recheck is not None:
        recheck()
    elif project_id is not None and permission is not None:
        authorization.require_after_lock(session, call, project_id=project_id, permission=permission)

    digest = atomic_request_digest(command)
    row = _find_live(session, call.tenant_id, actor_id=call.actor_id, route=route, key=key)
    if row is not None:
        replayed = _replay(row, digest=digest, legacy_payload=legacy_payload)
        if replayed is not None:
            uow.replayed = True
            return replayed
    else:
        row = _reserve(
            session,
            call.tenant_id,
            actor_id=call.actor_id,
            route=route,
            key=key,
            digest=digest,
            ttl_hours=call.settings.idempotency_ttl_hours,
        )

    if gate is not None:
        gate()
    resource_id, body = action()
    row.resource_id = resource_id
    row.response = {"format": INTERNAL_FORMAT, "result": body}
    row.expires_at = utcnow() + timedelta(hours=call.settings.idempotency_ttl_hours)
    session.flush()
    return body


def _find_live(session: Session, tenant_id: str, *, actor_id: str, route: str, key: str) -> IdempotencyRecord | None:
    row = session.scalar(
        select(IdempotencyRecord).where(
            IdempotencyRecord.tenant_id == tenant_id,
            IdempotencyRecord.actor_id == actor_id,
            IdempotencyRecord.route == route,
            IdempotencyRecord.key == key,
        )
    )
    if row is not None and coerce_utc(row.expires_at) <= utcnow():
        # Past the retention window the key is the client's again, exactly as the legacy path treats it.
        session.delete(row)
        session.flush()
        return None
    return row


def _reserve(
    session: Session, tenant_id: str, *, actor_id: str, route: str, key: str, digest: str, ttl_hours: int
) -> IdempotencyRecord:
    row = IdempotencyRecord(
        id=new_id(),
        tenant_id=tenant_id,
        actor_id=actor_id,
        route=route,
        key=key,
        request_digest=digest,
        resource_id=None,
        response=None,
        expires_at=utcnow() + timedelta(hours=ttl_hours),
    )
    session.add(row)
    session.flush()
    return row


def _replay(row: IdempotencyRecord, *, digest: str, legacy_payload: Any) -> dict[str, Any] | None:
    """The stored answer for this key, or nothing when the command still has to run.

    Returns `None` only for a live atomic reservation that this call is entitled to finish; every
    state that cannot be reconciled raises rather than re-executing.
    """
    stored = str(row.request_digest)
    if stored.startswith(ATOMIC_PREFIX):
        if stored != digest:
            raise ApiError(ErrorCode.IDEMPOTENCY_CONFLICT, _CONFLICT)
        if row.response is None:
            raise ApiError(
                ErrorCode.IDEMPOTENCY_RESULT_UNKNOWN,
                "An earlier attempt with this key has no recorded outcome; query the resource before retrying",
                details={"resource_id": row.resource_id},
            )
        return _unwrap(row.response)
    return _replay_legacy(row, legacy_payload=legacy_payload)


def _unwrap(response: dict[str, Any]) -> dict[str, Any]:
    body = response.get("result")
    if str(response.get("format")) != INTERNAL_FORMAT or not isinstance(body, dict):
        raise ApiError(
            ErrorCode.IDEMPOTENCY_RESULT_UNKNOWN,
            "The stored response for this key is not a recognised atomic result",
        )
    return dict(body)


def _replay_legacy(row: IdempotencyRecord, *, legacy_payload: Any) -> dict[str, Any]:
    """Prove an old record belongs to this command, or refuse to touch it (§9.2.2).

    The legacy digest is computed over the legacy request body, which is not the normalised command
    DTO, so equivalence can only be checked against a payload the route can rebuild exactly. Anything
    that cannot be proven is a conflict or an unknown outcome; it is never a silent re-execution,
    because re-executing is the one answer that could duplicate a committed resource.
    """
    if row.response is None:
        raise ApiError(
            ErrorCode.IDEMPOTENCY_RESULT_UNKNOWN,
            "A pre-upgrade idempotency record for this key has no recorded outcome; query the resource before retrying",
            details={"resource_id": row.resource_id},
        )
    if legacy_payload is None:
        raise ApiError(
            ErrorCode.IDEMPOTENCY_CONFLICT,
            "This key already has a record stored in the legacy format for a command that cannot be verified",
            details={"record_format": "legacy"},
        )
    if str(row.request_digest) != request_digest(legacy_payload):
        raise ApiError(
            ErrorCode.IDEMPOTENCY_CONFLICT,
            "This key already has a record stored with different inputs",
            details={"record_format": "legacy"},
        )
    body = row.response
    if not isinstance(body, dict):
        raise ApiError(ErrorCode.IDEMPOTENCY_RESULT_UNKNOWN, "The legacy response for this key is unreadable")
    return dict(body)
