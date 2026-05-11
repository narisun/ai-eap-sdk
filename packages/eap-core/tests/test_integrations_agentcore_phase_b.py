"""Tests for Phase B AgentCore adapters: Memory, Code Interpreter, Browser, JWT."""

from __future__ import annotations

import time
from typing import Any

import pytest

from eap_core.exceptions import RealRuntimeDisabledError
from eap_core.integrations.agentcore import (
    AgentCoreMemoryStore,
    InboundJwtVerifier,
    jwt_dependency,
    register_browser_tools,
    register_code_interpreter_tools,
)
from eap_core.mcp.registry import McpToolRegistry
from eap_core.memory import MemoryStore

# ---- AgentCoreMemoryStore ------------------------------------------------


def test_agentcore_memory_store_satisfies_memory_protocol():
    store = AgentCoreMemoryStore(memory_id="mem-1", region="us-east-1")
    assert isinstance(store, MemoryStore)


def test_agentcore_memory_construction_is_cheap():
    """Constructing the store must not import boto3 or hit the network."""
    import sys

    sys.modules.pop("boto3", None)
    store = AgentCoreMemoryStore(memory_id="m", region="us-east-1")
    assert "boto3" not in sys.modules
    _ = store


@pytest.fixture(autouse=True)
def _clear_real_flag(monkeypatch):
    monkeypatch.delenv("EAP_ENABLE_REAL_RUNTIMES", raising=False)


async def test_memory_remember_raises_without_env_flag():
    store = AgentCoreMemoryStore(memory_id="mem-1")
    with pytest.raises(RealRuntimeDisabledError, match="EAP_ENABLE_REAL_RUNTIMES"):
        await store.remember("session-1", "k", "v")


async def test_memory_recall_raises_without_env_flag():
    store = AgentCoreMemoryStore(memory_id="mem-1")
    with pytest.raises(RealRuntimeDisabledError, match="EAP_ENABLE_REAL_RUNTIMES"):
        await store.recall("session-1", "k")


async def test_memory_list_keys_raises_without_env_flag():
    store = AgentCoreMemoryStore(memory_id="mem-1")
    with pytest.raises(RealRuntimeDisabledError):
        await store.list_keys("session-1")


async def test_memory_forget_and_clear_raise_without_env_flag():
    store = AgentCoreMemoryStore(memory_id="mem-1")
    with pytest.raises(RealRuntimeDisabledError):
        await store.forget("session-1", "k")
    with pytest.raises(RealRuntimeDisabledError):
        await store.clear("session-1")


# ---- AgentCoreMemoryStore.recall — narrowed exception handling (H16) -----
#
# ``recall`` already catches only ``client.exceptions.ResourceNotFoundException``
# (the symmetric AgentCore-side guarantee); these tests pin that contract in
# place so a future widening of the ``except`` clause is caught immediately.
# boto3 dynamically generates exception classes on the client instance — the
# stubs below mimic that shape: ``client.exceptions.X`` resolves to a real
# Python exception type and ``raise`` instantiates it normally.


class _ResourceNotFoundException(Exception):  # noqa: N818  — mirrors boto3's class name
    """Stand-in for boto3's dynamically-generated ResourceNotFoundException."""


class _ThrottlingException(Exception):  # noqa: N818  — mirrors boto3's class name
    """Stand-in for boto3's dynamically-generated ThrottlingException."""


class _CredentialsError(Exception):
    """Stand-in for botocore.exceptions.NoCredentialsError / ClientError."""


class _FakeBotoExceptions:
    ResourceNotFoundException = _ResourceNotFoundException


class _FakeBotoClient:
    """Mimics enough of the boto3 client surface for ``recall`` to drive it."""

    exceptions = _FakeBotoExceptions()

    def __init__(self, *, raises: BaseException | None, response: dict | None = None):
        self._raises = raises
        self._response = response or {}

    def get_memory_record(self, **_kwargs):
        if self._raises is not None:
            raise self._raises
        return self._response


@pytest.mark.asyncio
async def test_agentcore_memory_recall_returns_none_on_not_found(monkeypatch):
    """``ResourceNotFoundException`` maps to ``None`` (cache miss)."""
    monkeypatch.setenv("EAP_ENABLE_REAL_RUNTIMES", "1")
    store = AgentCoreMemoryStore(memory_id="mem-1")
    fake = _FakeBotoClient(raises=_ResourceNotFoundException("absent"))
    monkeypatch.setattr(store, "_client", lambda: fake)

    assert await store.recall("session-1", "key-1") is None


@pytest.mark.asyncio
async def test_agentcore_memory_recall_propagates_throttle_error(monkeypatch):
    """ThrottlingException must propagate — NOT be silently masked (H16)."""
    monkeypatch.setenv("EAP_ENABLE_REAL_RUNTIMES", "1")
    store = AgentCoreMemoryStore(memory_id="mem-1")
    fake = _FakeBotoClient(raises=_ThrottlingException("429"))
    monkeypatch.setattr(store, "_client", lambda: fake)

    with pytest.raises(_ThrottlingException):
        await store.recall("session-1", "key-1")


