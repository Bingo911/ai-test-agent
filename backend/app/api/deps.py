"""Request plumbing every route shares: identity, tenancy, RBAC, paging, idempotency, errors (§13.1, §14.1)."""

from __future__ import annotations

import base64
import binascii
import hmac
import json
import re
import uuid
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import Depends, Header, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session
from starlette.exceptions import HTTPException as StarletteHTTPException

from ..application.context import CallContext
from ..application.identity import resolve_subject, resolve_user_id
from ..application.unit_of_work import UnitOfWork, command_transaction
from ..config import Settings, get_settings
from ..db.base import Database, get_database
from ..db.models import Project
from ..domain.enums import Permission
from ..domain.errors import ApiError, ErrorCode
from ..domain.rbac import Identity
from ..observability import current_request, current_tenant, get_logger, redact
from ..repositories.platform import AccessRepository, IdempotencyRepository

log = get_logger(__name__)

_REQUEST_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,60}$")


# ---------------------------------------------------------------------- identity


def _dev_subjects(settings: Settings) -> dict[str, str]:
    """Development identities are static bearer tokens mapped onto the seeded users (§14.1)."""
    table: dict[str, str] = {}
    if settings.dev_admin_token:
        table[settings.dev_admin_token] = "dev-admin"
    if settings.dev_engineer_token:
        table[settings.dev_engineer_token] = "dev-engineer"
    return table


def _bearer(authorization: str | None) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise ApiError(ErrorCode.UNAUTHENTICATED, "A bearer token is required")
    return authorization[7:].strip()


def _dev_credential(settings: Settings, token: str) -> str:
    """Constant-time match against the configured dev tokens; no session is stored anywhere."""
    subject = None
    for candidate, mapped in _dev_subjects(settings).items():
        if hmac.compare_digest(candidate, token):
            subject = mapped
    if subject is None:
        raise ApiError(ErrorCode.UNAUTHENTICATED, "Unknown development token")
    return subject


_jwks_clients: dict[str, Any] = {}


def _oidc_claims(settings: Settings, token: str) -> dict[str, Any]:
    """Signature, issuer, audience, expiry and `sub` are all verified, never assumed (§14.1)."""
    import jwt

    if not settings.oidc_jwks_uri:
        raise ApiError(ErrorCode.DEPENDENCY_UNAVAILABLE, "auth_mode=oidc needs oidc_jwks_uri configured")
    client = _jwks_clients.get(settings.oidc_jwks_uri)
    if client is None:
        client = jwt.PyJWKClient(settings.oidc_jwks_uri, lifespan=600, cache_keys=True)
        _jwks_clients[settings.oidc_jwks_uri] = client
    try:
        key = client.get_signing_key_from_jwt(token).key
        claims = jwt.decode(
            token,
            key,
            algorithms=["RS256", "ES256"],
            audience=settings.oidc_audience,
            issuer=settings.oidc_issuer,
            options={"require": ["exp", "iat", "sub", "aud"]},
        )
    except ApiError:
        raise
    except Exception as exc:
        raise ApiError(ErrorCode.UNAUTHENTICATED, f"The bearer token was not accepted: {type(exc).__name__}") from exc
    if not str(claims.get("sub") or ""):
        raise ApiError(ErrorCode.UNAUTHENTICATED, "The token carries no subject")
    return claims


@dataclass(frozen=True)
class Credential:
    """What the bearer token proved, before any database has been asked who that is (§5.1, §14.1)."""

    issuer: str
    subject: str
    display_name: str


def verify_credential(settings: Settings, authorization: str | None) -> Credential:
    """Signature, issuer, audience, expiry and `sub` are all verified, never assumed (§14.1)."""
    token = _bearer(authorization)
    if settings.auth_mode == "dev":
        subject = _dev_credential(settings, token)
        return Credential(issuer="local-dev", subject=subject, display_name=subject)
    claims = _oidc_claims(settings, token)
    subject = str(claims["sub"])
    # Email is read from the token but deliberately dropped: it is a contact attribute only and must
    # never merge accounts or carry permissions (§14.1).
    display = str(claims.get("name") or claims.get("preferred_username") or subject)[:200]
    return Credential(
        issuer=str(claims.get("iss") or settings.oidc_issuer or ""), subject=subject, display_name=display
    )


