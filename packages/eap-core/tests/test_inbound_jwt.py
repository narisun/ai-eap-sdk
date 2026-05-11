"""Tests for ``InboundJwtVerifier`` construction-time invariants (C4) and
JWKS/issuer trust pinning (C1, C2, C3).

The verifier MUST refuse to be constructed without at least one
``allowed_audience`` and without an ``issuer``, MUST reject plaintext
discovery URLs, MUST refuse to follow a ``jwks_uri`` to a different
origin than ``discovery_url``, and ``verify`` MUST always require the
``exp``, ``iat``, ``aud``, and ``iss`` claims to be present (defense
against "unbounded-lifetime" tokens or tokens that simply omit
``aud``/``iss``).
"""

from __future__ import annotations

import time
from typing import Any

import pytest

# ---- C4: audience validation is mandatory --------------------------------


def test_requires_at_least_one_audience() -> None:
    from eap_core.integrations.agentcore import InboundJwtVerifier

    with pytest.raises(ValueError, match="allowed_audience"):
        InboundJwtVerifier(
            discovery_url="https://idp.example/.well-known/openid-configuration",
            allowed_audiences=[],
            issuer="https://idp.example",
        )


def test_requires_audience_kwarg_explicitly() -> None:
    """Omitting the kwarg entirely is a TypeError — not silently accepted."""
    from eap_core.integrations.agentcore import InboundJwtVerifier

    with pytest.raises(TypeError):
        InboundJwtVerifier(  # type: ignore[call-arg]
            discovery_url="https://idp.example/.well-known/openid-configuration",
            issuer="https://idp.example",
        )


# ---- C2: issuer is a required constructor arg -----------------------------


def test_requires_issuer() -> None:
    """Omitting ``issuer`` is a TypeError — pinning is mandatory."""
    from eap_core.integrations.agentcore import InboundJwtVerifier

    with pytest.raises(TypeError):
        InboundJwtVerifier(  # type: ignore[call-arg]
            discovery_url="https://idp.example/.well-known/openid-configuration",
            allowed_audiences=["agent"],
            # issuer missing
        )


# ---- C1: scheme + same-host enforcement on discovery_url + jwks_uri -------


def test_rejects_http_discovery_url() -> None:
    """A plaintext ``http://`` discovery URL is a MITM hole — reject."""
    from eap_core.integrations.agentcore import InboundJwtVerifier

    with pytest.raises(ValueError, match="https"):
        InboundJwtVerifier(
            discovery_url="http://idp.example/.well-known/openid-configuration",
            allowed_audiences=["agent"],
            issuer="https://idp.example",
        )


def test_rejects_cross_host_jwks_uri() -> None:
    """A discovery doc that points jwks_uri at a different origin is rejected."""
    from eap_core.integrations.agentcore import InboundJwtVerifier

    class FakeResp:
        def __init__(self, data: dict[str, Any]) -> None:
            self._data = data

        def json(self) -> dict[str, Any]:
            return self._data

    verifier = InboundJwtVerifier(
        discovery_url="https://idp.example/.well-known/openid-configuration",
        allowed_audiences=["agent"],
        issuer="https://idp.example",
    )

    def http_get(url: str) -> FakeResp:
        if url == "https://idp.example/.well-known/openid-configuration":
            return FakeResp(
                {
                    "jwks_uri": "https://attacker.example/jwks",
                    "issuer": "https://idp.example",
                }
            )
        return FakeResp({"keys": []})

    with pytest.raises(ValueError, match="same host"):
        verifier._refresh_jwks(http_get)


