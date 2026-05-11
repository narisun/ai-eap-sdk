"""Tests for ``InboundJwtVerifier`` construction-time invariants (C4).

The verifier MUST refuse to be constructed without at least one
``allowed_audience``, and ``verify`` MUST always require the ``exp``,
``iat``, and ``aud`` claims to be present (defense against
"unbounded-lifetime" tokens or tokens that simply omit ``aud``).
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
        )


def test_requires_audience_kwarg_explicitly() -> None:
    """Omitting the kwarg entirely is a TypeError — not silently accepted."""
    from eap_core.integrations.agentcore import InboundJwtVerifier

    with pytest.raises(TypeError):
        InboundJwtVerifier(  # type: ignore[call-arg]
            discovery_url="https://idp.example/.well-known/openid-configuration",
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
    meta = {"jwks_uri": "https://idp.example/.well-known/jwks.json"}
    verifier = InboundJwtVerifier(
        discovery_url="https://idp.example/.well-known/openid-configuration",
        allowed_audiences=["my-agent"],
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
    meta = {"jwks_uri": "https://idp.example/.well-known/jwks.json"}
    verifier = InboundJwtVerifier(
        discovery_url="https://idp.example/.well-known/openid-configuration",
        allowed_audiences=["my-agent"],
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
    meta = {"jwks_uri": "https://idp.example/.well-known/jwks.json"}
    verifier = InboundJwtVerifier(
        discovery_url="https://idp.example/.well-known/openid-configuration",
        allowed_audiences=["my-agent"],
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