@pytest.mark.asyncio
async def test_agentcore_memory_recall_propagates_credentials_error(monkeypatch):
    """Credentials / auth errors must propagate (H16)."""
    monkeypatch.setenv("EAP_ENABLE_REAL_RUNTIMES", "1")
    store = AgentCoreMemoryStore(memory_id="mem-1")
    fake = _FakeBotoClient(raises=_CredentialsError("no aws creds"))
    monkeypatch.setattr(store, "_client", lambda: fake)

    with pytest.raises(_CredentialsError):
        await store.recall("session-1", "key-1")


@pytest.mark.asyncio
async def test_agentcore_memory_recall_returns_value_on_hit(monkeypatch):
    """Happy-path: recordValue surfaces back to the caller."""
    monkeypatch.setenv("EAP_ENABLE_REAL_RUNTIMES", "1")
    store = AgentCoreMemoryStore(memory_id="mem-1")
    fake = _FakeBotoClient(raises=None, response={"recordValue": "hello"})
    monkeypatch.setattr(store, "_client", lambda: fake)

    assert await store.recall("session-1", "key-1") == "hello"


# ---- Code Interpreter tools ----------------------------------------------


def test_register_code_interpreter_tools_adds_three_tools():
    reg = McpToolRegistry()
    register_code_interpreter_tools(reg)
    names = {spec.name for spec in reg.list_tools()}
    assert names == {"execute_python", "execute_javascript", "execute_typescript"}


async def test_code_interpreter_tools_raise_without_env_flag():
    reg = McpToolRegistry()
    register_code_interpreter_tools(reg)
    py = reg.get("execute_python")
    assert py is not None
    with pytest.raises(RealRuntimeDisabledError):
        await py.fn(code="print('hi')")


def test_code_interpreter_tools_have_input_schemas():
    """Schemas are generated from type hints for MCP exposure."""
    reg = McpToolRegistry()
    register_code_interpreter_tools(reg)
    py = reg.get("execute_python")
    assert py is not None
    assert "code" in py.input_schema["properties"]
    assert py.input_schema["properties"]["code"]["type"] == "string"


# ---- Browser tools --------------------------------------------------------


def test_register_browser_tools_adds_five_tools():
    reg = McpToolRegistry()
    register_browser_tools(reg)
    names = {spec.name for spec in reg.list_tools()}
    assert names == {
        "browser_navigate",
        "browser_click",
        "browser_fill",
        "browser_extract_text",
        "browser_screenshot",
    }


async def test_browser_navigate_raises_without_env_flag():
    reg = McpToolRegistry()
    register_browser_tools(reg)
    nav = reg.get("browser_navigate")
    assert nav is not None
    with pytest.raises(RealRuntimeDisabledError):
        await nav.fn(url="https://example.com")


def test_browser_fill_schema_requires_two_args():
    reg = McpToolRegistry()
    register_browser_tools(reg)
    fill = reg.get("browser_fill")
    assert fill is not None
    assert set(fill.input_schema["properties"].keys()) == {"selector", "value"}
    assert "selector" in fill.input_schema["required"]
    assert "value" in fill.input_schema["required"]


def test_browser_extract_text_default_selector():
    """extract_text has a default body selector — not required in schema."""
    reg = McpToolRegistry()
    register_browser_tools(reg)
    et = reg.get("browser_extract_text")
    assert et is not None
    required = et.input_schema.get("required", [])
    assert "selector" not in required


# ---- InboundJwtVerifier --------------------------------------------------