def resolve_identity(
    settings: Settings, database: Database, authorization: str | None, tenant_hint: str | None
) -> Identity:
    """Rebuild the caller's authority from the database on every request; never cache it (§14.1).

    The credential is proved first and only then read against the tenant tables, on the database this
    app instance owns: an identity resolved through another instance's connections would answer for
    the wrong process (§4.4).
    """
    credential = verify_credential(settings, authorization)
    with database.session() as session:
        return resolve_subject(
            session,
            issuer=credential.issuer,
            subject=credential.subject,
            tenant_hint=tenant_hint,
            display_name=credential.display_name,
        )


def resolve_identity_by_id(database: Database, *, user_id: str, tenant_id: str) -> Identity:
    """Re-authorise a caller who proved identity with a ticket instead of a bearer (§14.3).

    The ticket only says *who*; permissions are still read from the database here, so a revoked grant
    or a disabled account stops working on the next request even while the token itself stays valid.
    """
    with database.session() as session:
        return resolve_user_id(session, user_id=user_id, tenant_id=tenant_id)


def app_settings(app: Any) -> Settings:
    """The Settings this app instance was built with, not whatever the environment says now (§4.4)."""
    return getattr(app.state, "settings", None) or get_settings()


def app_database(app: Any) -> Database:
    """The database this app instance owns; the process default only serves the module-level app."""
    database = getattr(app.state, "database", None)
    return database if database is not None else get_database()


# ----------------------------------------------------------------------- context


