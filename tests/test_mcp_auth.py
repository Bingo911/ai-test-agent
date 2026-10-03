"""§5.1.1: the credential verification surface, on its own bounded resources.

Nothing else in the suite reaches this code. Every wire test authenticates with a development token, so
each clause about the OIDC path - which status a key problem answers with, what an unusable JWK is, whose
clock the authentication budget runs on - was only ever asserted by the comment above it. A claim nobody
can turn red is not verified, so this module drives the key source and the middleware against a fake
transport.

Two refusals have to stay distinguishable, and it is not a detail. 401 means the caller's token is the
problem and asking the platform again will not help; 503 means the token may be perfectly good and the
platform could not tell. Collapsing them puts attacker-chosen junk into the IdP-outage metric and sends a
client to re-authenticate during an outage.
"""

from __future__ import annotations

import asyncio
import base64
import json
import time
from collections.abc import Callable
from typing import Any

import httpx
import jwt
import pytest
from backend.app.config import Settings
from backend.app.mcp.auth import (
    ALL_SCOPES,
    AuthError,
    JwksKeySource,
    McpAuthenticator,
    VerifiedPrincipal,
    public_key_from_jwk,
)
from backend.app.mcp.callcontext import CALL_STARTED_STATE_KEY, REQUEST_ID_STATE_KEY
from backend.app.mcp.transport import MCP_PATH, McpAuthMiddleware
from cryptography.hazmat.primitives.asymmetric import ec, rsa

JWKS_URI = "https://idp.test/protocol/openid-connect/certs"
SERVED_KID = "kid-current"


def auth_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "app_env": "test",
        "auth_mode": "oidc",
        "oidc_issuer": "https://idp.test/realms/main",
        "oidc_jwks_uri": JWKS_URI,
        "mcp_oidc_audience": "aita-mcp",
        "log_level": "WARNING",
    }
    values.update(overrides)
    return Settings(**values)


def signing_jwk(kid: str = SERVED_KID) -> dict[str, Any]:
    """One real P-256 public key: a set this parser rejects would make an outage test of a kid test."""
    key = ec.generate_private_key(ec.SECP256R1())
    entry = dict(json.loads(jwt.algorithms.ECAlgorithm.to_jwk(key.public_key())))
    entry["kid"] = kid
    entry["alg"] = "ES256"
    return entry


def _served(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"keys": [signing_jwk()]})


def _source(handler: Callable[[httpx.Request], httpx.Response] | None = None) -> tuple[JwksKeySource, list[str]]:
    """A key source on a transport that records every address it was asked for (§5.1.1: only the URI).

    The recorder is the point of returning `asked`: one outage must not turn into one fetch per token, and
    a token's own `jku`/`x5u` must never choose a network address, which a count of addresses proves
    better than a code review of the two lines that build the request.
    """
    asked: list[str] = []
    serve = handler or _served

    def record(request: httpx.Request) -> httpx.Response:
        asked.append(str(request.url))
        return serve(request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(record))
    return JwksKeySource(auth_settings(), http_client=client), asked


class _Lease:
    """The auth permit a refresh runs on, counted the way §5.1.1 says resources are handed over."""

    def __init__(self) -> None:
        self.extended = 0
        self.released = 0

    def extend(self) -> None:
        self.extended += 1

    def release(self) -> None:
        self.released += 1

    @property
    def references(self) -> int:
        return 1 + self.extended - self.released


async def _refused(source: JwksKeySource, kid: str, *, lease: _Lease | None = None) -> AuthError:
    """Ask for one kid and hand back the refusal, because these cases are all about its status code."""
    holder = lease or _Lease()
    with pytest.raises(AuthError) as caught:
        await source.key_for(kid, lease=holder, deadline=asyncio.get_running_loop().time() + 5.0)
    return caught.value


# --------------------------------------------------------------------------------------
# which refusal a key problem is
# --------------------------------------------------------------------------------------