def _make_test_keypair_and_jwks() -> tuple[str, str, dict[str, Any]]:
    """Return (private_key_pem, kid, jwks_dict) for tests."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from jwt.algorithms import RSAAlgorithm

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    pub_jwk_json = RSAAlgorithm.to_jwk(key.public_key())  # type: ignore[no-untyped-call]
    import json

    pub_jwk = json.loads(pub_jwk_json)
    pub_jwk["kid"] = "test-key-1"
    return pem, "test-key-1", {"keys": [pub_jwk]}


class _FakeResponse:
    def __init__(self, payload: dict[str, Any]):
        self._payload = payload

    def json(self) -> dict[str, Any]:
        return self._payload


def _make_http_get(meta: dict[str, Any], jwks: dict[str, Any]):
    def _get(url: str):
        if url.endswith("/.well-known/openid-configuration"):
            return _FakeResponse(meta)
        return _FakeResponse(jwks)

    return _get


def _issue_token(
    private_pem: str,
    kid: str,
    *,
    aud: str = "my-agent",
    scope: str = "agent:invoke",
    client_id: str = "client-1",
    extra: dict[str, Any] | None = None,
    exp_offset: int = 600,
) -> str:
    import jwt

    payload: dict[str, Any] = {
        "iss": "https://idp.example",
        "sub": "user-1",
        "aud": aud,
        "scope": scope,
        "client_id": client_id,
        "iat": int(time.time()),
        "exp": int(time.time()) + exp_offset,
    }
    if extra:
        payload.update(extra)
    return jwt.encode(payload, private_pem, algorithm="RS256", headers={"kid": kid})


def test_inbound_jwt_verifier_accepts_valid_token():
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
    token = _issue_token(pem, kid)
    claims = verifier.verify(token, http_get=_make_http_get(meta, jwks))
    assert claims["sub"] == "user-1"
    assert claims["aud"] == "my-agent"


def test_inbound_jwt_verifier_rejects_wrong_audience():
    import jwt as _jwt

    pem, kid, jwks = _make_test_keypair_and_jwks()
    meta = {
        "jwks_uri": "https://idp.example/.well-known/jwks.json",
        "issuer": "https://idp.example",
    }
    verifier = InboundJwtVerifier(
        discovery_url="https://idp.example/.well-known/openid-configuration",
        allowed_audiences=["other-agent"],
        issuer="https://idp.example",
    )
    token = _issue_token(pem, kid, aud="my-agent")
    with pytest.raises(_jwt.InvalidAudienceError):
        verifier.verify(token, http_get=_make_http_get(meta, jwks))


def test_inbound_jwt_verifier_rejects_disallowed_client():
    import jwt as _jwt

    pem, kid, jwks = _make_test_keypair_and_jwks()
    meta = {
        "jwks_uri": "https://idp.example/.well-known/jwks.json",
        "issuer": "https://idp.example",
    }
    verifier = InboundJwtVerifier(
        discovery_url="https://idp.example/.well-known/openid-configuration",
        allowed_audiences=["my-agent"],
        allowed_clients=["expected-client"],
        issuer="https://idp.example",
    )
    token = _issue_token(pem, kid, client_id="impostor")
    with pytest.raises(_jwt.InvalidTokenError, match="client_id"):
        verifier.verify(token, http_get=_make_http_get(meta, jwks))


def test_inbound_jwt_verifier_rejects_missing_scope():
    import jwt as _jwt

    pem, kid, jwks = _make_test_keypair_and_jwks()
    meta = {
        "jwks_uri": "https://idp.example/.well-known/jwks.json",
        "issuer": "https://idp.example",
    }
    verifier = InboundJwtVerifier(
        discovery_url="https://idp.example/.well-known/openid-configuration",
        allowed_audiences=["my-agent"],
        allowed_scopes=["agent:admin"],
        issuer="https://idp.example",
    )
    token = _issue_token(pem, kid, scope="agent:read")
    with pytest.raises(_jwt.InvalidTokenError, match="scope"):
        verifier.verify(token, http_get=_make_http_get(meta, jwks))


def test_inbound_jwt_verifier_rejects_unknown_kid():
    import jwt as _jwt

    pem, _kid, jwks = _make_test_keypair_and_jwks()
    meta = {
        "jwks_uri": "https://idp.example/.well-known/jwks.json",
        "issuer": "https://idp.example",
    }
    verifier = InboundJwtVerifier(
        discovery_url="https://idp.example/.well-known/openid-configuration",
        allowed_audiences=["my-agent"],
        issuer="https://idp.example",
    )
    # Issue token with a kid the JWKS doesn't know about
    token = _issue_token(pem, "unknown-kid")
    with pytest.raises(_jwt.InvalidTokenError, match="kid"):
        verifier.verify(token, http_get=_make_http_get(meta, jwks))


def test_inbound_jwt_verifier_rejects_expired_token():
    import jwt as _jwt

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
    token = _issue_token(pem, kid, exp_offset=-60)  # expired 1 minute ago
    with pytest.raises(_jwt.ExpiredSignatureError):
        verifier.verify(token, http_get=_make_http_get(meta, jwks))


def test_inbound_jwt_verifier_caches_jwks():
    """Repeated verify calls should not refetch JWKS within the TTL."""
    pem, kid, jwks = _make_test_keypair_and_jwks()
    meta = {
        "jwks_uri": "https://idp.example/.well-known/jwks.json",
        "issuer": "https://idp.example",
    }
    call_count = {"n": 0}

    def _counting_get(url: str):
        call_count["n"] += 1
        if url.endswith("/.well-known/openid-configuration"):
            return _FakeResponse(meta)
        return _FakeResponse(jwks)

    verifier = InboundJwtVerifier(
        discovery_url="https://idp.example/.well-known/openid-configuration",
        allowed_audiences=["my-agent"],
        issuer="https://idp.example",
    )
    token = _issue_token(pem, kid)
    verifier.verify(token, http_get=_counting_get)
    first_count = call_count["n"]
    verifier.verify(token, http_get=_counting_get)
    # Second call should not have refetched (cache hit).
    assert call_count["n"] == first_count


# ---- jwt_dependency (FastAPI) -------------------------------------------


@pytest.mark.extras
def test_jwt_dependency_requires_fastapi():
    """Calling jwt_dependency without [a2a] extra raises ImportError.

    With [a2a] installed (extras matrix), the dependency factory returns
    a callable. We verify the latter path here; the former is exercised
    by absence in the default test-core matrix.
    """
    pytest.importorskip("fastapi")
    verifier = InboundJwtVerifier(
        discovery_url="https://idp.example/.well-known/openid-configuration",
        allowed_audiences=["my-agent"],
        issuer="https://idp.example",
    )
    dep = jwt_dependency(verifier)
    assert callable(dep)