def test_rejects_http_jwks_uri() -> None:
    """A discovery doc that advertises an http:// jwks_uri is rejected."""
    from eap_core.integrations.agentcore import InboundJwtVerifier

    class FakeResp:
        def __init__(self, data: dict[str, Any]) -> None:
            self._data = data

        def json(self) -> dict[str, Any]:
            return self._data

    verifier = InboundJwtVerifier(
        discovery_url="https://idp.example/.well-known/openid-configuration",
        allowed_audiences=["agent"],
        issuer="https://idp.example",
    )

    def http_get(url: str) -> FakeResp:
        return FakeResp({"jwks_uri": "http://idp.example/jwks", "issuer": "https://idp.example"})

    with pytest.raises(ValueError, match="https"):
        verifier._refresh_jwks(http_get)


def test_rejects_mismatched_advertised_issuer() -> None:
    """Discovery doc's ``issuer`` field must match the configured issuer."""
    from eap_core.integrations.agentcore import InboundJwtVerifier

    class FakeResp:
        def __init__(self, data: dict[str, Any]) -> None:
            self._data = data

        def json(self) -> dict[str, Any]:
            return self._data

    verifier = InboundJwtVerifier(
        discovery_url="https://idp.example/.well-known/openid-configuration",
        allowed_audiences=["agent"],
        issuer="https://idp.example",
    )

    def http_get(url: str) -> FakeResp:
        if url.endswith("/.well-known/openid-configuration"):
            return FakeResp(
                {
                    "jwks_uri": "https://idp.example/jwks",
                    "issuer": "https://attacker.example",
                }
            )
        return FakeResp({"keys": []})

    with pytest.raises(ValueError, match="issuer"):
        verifier._refresh_jwks(http_get)


def test_rejects_discovery_doc_without_issuer_field() -> None:
    """OIDC Discovery 1.0 §3 makes ``issuer`` REQUIRED — a missing field
    must not silently bypass the cross-check."""
    from eap_core.integrations.agentcore import InboundJwtVerifier

    class FakeResp:
        def __init__(self, data: dict[str, Any]) -> None:
            self._data = data

        def json(self) -> dict[str, Any]:
            return self._data

    verifier = InboundJwtVerifier(
        discovery_url="https://idp.example/.well-known/openid-configuration",
        allowed_audiences=["agent"],
        issuer="https://idp.example",
    )

    def http_get(url: str) -> FakeResp:
        if url.endswith("/.well-known/openid-configuration"):
            # jwks_uri present, but ``issuer`` deliberately omitted.
            return FakeResp({"jwks_uri": "https://idp.example/jwks"})
        return FakeResp({"keys": []})

    with pytest.raises(ValueError, match="no 'issuer' field"):
        verifier._refresh_jwks(http_get)


def test_accepts_mixed_case_jwks_host() -> None:
    """RFC 3986 §3.2.2: host is case-insensitive. ``IDP.example`` and
    ``idp.example`` are the same origin."""
    from eap_core.integrations.agentcore import InboundJwtVerifier

    class FakeResp:
        def __init__(self, data: dict[str, Any]) -> None:
            self._data = data

        def json(self) -> dict[str, Any]:
            return self._data

    verifier = InboundJwtVerifier(
        discovery_url="https://idp.example/.well-known/openid-configuration",
        allowed_audiences=["agent"],
        issuer="https://idp.example",
    )

    def http_get(url: str) -> FakeResp:
        if url.endswith("/.well-known/openid-configuration"):
            return FakeResp(
                {
                    "jwks_uri": "https://IDP.example/jwks",
                    "issuer": "https://idp.example",
                }
            )
        return FakeResp({"keys": []})

    # Must NOT raise — mixed-case host is the same origin.
    verifier._refresh_jwks(http_get)