async def test_a_key_this_resource_cannot_find_after_a_refresh_is_a_bad_token() -> None:
    """§5.1.1: 成功刷新后仍无对应 kid 为 HTTP 401, and nothing about it is an outage (§5.1.1, AC-41).

    The fetch is the discriminating fact. A key set that could not be read says "the platform could not
    tell", which is a 503 the caller may retry; a key set that *was* read successfully and does not name
    this kid says the token was signed by someone this resource does not trust, which no retry fixes.
    Answering 503 here would count attacker-chosen kids in the IdP-outage metric and send a client to
    re-authenticate while nothing is wrong with its credential.
    """
    source, asked = _source()
    try:
        failure = await _refused(source, "kid-from-another-issuer")
        assert failure.status == 401, f"{failure.code}: {failure.message}"
        assert failure.code == "invalid_token"
        # The claimed kid is not repeated back: a message that names it invites a log reader to treat it
        # as an identity that was ever verified (§5.1.1: 日志不包含 token/kid 原文).
        assert "kid-from-another-issuer" not in failure.message
        assert asked == [JWKS_URI]
    finally:
        await source.close()


async def test_a_key_set_that_could_not_be_read_stays_a_bounded_503() -> None:
    """The other side of the same line: an unreadable key set is never dressed up as a bad token."""
    source, asked = _source(lambda request: httpx.Response(500, text="boom"))
    try:
        failure = await _refused(source, SERVED_KID)
        assert failure.status == 503, failure.message
        assert failure.code == "dependency_unavailable"
        # Inside the refresh cooldown the answer is the same refusal without a second fetch: a failing
        # IdP must not be probed once per token an attacker cares to invent (§5.1.1).
        again = await _refused(source, SERVED_KID)
        assert again.status == 503
        assert asked == [JWKS_URI], asked
    finally:
        await source.close()


async def test_a_key_set_with_no_usable_key_is_an_outage_rather_than_a_token_verdict() -> None:
    """An empty set is this resource's problem, so it answers 503 and not 401 (§5.1.1)."""
    source, _asked = _source(lambda request: httpx.Response(200, json={"keys": []}))
    try:
        failure = await _refused(source, SERVED_KID)
        assert failure.status == 503
        assert failure.code == "dependency_unavailable"
    finally:
        await source.close()


async def test_the_refresh_keeps_the_permit_the_caller_handed_it() -> None:
    """A caller that comes back with an answer gives up only its own reference (§5.1.1, AC-41)."""
    source, _asked = _source()
    lease = _Lease()
    try:
        failure = await _refused(source, "kid-absent", lease=lease)
        assert failure.status == 401
        # The refresh took a reference of its own and released exactly that one, so what is left is the
        # caller's - which `McpAuthenticator.authenticate` gives up in its own `finally`.
        assert lease.references == 1
    finally:
        await source.close()


# --------------------------------------------------------------------------------------
# what an untrusted JWK is refused as
# --------------------------------------------------------------------------------------


def _ec_jwk(curve: ec.EllipticCurve, *, private: bool = False) -> dict[str, Any]:
    """An entry a JWK parser accepts, which is what makes a vetting guard worth testing at all.

    Malformed material is refused by the parser itself, so a test built only from broken input passes
    with the guard deleted. Well-formed material that is still unwanted - another curve, the private half
    of a signing key - is the case only this platform's own vetting catches.
    """
    key = ec.generate_private_key(curve)
    material = key if private else key.public_key()
    entry = dict(json.loads(jwt.algorithms.ECAlgorithm.to_jwk(material)))
    entry["kid"] = "kid-vetted"
    return entry