@dataclass
class Context:
    """Everything a route may legitimately know about the caller.

    Settings and database arrive from the app instance that is handling the request, so a second app
    built with other configuration cannot answer from the first one's connections (§4.4).
    """

    identity: Identity
    request_id: str
    settings: Settings
    database: Database
    #: Only a ticket-authenticated caller has one: the worker lease generation the ticket was minted in.
    session_epoch: int | None = None

    @property
    def tenant_id(self) -> str:
        return self.identity.tenant_id

    @property
    def actor_id(self) -> str:
        return self.identity.user_id

    @contextmanager
    def session(self) -> Iterator[Session]:
        with self.database.session(self.tenant_id) as session:
            yield session

    def call_context(self) -> CallContext:
        """Hand the same authority to a shared application case, without handing it the request."""
        return CallContext(
            identity=self.identity,
            request_id=self.request_id,
            settings=self.settings,
            entrypoint="rest",
            session_epoch=self.session_epoch,
        )

    def unit_of_work(self, *, write: bool = False) -> AbstractContextManager[UnitOfWork]:
        """The transaction handle a shared application case runs in (§9.3.5).

        Deliberately not a context manager on `Context`: an atomic command owns its commit through the
        unit of work, so no route should be able to finish a transaction by leaving an indentation.

        A write comes back wrapped, so a database that refused to wait for a lock reaches the caller as
        the retryable `COMMAND_BUSY` the contract promises rather than as a driver error.
        """
        database, call = self.database, self.call_context()
        if write:
            return command_transaction(database, call)
        return UnitOfWork(database=database, call=call, write=False)

    def require(self, permission: Permission, project_id: str | None = None) -> None:
        self.identity.require(permission, project_id)

    def can(self, permission: Permission, project_id: str | None = None) -> bool:
        return self.identity.can(permission, project_id)

    def visible_project(self, project_id: str) -> bool:
        return self.identity.role_in(project_id) is not None

    def project(self, project_id: str, *, permission: Permission | None = None) -> Project:
        """Load a project and authorise it in one step: unknown or foreign ids read as 404 (§14.1)."""
        with self.session() as session:
            project = AccessRepository(session, self.tenant_id).project(self.tenant_id, project_id)
        if project is None:
            raise ApiError(ErrorCode.NOT_FOUND, "Project not found in your tenant")
        if not self.visible_project(project_id):
            raise ApiError(ErrorCode.FORBIDDEN, "You are not a member of this project")
        if permission is not None:
            self.require(permission, project_id)
        return project

    def audit(
        self,
        session: Session,
        *,
        operation: str,
        resource_type: str,
        resource_id: str | None = None,
        project_id: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        from ..repositories.platform import AuditRepository

        AuditRepository(session, self.tenant_id).append(
            operation=operation,
            resource_type=resource_type,
            resource_id=resource_id,
            actor_id=self.actor_id,
            project_id=project_id,
            request_id=self.request_id,
            detail=detail or {},
        )


def new_request_id() -> str:
    return "req_" + uuid.uuid4().hex[:20]


def get_context(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
    x_tenant_id: Annotated[str | None, Header(alias="X-Tenant-Id")] = None,
) -> Context:
    settings, database = app_settings(request.app), app_database(request.app)
    identity = resolve_identity(settings, database, authorization, x_tenant_id)
    current_tenant.set(identity.tenant_id)
    request_id = getattr(request.state, "request_id", None) or new_request_id()
    current_request.set(request_id)
    return Context(identity=identity, request_id=request_id, settings=settings, database=database)


Ctx = Annotated[Context, Depends(get_context)]


# --------------------------------------------------------------------- pagination


@dataclass
class Page:
    limit: int = 20
    offset: int = 0


def page_params(
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    cursor: Annotated[str | None, Query(description="Opaque continuation from a previous page")] = None,
) -> Page:
    return Page(limit=limit, offset=_decode_cursor(cursor))


def _decode_cursor(cursor: str | None) -> int:
    if not cursor:
        return 0
    try:
        decoded = base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8")
        offset = int(json.loads(decoded)["o"])
    except (ValueError, TypeError, KeyError, binascii.Error, UnicodeDecodeError, json.JSONDecodeError):
        raise ApiError(ErrorCode.VALIDATION_ERROR, "The page cursor is not one this list issued") from None
    return max(0, offset)


def _encode_cursor(offset: int) -> str:
    return base64.urlsafe_b64encode(json.dumps({"o": offset}).encode("utf-8")).decode("ascii")


def page_envelope(items: list[Any], page: Page, *, total: int | None = None) -> dict[str, Any]:
    """`{items,next_cursor}` over a fixed `created_at DESC, id DESC` order upstream (§13.1)."""
    payload: dict[str, Any] = {"items": items, "next_cursor": None}
    if total is not None:
        payload["total"] = total
    if items and len(items) >= page.limit:
        payload["next_cursor"] = _encode_cursor(page.offset + len(items))
    return payload


# -------------------------------------------------------------------- idempotency


def idempotent(
    ctx: Context,
    *,
    route: str,
    key: str | None,
    payload: Any,
    action: Callable[[], tuple[str, dict[str, Any]]],
) -> dict[str, Any]:
    """Run `action` at most once per key, or replay the response an earlier call stored (§13.1).

    A key whose first attempt never answered is taken over rather than blocked, and any failure frees
    the key again, so a client retrying after a network fault is never locked out.
    """
    if not key:
        return action()[1]
    if len(key) > 120:
        raise ApiError(ErrorCode.VALIDATION_ERROR, "Idempotency-Key must be at most 120 characters")
    with ctx.session() as session:
        repo = IdempotencyRepository(session, ctx.tenant_id)
        row = repo.reserve_or_replay(
            actor_id=ctx.actor_id,
            route=route,
            key=key,
            payload=payload,
            ttl_hours=ctx.settings.idempotency_ttl_hours,
        )
        if row[1] is not None:
            return dict(row[1])
        record = row[0]
        session.commit()
    try:
        resource_id, body = action()
    except Exception:
        with ctx.session() as session:
            live = IdempotencyRepository(session, ctx.tenant_id).find(actor_id=ctx.actor_id, route=route, key=key)
            if live is not None and live.response is None:
                IdempotencyRepository(session, ctx.tenant_id).release(live)
                session.commit()
        raise
    with ctx.session() as session:
        scoped = IdempotencyRepository(session, ctx.tenant_id)
        live = scoped.find(actor_id=ctx.actor_id, route=route, key=key) or record
        scoped.complete(live, resource_id=resource_id, response=body)
        session.commit()
    return body


# ------------------------------------------------------------------- ETag plumbing


def etag(kind: str, row_id: str, row_version: int) -> str:
    return f'"{kind}-{row_id}-{row_version}"'


def parse_if_match(value: str | None, *, required: bool = False) -> int | None:
    """`If-Match` carries the row version, so an edit cannot silently overwrite a newer save (§13.1)."""
    if not value:
        if required:
            raise ApiError(ErrorCode.VALIDATION_ERROR, "This edit requires an If-Match header")
        return None
    text = value.strip()
    if text in ("*", "W/*"):
        return None
    if text.startswith("W/"):
        text = text[2:]
    text = text.strip().strip('"')
    match = re.search(r"-(\d+)$", text)
    if not match:
        raise ApiError(ErrorCode.VALIDATION_ERROR, "If-Match must be the ETag read from the resource")
    return int(match.group(1))


def etag_response(payload: dict[str, Any], *, tag: str) -> JSONResponse:
    return JSONResponse(content=payload, headers={"ETag": tag})


# ------------------------------------------------------------------- error plumbing


def error_body(
    code: str, message: str, *, request_id: str | None = None, details: dict[str, Any] | None = None
) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": redact(str(message))[:600]}
    if request_id:
        error["request_id"] = request_id
    if details:
        error["details"] = details
    return {"error": error}


def error_response(
    code: str,
    message: str,
    status: int,
    *,
    request_id: str | None = None,
    details: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content=error_body(code, message, request_id=request_id, details=details),
        headers=headers,
    )


def register_exception_handlers(app: Any) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(request: Request, exc: ApiError) -> JSONResponse:
        request_id = getattr(request.state, "request_id", None)
        if exc.http_status >= 500:
            log.error(
                "request failed",
                extra={"context": {"code": exc.code.value, "status": exc.http_status, "path": request.url.path}},
            )
        return error_response(exc.code.value, exc.message, exc.http_status, request_id=request_id, details=exc.details)

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        problems = [
            {
                "field": ".".join(str(part) for part in item.get("loc", ()) if part != "body"),
                "reason": str(item.get("msg"))[:200],
            }
            for item in list(exc.errors())[:20]
        ]
        return error_response(
            ErrorCode.VALIDATION_ERROR.value,
            "The request body or parameters did not validate",
            400,
            request_id=getattr(request.state, "request_id", None),
            details={"problems": problems},
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = {
            401: ErrorCode.UNAUTHENTICATED.value,
            403: ErrorCode.FORBIDDEN.value,
            404: ErrorCode.NOT_FOUND.value,
            405: ErrorCode.VALIDATION_ERROR.value,
            413: ErrorCode.PAYLOAD_TOO_LARGE.value,
        }.get(
            exc.status_code,
            ErrorCode.DEPENDENCY_UNAVAILABLE.value if exc.status_code >= 500 else ErrorCode.VALIDATION_ERROR.value,
        )
        return error_response(
            code,
            str(exc.detail or "The request could not be handled"),
            exc.status_code,
            request_id=getattr(request.state, "request_id", None),
        )

    @app.exception_handler(Exception)
    async def _unexpected(request: Request, exc: Exception) -> JSONResponse:  # pragma: no cover - safety net
        log.error(
            "unhandled error",
            extra={"context": {"path": request.url.path, "error": f"{type(exc).__name__}: {exc}"[:300]}},
        )
        return error_response(
            "INTERNAL",
            "The request failed unexpectedly; it was logged against this request id",
            500,
            request_id=getattr(request.state, "request_id", None),
        )


class RequestContextMiddleware:
    """Assigns the correlation id and refuses oversized bodies before a handler touches them (§13.1)."""

    def __init__(self, app: Any, *, max_body_bytes: int) -> None:
        self.app = app
        self.max_body_bytes = max_body_bytes

    async def __call__(self, scope: dict, receive: Callable, send: Callable) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        headers = {key.decode("latin-1").lower(): value.decode("latin-1") for key, value in scope.get("headers", [])}
        supplied = (headers.get("x-request-id") or "").strip()
        request_id = supplied if _REQUEST_ID.match(supplied) else new_request_id()
        scope["state"] = dict(scope.get("state") or {})
        scope["state"]["request_id"] = request_id
        current_request.set(request_id)
        length = _as_int(headers.get("content-length"))
        if length is not None and length > self.max_body_bytes:
            await _send_json(
                send,
                status=413,
                payload=error_body(
                    ErrorCode.PAYLOAD_TOO_LARGE.value,
                    f"Request bodies are limited to {self.max_body_bytes} bytes",
                    request_id=request_id,
                ),
                request_id=request_id,
            )
            return

        async def send_wrapper(message: dict) -> None:
            if message["type"] == "http.response.start":
                pairs = [pair for pair in (message.get("headers") or []) if pair[0].lower() != b"x-request-id"]
                pairs.append((b"x-request-id", request_id.encode("latin-1")))
                message["headers"] = pairs
            await send(message)

        await self.app(scope, receive, send_wrapper)


async def _send_json(send: Callable, *, status: int, payload: dict, request_id: str) -> None:
    body = json.dumps(payload).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json; charset=utf-8"),
                (b"content-length", str(len(body)).encode("ascii")),
                (b"x-request-id", request_id.encode("latin-1")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


def _as_int(value: str | None) -> int | None:
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None