def test_accepts_explicit_default_https_port_in_jwks_uri() -> None:
    """``https://idp.example`` and ``https://idp.example:443`` are the
    same origin — implicit default port must compare equal to explicit
    ``:443``."""
    from eap_core.integrations.agentcore import InboundJwtVerifier

    class FakeResp:
        def __init__(self, data: dict[str, Any]) -> None:
            self._data = data

        def json(self) -> dict[str, Any]:
            return self._data

    verifier = InboundJwtVerifier(
        discovery_url="https://idp.example/.well-known/openid-configuration",
        allowed_audiences=["agent"],
        issuer="https://idp.example",
    )

    def http_get(url: str) -> FakeResp:
        if url.endswith("/.well-known/openid-configuration"):
            return FakeResp(
                {
                    "jwks_uri": "https://idp.example:443/jwks",
                    "issuer": "https://idp.example",
                }
            )
        return FakeResp({"keys": []})

    # Must NOT raise — explicit :443 is the default https port.
    verifier._refresh_jwks(http_get)


# ---- Issuer scheme validation at construction -----------------------------


def test_rejects_http_issuer() -> None:
    """An ``http://`` issuer is rejected — we won't pin to a plaintext
    issuer string while claiming https-only discovery."""
    from eap_core.integrations.agentcore import InboundJwtVerifier

    with pytest.raises(ValueError, match="http://"):
        InboundJwtVerifier(
            discovery_url="https://idp.example/.well-known/openid-configuration",
            allowed_audiences=["agent"],
            issuer="http://idp.example",
        )


# ---- C4: require=["exp","iat","aud"] is enforced --------------------------


def _make_test_keypair_and_jwks() -> tuple[str, str, dict[str, Any]]:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from jwt.algorithms import RSAAlgorithm

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    import json

    pub_jwk = json.loads(RSAAlgorithm.to_jwk(key.public_key()))  # type: ignore[no-untyped-call]
    pub_jwk["kid"] = "test-key-1"
    return pem, "test-key-1", {"keys": [pub_jwk]}


class _FakeResponse:
    def __init__(self, payload: dict[str, Any]):
        self._payload = payload

    def json(self) -> dict[str, Any]:
        return self._payload


def _make_http_get(meta: dict[str, Any], jwks: dict[str, Any]):
    def _get(url: str) -> _FakeResponse:
        if url.endswith("/.well-known/openid-configuration"):
            return _FakeResponse(meta)
        return _FakeResponse(jwks)

    return _get


def _encode_token(private_pem: str, kid: str, payload: dict[str, Any]) -> str:
    import jwt

    return jwt.encode(payload, private_pem, algorithm="RS256", headers={"kid": kid})


def test_token_without_exp_is_rejected() -> None:
    """A token missing the ``exp`` claim is rejected — `require` enforced."""
    import jwt as _jwt

    from eap_core.integrations.agentcore import InboundJwtVerifier

    pem, kid, jwks = _make_test_keypair_and_jwks()
    meta = {
        "jwks_uri": "https://idp.example/.well-known/jwks.json",
        "issuer": "https://idp.example",
    }
    verifier = InboundJwtVerifier(
        discovery_url="https://idp.example/.well-known/openid-configuration",
        allowed_audiences=["my-agent"],
        issuer="https://idp.example",
    )
    # iat + aud but no exp.
    token = _encode_token(
        pem,
        kid,
        {
            "iss": "https://idp.example",
            "sub": "user-1",
            "aud": "my-agent",
            "iat": int(time.time()),
        },
    )
    with pytest.raises(_jwt.MissingRequiredClaimError, match="exp"):
        verifier.verify(token, http_get=_make_http_get(meta, jwks))


def test_token_without_iat_is_rejected() -> None:
    import jwt as _jwt

    from eap_core.integrations.agentcore import InboundJwtVerifier

    pem, kid, jwks = _make_test_keypair_and_jwks()
    meta = {
        "jwks_uri": "https://idp.example/.well-known/jwks.json",
        "issuer": "https://idp.example",
    }
    verifier = InboundJwtVerifier(
        discovery_url="https://idp.example/.well-known/openid-configuration",
        allowed_audiences=["my-agent"],
        issuer="https://idp.example",
    )
    token = _encode_token(
        pem,
        kid,
        {
            "iss": "https://idp.example",
            "sub": "user-1",
            "aud": "my-agent",
            "exp": int(time.time()) + 600,
        },
    )
    with pytest.raises(_jwt.MissingRequiredClaimError, match="iat"):
        verifier.verify(token, http_get=_make_http_get(meta, jwks))