def _rsa_jwk(bits: int) -> dict[str, Any]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=bits)
    numbers = key.public_key().public_numbers()

    def encode(value: int) -> str:
        raw = value.to_bytes((value.bit_length() + 7) // 8, "big")
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    return {"kty": "RSA", "n": encode(numbers.n), "e": encode(numbers.e), "kid": "kid-rsa"}


def test_a_jwk_carrying_unusable_or_private_material_is_never_a_key() -> None:
    """Key material is untrusted input: only the public half of an approved shape is ever built (§5.1.1)."""
    entry = signing_jwk()
    assert public_key_from_jwk(entry) is not None, "the accepted shape must still be accepted"
    assert public_key_from_jwk(_ec_jwk(ec.SECP384R1())) is None, "a curve this resource did not choose"
    assert public_key_from_jwk(_ec_jwk(ec.SECP256R1(), private=True)) is None, "a private key is not a verifier"
    assert public_key_from_jwk({**entry, "d": "aGV5LXNlY3JldA"}) is None
    assert public_key_from_jwk({"kty": "oct", "k": "c2VjcmV0", "kid": "k"}) is None
    # A 1-byte modulus would be an exponent-magnitude attack handed to `jwt.decode`.
    assert public_key_from_jwk({"kty": "RSA", "n": "AAAA", "e": "AQAB", "kid": "k"}) is None
    assert public_key_from_jwk(_rsa_jwk(1024)) is None, "under the modulus this resource will pay to parse"
    assert public_key_from_jwk(_rsa_jwk(2048)) is not None, "the smallest accepted modulus stays accepted"
    assert public_key_from_jwk("not even an object") is None


def test_a_scope_problem_is_a_403_and_never_a_credential_problem() -> None:
    """403 says which scope to go and ask for; 401 says the token itself is the problem (§5.3)."""
    principal = VerifiedPrincipal(issuer="https://idp.test", subject="caller", scopes=("aita:connect",))
    principal.require_scopes("aita:connect")
    with pytest.raises(AuthError) as caught:
        principal.require_scopes("aita:run")
    assert caught.value.status == 403
    assert caught.value.scope_hint == "aita:run"


# --------------------------------------------------------------------------------------
# the middleware's deadline
# --------------------------------------------------------------------------------------


def _scope(*, started: float) -> dict[str, Any]:
    return {
        "type": "http",
        "method": "POST",
        "path": MCP_PATH,
        "headers": [(b"authorization", b"Bearer whatever"), (b"content-type", b"application/json")],
        "state": {REQUEST_ID_STATE_KEY: "req_deadline", CALL_STARTED_STATE_KEY: started},
        "query_string": b"",
        "root_path": "",
    }


class _RecordingAuthenticator:
    """What the middleware handed to verification, on the clock the call arrived on."""

    def __init__(self) -> None:
        self.deadlines: list[float | None] = []

    async def authenticate(self, authorization: str | None, *, deadline: float | None = None) -> VerifiedPrincipal:
        self.deadlines.append(deadline)
        return VerifiedPrincipal(issuer="https://idp.test", subject="caller", scopes=ALL_SCOPES)

    async def close(self) -> None:
        return


async def _remaining(authenticator: _RecordingAuthenticator, started: float) -> float:
    """The allowance the middleware passed, measured on the loop's own clock."""
    sent: list[Any] = []

    async def inner(scope: dict[str, Any], _receive: Any, _send: Any) -> None:
        sent.append(scope)

    settings = auth_settings(mcp_tool_timeout_seconds=15.0, mcp_auth_timeout_seconds=2.0)
    await McpAuthMiddleware(
        inner, authenticator=authenticator, settings=settings
    )(_scope(started=started), lambda: None, lambda message: sent.append(message))
    assert sent, "the call never reached the application behind the middleware"
    deadline = authenticator.deadlines[0]
    assert deadline is not None, "verification was given no deadline at all"
    return deadline - asyncio.get_running_loop().time()


async def test_authentication_is_spent_from_the_clock_the_call_arrived_on() -> None:
    """§5.1.1: the 2 s authentication budget is deducted from the call, not started by it (§11).

    The transport records when a call arrived and every other deadline in the platform is measured from
    that moment. Verification restarting its own clock would let a request that had already spent most of
    its budget spend two more seconds on a JWKS fetch, so the ceiling this checks is the call's, not the
    authenticator's preference: 14.5 s of a 15 s budget gone leaves 0.5 s, which is under the 2 s
    authentication allowance and must therefore win.
    """
    authenticator = _RecordingAuthenticator()
    remaining = await _remaining(authenticator, time.monotonic() - 14.5)
    assert 0.0 < remaining <= 0.6, remaining


async def test_a_fresh_call_still_gets_the_whole_authentication_allowance() -> None:
    """The positive control: the budget is a cap, not a charge a new call pays up front."""
    authenticator = _RecordingAuthenticator()
    remaining = await _remaining(authenticator, time.monotonic())
    assert 1.5 < remaining <= 2.0, remaining


async def test_a_bearer_over_the_accepted_size_is_refused_before_anything_is_parsed() -> None:
    """16 KiB is a load bound, not a courtesy: 431 says the header is the problem (§5.1.1)."""
    authenticator = McpAuthenticator(auth_settings(auth_mode="dev", dev_engineer_token="t-1"))
    try:
        principal = await authenticator.authenticate("Bearer t-1")
        assert principal.subject == "dev-engineer", "a development token still stands in for a full grant"
        with pytest.raises(AuthError) as too_big:
            await authenticator.authenticate("Bearer " + "k" * (17 * 1024))
        assert too_big.value.status == 431
        with pytest.raises(AuthError) as absent:
            await authenticator.authenticate(None)
        assert absent.value.status == 401
    finally:
        await authenticator.close()
