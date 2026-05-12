"""Regression tests for runtime exception hierarchy + vendor mapping (Finding 4).

Verifies:

1. Canonical hierarchy shape (``RuntimeAdapterError`` subtree).
2. Bedrock ``_map_botocore_error`` maps vendor codes to canonical types.
3. Vertex ``_map_google_error`` maps vendor exceptions to canonical types.
4. Cause chain preserved when call sites use ``raise ... from exc``.
5. The gated ``generate()`` call site re-raises canonical types when the
   vendor SDK raises (verified via ``unittest.mock.patch``).

Vendor-specific tests are gated by ``pytest.importorskip`` and tagged
``extras`` so they skip cleanly when ``[aws]`` / ``[gcp]`` extras are
not installed (the extras CI matrix exercises them separately).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from eap_core.runtimes.errors import (
    RuntimeAdapterError,
    RuntimeAuthError,
    RuntimeContextLengthError,
    RuntimeRateLimitError,
    RuntimeServerError,
    RuntimeTimeoutError,
)

# ---------------------------------------------------------------------------
# Hierarchy smoke (no vendor deps — runs under bare gauntlet)
# ---------------------------------------------------------------------------


def test_hierarchy_smoke() -> None:
    """Every canonical subtype must inherit ``RuntimeAdapterError``."""
    for cls in (
        RuntimeAuthError,
        RuntimeRateLimitError,
        RuntimeTimeoutError,
        RuntimeServerError,
        RuntimeContextLengthError,
    ):
        assert issubclass(cls, RuntimeAdapterError)
    assert issubclass(RuntimeAdapterError, Exception)


# ---------------------------------------------------------------------------
# Bedrock mapping tests (require [aws] extra → botocore)
# ---------------------------------------------------------------------------


botocore = pytest.importorskip("botocore")


def _client_error(code: str, message: str = "test") -> Exception:
    from botocore.exceptions import ClientError

    return ClientError(
        error_response={"Error": {"Code": code, "Message": message}},
        operation_name="InvokeModel",
    )


@pytest.mark.extras
def test_bedrock_maps_access_denied_to_auth_error() -> None:
    from eap_core.runtimes.bedrock import _map_botocore_error

    mapped = _map_botocore_error(_client_error("AccessDeniedException", "no perms"))
    assert isinstance(mapped, RuntimeAuthError)


@pytest.mark.extras
def test_bedrock_maps_unauthorized_to_auth_error() -> None:
    from eap_core.runtimes.bedrock import _map_botocore_error

    mapped = _map_botocore_error(_client_error("UnauthorizedException", "missing token"))
    assert isinstance(mapped, RuntimeAuthError)


@pytest.mark.extras
def test_bedrock_maps_throttling_to_rate_limit() -> None:
    from eap_core.runtimes.bedrock import _map_botocore_error

    mapped = _map_botocore_error(_client_error("ThrottlingException", "slow down"))
    assert isinstance(mapped, RuntimeRateLimitError)


@pytest.mark.extras
def test_bedrock_maps_too_many_requests_to_rate_limit() -> None:
    from eap_core.runtimes.bedrock import _map_botocore_error

    mapped = _map_botocore_error(_client_error("TooManyRequestsException", "stop"))
    assert isinstance(mapped, RuntimeRateLimitError)


@pytest.mark.extras
def test_bedrock_maps_service_unavailable_to_server_error() -> None:
    from eap_core.runtimes.bedrock import _map_botocore_error

    mapped = _map_botocore_error(_client_error("ServiceUnavailableException", "down"))
    assert isinstance(mapped, RuntimeServerError)


@pytest.mark.extras
def test_bedrock_maps_validation_context_length_to_context_error() -> None:
    from eap_core.runtimes.bedrock import _map_botocore_error

    mapped = _map_botocore_error(
        _client_error("ValidationException", "input exceeds context length")
    )
    assert isinstance(mapped, RuntimeContextLengthError)


@pytest.mark.extras
def test_bedrock_maps_unknown_code_to_base_error() -> None:
    from eap_core.runtimes.bedrock import _map_botocore_error

    mapped = _map_botocore_error(_client_error("MystifyingNewException", "huh"))
    # An unknown code should fall back to the base type so middleware can
    # still ``except RuntimeAdapterError`` without losing the failure.
    assert isinstance(mapped, RuntimeAdapterError)
    assert not isinstance(
        mapped,
        (
            RuntimeAuthError,
            RuntimeRateLimitError,
            RuntimeServerError,
            RuntimeContextLengthError,
            RuntimeTimeoutError,
        ),
    )


@pytest.mark.extras
def test_bedrock_maps_endpoint_connection_to_server_error() -> None:
    from botocore.exceptions import EndpointConnectionError

    from eap_core.runtimes.bedrock import _map_botocore_error

    mapped = _map_botocore_error(EndpointConnectionError(endpoint_url="https://x"))
    assert isinstance(mapped, RuntimeServerError)


@pytest.mark.extras
def test_bedrock_maps_read_timeout_to_timeout_error() -> None:
    from botocore.exceptions import ReadTimeoutError

    from eap_core.runtimes.bedrock import _map_botocore_error

    mapped = _map_botocore_error(ReadTimeoutError(endpoint_url="https://x"))
    assert isinstance(mapped, RuntimeTimeoutError)


@pytest.mark.extras
async def test_bedrock_generate_translates_vendor_exception(monkeypatch) -> None:
    """If the gated real call raises a botocore ClientError, generate()
    re-raises the canonical EAP-Core type (with ``__cause__`` preserved)."""
    from eap_core.config import RuntimeConfig
    from eap_core.runtimes.bedrock import BedrockRuntimeAdapter
    from eap_core.types import Message, Request

    monkeypatch.setenv("EAP_ENABLE_REAL_RUNTIMES", "1")

    vendor_exc = _client_error("AccessDeniedException", "denied")
    fake_client = MagicMock()
    fake_client.converse.side_effect = vendor_exc
    fake_boto3 = MagicMock()
    fake_boto3.client.return_value = fake_client

    adapter = BedrockRuntimeAdapter(
        RuntimeConfig(
            provider="bedrock",
            model="anthropic.claude-3-5-sonnet",
            options={"region": "us-east-1"},
        )
    )

    with patch.dict("sys.modules", {"boto3": fake_boto3}):
        with pytest.raises(RuntimeAuthError) as exc_info:
            await adapter.generate(
                Request(
                    model="anthropic.claude-3-5-sonnet",
                    messages=[Message(role="user", content="hi")],
                )
            )

    # The original vendor exception must be reachable for audit inspection.
    assert exc_info.value.__cause__ is vendor_exc


@pytest.mark.extras
async def test_bedrock_generate_translates_throttling(monkeypatch) -> None:
    """ThrottlingException at the call site must surface as RuntimeRateLimitError."""
    from eap_core.config import RuntimeConfig
    from eap_core.runtimes.bedrock import BedrockRuntimeAdapter
    from eap_core.types import Message, Request

    monkeypatch.setenv("EAP_ENABLE_REAL_RUNTIMES", "1")

    vendor_exc = _client_error("ThrottlingException", "rate")
    fake_client = MagicMock()
    fake_client.converse.side_effect = vendor_exc
    fake_boto3 = MagicMock()
    fake_boto3.client.return_value = fake_client

    adapter = BedrockRuntimeAdapter(
        RuntimeConfig(provider="bedrock", model="m", options={"region": "us-east-1"})
    )

    with patch.dict("sys.modules", {"boto3": fake_boto3}):
        with pytest.raises(RuntimeRateLimitError) as exc_info:
            await adapter.generate(
                Request(model="m", messages=[Message(role="user", content="hi")])
            )

    assert exc_info.value.__cause__ is vendor_exc


# ---------------------------------------------------------------------------
# Vertex mapping tests (require [gcp] extra → google.api_core)
# ---------------------------------------------------------------------------


google_api_core = pytest.importorskip("google.api_core")


@pytest.mark.extras
def test_vertex_maps_permission_denied_to_auth_error() -> None:
    from google.api_core.exceptions import PermissionDenied

    from eap_core.runtimes.vertex import _map_google_error

    mapped = _map_google_error(PermissionDenied("nope"))
    assert isinstance(mapped, RuntimeAuthError)


@pytest.mark.extras
def test_vertex_maps_resource_exhausted_to_rate_limit() -> None:
    from google.api_core.exceptions import ResourceExhausted

    from eap_core.runtimes.vertex import _map_google_error

    mapped = _map_google_error(ResourceExhausted("quota"))
    assert isinstance(mapped, RuntimeRateLimitError)


@pytest.mark.extras
def test_vertex_maps_deadline_exceeded_to_timeout() -> None:
    from google.api_core.exceptions import DeadlineExceeded

    from eap_core.runtimes.vertex import _map_google_error

    mapped = _map_google_error(DeadlineExceeded("too slow"))
    assert isinstance(mapped, RuntimeTimeoutError)


@pytest.mark.extras
def test_vertex_maps_internal_server_to_server_error() -> None:
    from google.api_core.exceptions import InternalServerError

    from eap_core.runtimes.vertex import _map_google_error

    mapped = _map_google_error(InternalServerError("boom"))
    assert isinstance(mapped, RuntimeServerError)


@pytest.mark.extras
def test_vertex_maps_service_unavailable_to_server_error() -> None:
    from google.api_core.exceptions import ServiceUnavailable

    from eap_core.runtimes.vertex import _map_google_error

    mapped = _map_google_error(ServiceUnavailable("503"))
    assert isinstance(mapped, RuntimeServerError)


@pytest.mark.extras
def test_vertex_maps_invalid_argument_context_length_to_context_error() -> None:
    from google.api_core.exceptions import InvalidArgument

    from eap_core.runtimes.vertex import _map_google_error

    mapped = _map_google_error(InvalidArgument("request exceeds context length of model"))
    assert isinstance(mapped, RuntimeContextLengthError)


@pytest.mark.extras
def test_vertex_maps_unknown_google_error_to_base_error() -> None:
    from google.api_core.exceptions import GoogleAPICallError

    from eap_core.runtimes.vertex import _map_google_error

    class _NovelError(GoogleAPICallError):  # type: ignore[misc,unused-ignore]
        pass

    mapped = _map_google_error(_NovelError("dunno"))
    assert isinstance(mapped, RuntimeAdapterError)
    assert not isinstance(
        mapped,
        (
            RuntimeAuthError,
            RuntimeRateLimitError,
            RuntimeServerError,
            RuntimeContextLengthError,
            RuntimeTimeoutError,
        ),
    )


@pytest.mark.extras
def test_vertex_maps_generic_exception_to_base_error() -> None:
    from eap_core.runtimes.vertex import _map_google_error

    mapped = _map_google_error(ValueError("random non-google error"))
    assert isinstance(mapped, RuntimeAdapterError)


@pytest.mark.extras
async def test_vertex_generate_translates_vendor_exception(monkeypatch) -> None:
    """If the gated real Vertex call raises a google.api_core exception,
    generate() re-raises the canonical EAP-Core type with ``__cause__``
    preserved."""
    from google.api_core.exceptions import PermissionDenied

    from eap_core.config import RuntimeConfig
    from eap_core.runtimes.vertex import VertexRuntimeAdapter
    from eap_core.types import Message, Request

    monkeypatch.setenv("EAP_ENABLE_REAL_RUNTIMES", "1")

    vendor_exc = PermissionDenied("no access")
    fake_model = MagicMock()
    fake_model.generate_content.side_effect = vendor_exc
    fake_generative_models = MagicMock()
    fake_generative_models.GenerativeModel.return_value = fake_model
    fake_vertexai = MagicMock()
    fake_vertexai.generative_models = fake_generative_models

    adapter = VertexRuntimeAdapter(
        RuntimeConfig(
            provider="vertex",
            model="gemini-1.5-pro",
            options={"project": "p", "location": "us-central1"},
        )
    )

    with patch.dict(
        "sys.modules",
        {
            "vertexai": fake_vertexai,
            "vertexai.generative_models": fake_generative_models,
        },
    ):
        with pytest.raises(RuntimeAuthError) as exc_info:
            await adapter.generate(
                Request(
                    model="gemini-1.5-pro",
                    messages=[Message(role="user", content="hi")],
                )
            )

    assert exc_info.value.__cause__ is vendor_exc