def test_token_without_aud_is_rejected() -> None:
    import jwt as _jwt

    from eap_core.integrations.agentcore import InboundJwtVerifier

    pem, kid, jwks = _make_test_keypair_and_jwks()
    meta = {
        "jwks_uri": "https://idp.example/.well-known/jwks.json",
        "issuer": "https://idp.example",
    }
    verifier = InboundJwtVerifier(
        discovery_url="https://idp.example/.well-known/openid-configuration",
        allowed_audiences=["my-agent"],
        issuer="https://idp.example",
    )
    token = _encode_token(
        pem,
        kid,
        {
            "iss": "https://idp.example",
            "sub": "user-1",
            "iat": int(time.time()),
            "exp": int(time.time()) + 600,
        },
    )
    # PyJWT raises ``MissingRequiredClaimError("aud")`` because ``require``
    # includes "aud" — this is the regression test for C4.
    with pytest.raises(_jwt.MissingRequiredClaimError, match="aud"):
        verifier.verify(token, http_get=_make_http_get(meta, jwks))


def test_token_with_wrong_issuer_is_rejected() -> None:
    """A token whose ``iss`` claim disagrees with the configured issuer
    is rejected — closes C2."""
    import jwt as _jwt

    from eap_core.integrations.agentcore import InboundJwtVerifier

    pem, kid, jwks = _make_test_keypair_and_jwks()
    meta = {
        "jwks_uri": "https://idp.example/.well-known/jwks.json",
        "issuer": "https://idp.example",
    }
    verifier = InboundJwtVerifier(
        discovery_url="https://idp.example/.well-known/openid-configuration",
        allowed_audiences=["my-agent"],
        issuer="https://idp.example",
    )
    token = _encode_token(
        pem,
        kid,
        {
            "iss": "https://attacker.example",
            "sub": "user-1",
            "aud": "my-agent",
            "iat": int(time.time()),
            "exp": int(time.time()) + 600,
        },
    )
    with pytest.raises(_jwt.InvalidIssuerError):
        verifier.verify(token, http_get=_make_http_get(meta, jwks))


# ---- Sync/async cache parity: empty JWKS must not refetch within TTL ------


def test_sync_verify_does_not_refetch_empty_jwks_within_ttl() -> None:
    """Empty-but-valid JWKS response must not cause infinite refetch on sync path.

    Locks parity with ``_amaybe_refresh_jwks``: the cache-populated signal
    is the timestamp, not the truthiness of ``self._jwks``. An IdP that
    legitimately returns ``{"keys": []}`` would otherwise re-fetch on every
    ``verify()`` call.
    """
    import jwt

    from eap_core.integrations.agentcore import InboundJwtVerifier

    fetches: list[str] = []

    class FakeResp:
        def __init__(self, payload: dict[str, Any]) -> None:
            self._payload = payload

        def json(self) -> dict[str, Any]:
            return self._payload

    def fake_http_get(url: str) -> FakeResp:
        fetches.append(url)
        if "well-known" in url:
            return FakeResp(
                {"issuer": "https://idp.example", "jwks_uri": "https://idp.example/jwks"}
            )
        return FakeResp({"keys": []})

    verifier = InboundJwtVerifier(
        discovery_url="https://idp.example/.well-known/openid-configuration",
        issuer="https://idp.example",
        allowed_audiences=["agent"],
        jwks_cache_ttl_seconds=600,
    )

    # First call refreshes (cold cache).
    try:
        verifier.verify("not-a-real-token", http_get=fake_http_get)
    except (jwt.InvalidTokenError, Exception):
        pass
    first_fetch_count = len(fetches)
    assert first_fetch_count == 2  # discovery + jwks

    # Second call within TTL — must NOT refetch.
    try:
        verifier.verify("another-not-a-real-token", http_get=fake_http_get)
    except (jwt.InvalidTokenError, Exception):
        pass
    assert len(fetches) == first_fetch_count, "sync verify re-fetched within TTL on empty JWKS"


