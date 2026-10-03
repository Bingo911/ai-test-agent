"""MCP token verification on its own bounded resources (MCP design §5.1.1, §5.3).

The REST stack's `_oidc_claims` fetches keys synchronously through a module-level cache, and
`get_context` opens the global database. Neither can be reused here: a blocking fetch would run on
the same event loop that serves MCP streaming, and a global database would hand MCP traffic the REST
connection pool. So this module owns its own HTTP client, its own two threads and its own key cache,
every path with a deadline; the database identity lookup stays in the caller's injected session.

Resource ownership is the subtle part. A key refresh started by one request may outlive that request,
so the auth permit travels with the refresh task rather than the caller: a caller that gives up, or a
waiter that times out, must not report the slot as free while a thread and a socket still hold it.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import functools
import hashlib
import hmac
import json
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric import ec, rsa

from ..config import Settings
from ..observability import get_logger

log = get_logger(__name__)

#: §5.3 - the scopes this resource issues, in one place so the metadata and the checks cannot drift.
SCOPE_CONNECT = "aita:connect"
SCOPE_READ = "aita:read"
SCOPE_WRITE = "aita:write"
SCOPE_RUN = "aita:run"
ALL_SCOPES = (SCOPE_CONNECT, SCOPE_READ, SCOPE_WRITE, SCOPE_RUN)

_ALLOWED_JWT_ALGORITHMS = ("RS256", "ES256")
_MAX_JWKS_BYTES = 1_024 * 1024
_MAX_JWKS_KEYS = 64
_MAX_BEARER_BYTES = 16 * 1024
_KEY_CACHE_SECONDS = 600.0
_REFRESH_COOLDOWN_SECONDS = 5.0
_RSA_MIN_BITS = 2048
_RSA_MAX_BITS = 8192
#: §5.1.1 staged HTTP budgets; the whole fetch is additionally capped by mcp_jwks_timeout_seconds.
_CONNECT_TIMEOUT_SECONDS = 0.25
_READ_TIMEOUT_SECONDS = 0.5
_POOL_TIMEOUT_SECONDS = 0.1


class AuthError(Exception):
    """An HTTP-level refusal raised before any tool runs (§10).

    Status and challenge are decided here rather than by the caller so a client can tell the three
    cases apart: 401 means "get another token", 403 means "ask for a wider scope", 503 means "this
    token is fine, retry later". Collapsing them would leak IdP outages into the invalid-token metric.
    """

    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        *,
        scope_hint: str | None = None,
        retry_after_ms: int | None = None,
    ) -> None:
        self.status = status
        self.code = code
        self.message = message
        self.scope_hint = scope_hint
        self.retry_after_ms = retry_after_ms
        super().__init__(message)


@dataclass(frozen=True)
class VerifiedPrincipal:
    """What the signature actually proved - tenant, roles and project rights are still to be read (§5.1)."""

    issuer: str
    subject: str
    scopes: tuple[str, ...] = ()
    client_id: str | None = None

    @property
    def subject_key(self) -> str:
        """A stable digest for per-subject admission; the raw subject never becomes a metric label (§11)."""
        return hashlib.sha256(f"{self.issuer}\n{self.subject}".encode()).hexdigest()[:32]

    def require_scopes(self, *scopes: str) -> None:
        missing = [scope for scope in scopes if scope not in self.scopes]
        if missing:
            raise AuthError(
                403,
                "insufficient_scope",
                "The token does not carry the scope for this operation",
                scope_hint=" ".join(missing),
            )


class _SlotLease:
    """A counted lease on one auth slot, so a resource can outlive the request that took it.

    Two holders exist by design: the caller waiting for a result, and work that keeps running after
    that caller gives up - a shared key refresh, or a verification thread already executing. Each
    takes a reference, and the slot returns to the semaphore only when the last one lets go. Without
    the count, either a cancelled caller frees a live thread's slot, or every path releases and the
    semaphore is credited a permit it never handed out.
    """

    def __init__(self, release: Callable[[], None]) -> None:
        self._release = release
        self._held = 1
        self._done = False

    def extend(self) -> None:
        if not self._done:
            self._held += 1

    def release(self) -> None:
        if self._done:
            return
        self._held -= 1
        if self._held <= 0:
            self._done = True
            self._release()

    @property
    def references(self) -> int:
        return 0 if self._done else self._held


@dataclass
class _CachedKey:
    kid: str
    key: Any


def _b64_bigint(value: str) -> int:
    padded = value + "=" * (-len(value) % 4)
    return int.from_bytes(base64.urlsafe_b64decode(padded), "big")


def public_key_from_jwk(entry: Any) -> Any | None:
    """Return a vetted public key, or None when the entry is unusable, private, or hostile (§5.1.1).

    A key set is untrusted input. Beyond the signature check itself, the material is bounded - RSA
    moduli 2048..8192 bits with an odd exponent under 32 bits, EC restricted to P-256, and no
    symmetric or private halves - because an unbounded key construction is CPU the attacker chose,
    and a symmetric key handed to `jwt.decode` would let one pick the algorithm we allowlisted.
    """
    if not isinstance(entry, dict) or entry.get("kty") not in ("RSA", "EC"):
        return None
    if any(marker in entry for marker in ("d", "p", "q", "dp", "dq", "qi")):
        return None
    try:
        if entry["kty"] == "RSA":
            bits = _b64_bigint(str(entry.get("n") or "")).bit_length()
            if not _RSA_MIN_BITS <= bits <= _RSA_MAX_BITS:
                return None
            exponent = _b64_bigint(str(entry.get("e") or ""))
            if not 1 < exponent <= 0xFFFFFFFF or exponent % 2 == 0:
                return None
            key = jwt.algorithms.RSAAlgorithm.from_jwk(json.dumps(entry))
            return key if isinstance(key, rsa.RSAPublicKey) else None
        if entry.get("crv") != "P-256":
            return None
        key = jwt.algorithms.ECAlgorithm.from_jwk(json.dumps(entry))
        if not isinstance(key, ec.EllipticCurvePublicKey) or not isinstance(key.curve, ec.SECP256R1):
            return None
        return key
    except (binascii.Error, ValueError, TypeError, Exception):
        # One unusable entry must not poison a set that also contains the key we actually need.
        return None


def parse_jwks(payload: bytes) -> dict[str, _CachedKey]:
    try:
        document = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AuthError(503, "dependency_unavailable", "The signing key response was not JSON") from exc
    entries = document.get("keys") if isinstance(document, dict) else None
    if not isinstance(entries, list) or len(entries) > _MAX_JWKS_KEYS:
        raise AuthError(503, "dependency_unavailable", "The signing key set was malformed or too large")
    keys: dict[str, _CachedKey] = {}
    for entry in entries:
        key = public_key_from_jwk(entry)
        kid = entry.get("kid") if isinstance(entry, dict) else None
        if key is None or not isinstance(kid, str) or not kid:
            continue
        keys[kid] = _CachedKey(kid=kid, key=key)
    return keys


class JwksKeySource:
    """Fetches and caches signing keys for one configured issuer (§5.1.1).

    Only `oidc_jwks_uri` is ever contacted, and only over HTTPS with certificate verification and no
    redirects: `jku`, `x5u` and `iss` inside a token never select a network address.
    """

    def __init__(self, settings: Settings, *, http_client: httpx.AsyncClient | None = None) -> None:
        self.uri = settings.oidc_jwks_uri or ""
        self.timeout = min(settings.mcp_jwks_timeout_seconds, settings.mcp_auth_timeout_seconds)
        self._client = http_client
        self._owns_client = http_client is None
        self._keys: Mapping[str, _CachedKey] = {}
        self._fetched_at: float | None = None
        self._last_attempt: float | None = None
        self._refresh: asyncio.Task[None] | None = None
        self._refresh_lease: _SlotLease | None = None

    def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                limits=httpx.Limits(max_connections=2, max_keepalive_connections=2),
                timeout=httpx.Timeout(
                    connect=_CONNECT_TIMEOUT_SECONDS,
                    read=_READ_TIMEOUT_SECONDS,
                    write=_READ_TIMEOUT_SECONDS,
                    pool=_POOL_TIMEOUT_SECONDS,
                ),
                follow_redirects=False,
            )
        return self._client

    @property
    def cache_age_seconds(self) -> float | None:
        return None if self._fetched_at is None else asyncio.get_running_loop().time() - self._fetched_at

    async def close(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    async def key_for(self, kid: str | None, *, lease: _SlotLease, deadline: float) -> Any:
        """Return a cached or freshly fetched public key, using at most one refresh per issuer at a time.

        `lease` is the auth slot the caller already holds. A refresh it starts takes a reference on
        that lease for the refresh's own lifetime, so a caller that abandons the wait cannot report
        the slot as free while the fetch and parse are still running.
        """
        loop = asyncio.get_running_loop()
        cached = self._usable(kid)
        if cached is not None:
            return cached
        handed_off = False
        try:
            refresh = self._refresh
            if refresh is not None and not refresh.done():
                await self._await(refresh, deadline)
                return self._require(kid)
            now = loop.time()
            if self._last_attempt is not None and now - self._last_attempt < _REFRESH_COOLDOWN_SECONDS:
                # Retrying per token would turn one IdP outage into an unbounded fan-out of fetches.
                raise AuthError(503, "dependency_unavailable", "Signing keys are temporarily unavailable")
            self._last_attempt = now
            self._refresh_lease = lease
            lease.extend()
            handed_off = True
            self._refresh = asyncio.create_task(self._refresh_then_release())
            await self._await(self._refresh, deadline)
            return self._require(kid)
        finally:
            if not handed_off:
                lease.release()

    async def _await(self, task: asyncio.Task[None], deadline: float) -> None:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise AuthError(503, "dependency_unavailable", "The authentication deadline was reached")
        try:
            # shield: a waiter giving up must not cancel the refresh other calls are depending on.
            await asyncio.wait_for(asyncio.shield(task), remaining)
        except asyncio.TimeoutError as exc:
            raise AuthError(503, "dependency_unavailable", "The signing key refresh took too long") from exc

    def _usable(self, kid: str | None) -> Any | None:
        if self._fetched_at is None:
            return None
        if asyncio.get_running_loop().time() - self._fetched_at >= _KEY_CACHE_SECONDS:
            return None
        entry = self._keys.get(kid or "")
        return entry.key if entry is not None else None

    def _require(self, kid: str | None) -> Any:
        key = self._usable(kid)
        if key is None:
            # Reaching this line means a refresh just *succeeded*: a fetch that failed raised as a 503 on
            # its way out, and an empty or expired cache never arrives here. A key set this resource read
            # and that does not name this kid is a statement about the token, so §5.1.1 makes it a 401 -
            # "read, and no" rather than "could not tell" - rather than letting a caller invent kids until
            # the IdP-outage metric is full and a client is told to go and re-authorise.
            raise AuthError(401, "invalid_token", "This token was signed by a key this resource does not know")
        return key

    async def _refresh_then_release(self) -> None:
        lease = self._refresh_lease
        try:
            await self._fetch()
        finally:
            self._refresh = None
            self._refresh_lease = None
            if lease is not None:
                lease.release()

    async def _fetch(self) -> None:
        client = self._ensure_client()
        request = client.build_request(
            "GET", self.uri, headers={"Accept": "application/json", "Accept-Encoding": "identity"}
        )
        try:
            response = await asyncio.wait_for(client.send(request, stream=True), self.timeout)
        except (httpx.HTTPError, asyncio.TimeoutError) as exc:
            raise AuthError(503, "dependency_unavailable", "The signing key service is unreachable") from exc
        try:
            if response.status_code != 200:
                raise AuthError(503, "dependency_unavailable", "The signing key service refused the request")
            if (response.headers.get("content-encoding") or "identity") != "identity":
                raise AuthError(503, "dependency_unavailable", "Unexpected encoding in the signing key response")
            body = bytearray()
            try:
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    # Refuse while reading rather than buffering a response chosen to exhaust memory.
                    if len(body) > _MAX_JWKS_BYTES:
                        raise AuthError(503, "dependency_unavailable", "The signing key response was too large")
            except httpx.HTTPError as exc:
                raise AuthError(503, "dependency_unavailable", "The signing key response was interrupted") from exc
            keys = parse_jwks(bytes(body))
        finally:
            await response.aclose()
        if not keys:
            raise AuthError(503, "dependency_unavailable", "The signing key set contained no usable key")
        self._keys = keys
        self._fetched_at = asyncio.get_running_loop().time()


class DevTokenSource:
    """Development bearer tokens mapped to fixed subjects: loopback only, never a network path (§5.2)."""

    def __init__(self, settings: Settings) -> None:
        self._tokens: dict[str, str] = {}
        if settings.dev_admin_token:
            self._tokens[settings.dev_admin_token] = "dev-admin"
        if settings.dev_engineer_token:
            self._tokens[settings.dev_engineer_token] = "dev-engineer"

    def subject_for(self, token: str) -> str | None:
        found: str | None = None
        for candidate, mapped in self._tokens.items():
            # Every entry is compared: short-circuiting would leak which token matched through timing.
            if hmac.compare_digest(candidate, token):
                found = mapped
        return found


class McpAuthenticator:
    """Verifies the bearer token on two dedicated threads and holds the result to a scope floor (§5.3)."""

    def __init__(self, settings: Settings, *, key_source: JwksKeySource | None = None) -> None:
        self.settings = settings
        self.audience = settings.mcp_oidc_audience
        self.auth_executor = ThreadPoolExecutor(
            max_workers=settings.mcp_auth_max_inflight,
            thread_name_prefix="mcp-auth",
        )
        # The semaphore bound equals the thread bound on purpose: waiting here is bounded, while an
        # executor queue would accept an unlimited number of callers behind those two threads.
        self._slots = asyncio.Semaphore(settings.mcp_auth_max_inflight)
        #: Verification futures that outlived their caller; referenced so they are not collected mid-run.
        self._running_verifications: set[asyncio.Future[Any]] = set()
        self._dev = None if settings.auth_mode == "oidc" else DevTokenSource(settings)
        self.key_source = key_source
        if settings.auth_mode == "oidc" and self.key_source is None:
            self.key_source = JwksKeySource(settings)

    async def close(self) -> None:
        if self.key_source is not None:
            await self.key_source.close()
        # Waiting is the only form that matches the shutdown contract: an auth thread that is still
        # verifying must finish before this resource is reported as released (§4.3).
        await asyncio.get_running_loop().run_in_executor(None, self.auth_executor.shutdown, True)

    async def authenticate(self, authorization: str | None, *, deadline: float | None = None) -> VerifiedPrincipal:
        """Prove the token, or raise `AuthError` with the status the client must see."""
        token = bearer_token(authorization)
        loop = asyncio.get_running_loop()
        ends = deadline if deadline is not None else loop.time() + self.settings.mcp_auth_timeout_seconds
        lease = await self._acquire_slot(ends)
        try:
            if self._dev is not None:
                subject = self._dev.subject_for(token)
                if subject is None:
                    raise AuthError(401, "invalid_token", "This development token is not recognised")
                # A dev token stands in for a full grant, so it carries every scope this resource knows.
                return VerifiedPrincipal(issuer="local-dev", subject=subject, scopes=ALL_SCOPES)
            return await self._verify_oidc(token, lease=lease, ends=ends)
        finally:
            lease.release()

    async def _acquire_slot(self, ends: float) -> _SlotLease:
        """Wait at most 100 ms for one of the two auth threads, then busy-reject (§5.1.1)."""
        loop = asyncio.get_running_loop()
        wait = min(0.1, max(0.0, ends - loop.time()))
        try:
            await asyncio.wait_for(self._slots.acquire(), wait)
        except asyncio.TimeoutError as exc:
            raise AuthError(503, "command_busy", "The authentication path is saturated", retry_after_ms=50) from exc
        return _SlotLease(self._slots.release)

    async def _verify_oidc(self, token: str, *, lease: _SlotLease, ends: float) -> VerifiedPrincipal:
        if not self.audience:
            raise AuthError(503, "dependency_unavailable", "auth_mode=oidc needs mcp_oidc_audience for MCP")
        if self.key_source is None:
            # `auth_mode=oidc` always builds one above; this is the boundary where a caller-supplied
            # key source could have been left out, and a bounded 503 beats an AttributeError on a token.
            raise AuthError(503, "dependency_unavailable", "MCP key resolution is not configured")
        headers = _unverified_headers(token)
        key = await self.key_source.key_for(headers.get("kid"), lease=lease, deadline=ends)
        remaining = ends - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise AuthError(503, "dependency_unavailable", "The authentication deadline was reached")
        # Parsing, key construction and signature work all run inside the two auth threads.
        loop = asyncio.get_running_loop()
        running = loop.run_in_executor(
            self.auth_executor, functools.partial(_decode, token, key, self.audience, self.settings.oidc_issuer)
        )
        # A verification already running cannot be stopped by an await timeout, so the slot is held by
        # the worker itself: the caller may give up, but the thread keeps its reservation until it
        # actually returns, otherwise repeated timeouts would admit unbounded replacement threads.
        lease.extend()
        running.add_done_callback(functools.partial(self._finish_verification, lease))
        self._running_verifications.add(running)
        try:
            claims = await asyncio.wait_for(asyncio.shield(running), remaining)
        except asyncio.TimeoutError as exc:
            raise AuthError(503, "dependency_unavailable", "Token verification took too long") from exc
        except Exception as exc:
            raise AuthError(401, "invalid_token", f"The bearer token was not accepted: {type(exc).__name__}") from exc
        return principal_from_claims(claims, self.settings.oidc_issuer or "")

    def _finish_verification(self, lease: _SlotLease, future: asyncio.Future[Any]) -> None:
        self._running_verifications.discard(future)
        # Read the outcome so an abandoned verification does not print "exception never retrieved";
        # the caller already has its own answer by now, or has given up on this attempt.
        if not future.cancelled():
            future.exception()
        lease.release()


def bearer_token(authorization: str | None) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise AuthError(401, "invalid_request", "A bearer token is required")
    token = authorization[7:].strip()
    if not token:
        raise AuthError(401, "invalid_token", "The bearer token is empty")
    # Rejected before any parsing: a header that large is a load vector, not a credential (§5.1.1).
    if len(token.encode("utf-8")) > _MAX_BEARER_BYTES:
        raise AuthError(431, "invalid_request", "The bearer token exceeds the accepted size")
    return token


def _unverified_headers(token: str) -> dict[str, Any]:
    try:
        return dict(jwt.get_unverified_header(token))
    except Exception as exc:
        raise AuthError(401, "invalid_token", "The bearer token is not a well-formed JWT") from exc


def _decode(token: str, key: Any, audience: str, issuer: str | None) -> dict[str, Any]:
    """Verify signature, audience, issuer and the required claims; the token's own `alg` cannot widen the set."""
    return jwt.decode(
        token,
        key,
        algorithms=list(_ALLOWED_JWT_ALGORITHMS),
        audience=audience,
        issuer=issuer or None,
        options={"require": ["exp", "iat", "sub", "aud"]},
    )


def principal_from_claims(claims: dict[str, Any], configured_issuer: str) -> VerifiedPrincipal:
    subject = str(claims.get("sub") or "")
    if not subject:
        raise AuthError(401, "invalid_token", "The token carries no subject")
    # A nonce only appears in an ID Token, whose audience is a client rather than this resource server.
    if "nonce" in claims:
        raise AuthError(401, "invalid_token", "An ID Token cannot be used as an API access token")
    return VerifiedPrincipal(
        issuer=str(claims.get("iss") or configured_issuer),
        subject=subject,
        scopes=_claim_scopes(claims),
        client_id=str(claims.get("azp") or claims.get("client_id") or "") or None,
    )


def _claim_scopes(claims: dict[str, Any]) -> tuple[str, ...]:
    raw = claims.get("scope")
    items = raw.split() if isinstance(raw, str) else [str(item) for item in raw] if isinstance(raw, list) else []
    return tuple(item for item in items if item in ALL_SCOPES)