# ---- H-N2: averify is async + JWKS refresh single-flights -----------------


@pytest.mark.asyncio
async def test_averify_is_async_and_uses_async_http() -> None:
    """``averify`` must be a coroutine function so the FastAPI dependency
    doesn't block the event loop on JWKS fetch."""
    import inspect

    from eap_core.integrations.agentcore import InboundJwtVerifier

    verifier = InboundJwtVerifier(
        discovery_url="https://idp.example/.well-known/openid-configuration",
        issuer="https://idp.example",
        allowed_audiences=["agent"],
    )
    assert inspect.iscoroutinefunction(verifier.averify)


@pytest.mark.asyncio
async def test_averify_single_flights_concurrent_jwks_refresh() -> None:
    """20 concurrent ``averify()`` calls on a cold cache must fetch each
    URL at most once — the ``_refresh_lock`` single-flights the refresh."""
    import asyncio as _asyncio

    from eap_core.integrations.agentcore import InboundJwtVerifier

    verifier = InboundJwtVerifier(
        discovery_url="https://idp.example/.well-known/openid-configuration",
        issuer="https://idp.example",
        allowed_audiences=["agent"],
        jwks_cache_ttl_seconds=600,
    )

    fetches: list[str] = []

    class FakeAsyncHttp:
        async def get(self, url: str) -> Any:
            fetches.append(url)

            class _R:
                def json(self_inner) -> dict[str, Any]:  # noqa: N805
                    if "well-known" in url:
                        return {
                            "issuer": "https://idp.example",
                            "jwks_uri": "https://idp.example/jwks",
                        }
                    return {"keys": []}

            return _R()

        async def aclose(self) -> None:
            return None

    # All 20 calls share one fake http client; each call's verify step is
    # expected to fail (no JWKS key matches the kid), but the *refresh*
    # side-effect is what we assert on.
    http = FakeAsyncHttp()

    async def call() -> None:
        try:
            await verifier.averify("not-a-real-token", http=http)
        except Exception:
            pass

    await _asyncio.gather(*[call() for _ in range(20)])

    assert fetches.count("https://idp.example/.well-known/openid-configuration") == 1
    assert fetches.count("https://idp.example/jwks") == 1


# ---- M-N5: jwt_dependency HTTPException detail is sanitized ---------------


@pytest.mark.asyncio
async def test_jwt_dependency_does_not_leak_internal_error_detail() -> None:
    """The HTTPException detail must be a fixed sanitized string —
    raw PyJWT error text (which can carry attacker-controlled ``kid``
    values or verifier configuration) must never reach the response."""
    pytest.importorskip("fastapi")
    from fastapi import HTTPException

    from eap_core.integrations.agentcore import InboundJwtVerifier, jwt_dependency

    verifier = InboundJwtVerifier(
        discovery_url="https://idp.example/.well-known/openid-configuration",
        issuer="https://idp.example",
        allowed_audiences=["agent"],
    )
    dep = jwt_dependency(verifier)

    class _FakeCreds:
        scheme = "Bearer"
        credentials = "garbage-not-a-jwt-<kid=attacker-controlled>"

    with pytest.raises(HTTPException) as exc_info:
        await dep(credentials=_FakeCreds())
    assert exc_info.value.status_code == 401
    assert exc_info.value.detail in {"invalid token", "missing bearer token"}
    # Defense-in-depth: ``from None`` must clear the chain so middleware
    # that prints __cause__ can't leak the verifier's exception.
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__suppress_context__ is True  # blocks __context__ from default traceback
