"""Tests for the AgentCore integration helpers."""

from __future__ import annotations

import httpx
import pytest

from eap_core.integrations.agentcore import (
    OIDCTokenExchange,
    _agentcore_identity_token_endpoint,
    configure_for_agentcore,
)


def test_default_token_endpoint_is_regional():
    url = _agentcore_identity_token_endpoint("us-east-1")
    assert url == "https://bedrock-agentcore.us-east-1.amazonaws.com/identity/token"

    url2 = _agentcore_identity_token_endpoint("eu-west-1")
    assert url2 == "https://bedrock-agentcore.eu-west-1.amazonaws.com/identity/token"


def test_from_agentcore_uses_regional_endpoint_by_default():
    ex = OIDCTokenExchange.from_agentcore(region="us-east-1")
    assert ex._endpoint == "https://bedrock-agentcore.us-east-1.amazonaws.com/identity/token"


def test_from_agentcore_accepts_endpoint_override():
    ex = OIDCTokenExchange.from_agentcore(token_endpoint="https://my-custom/idp/token")
    assert ex._endpoint == "https://my-custom/idp/token"


def test_from_agentcore_records_workload_identity_id_from_arg():
    ex = OIDCTokenExchange.from_agentcore(region="us-east-1", workload_identity_id="agent-42")
    assert ex._workload_identity_id == "agent-42"  # type: ignore[attr-defined]


def test_from_agentcore_reads_workload_identity_id_from_env(monkeypatch):
    monkeypatch.setenv("AGENTCORE_WORKLOAD_IDENTITY_ID", "env-agent")
    ex = OIDCTokenExchange.from_agentcore(region="us-east-1")
    assert ex._workload_identity_id == "env-agent"  # type: ignore[attr-defined]


class _MockTransport(httpx.AsyncBaseTransport):
    def __init__(self, handler):
        self._handler = handler

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return self._handler(request)


async def test_from_agentcore_token_exchange_works_against_mock():
    """Sanity check: the AgentCore-flavored client still does RFC 8693 exchange."""
    captured: dict = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["url"] = str(req.url)
        body = httpx.QueryParams(req.content.decode())
        captured["body"] = dict(body)
        return httpx.Response(
            200, json={"access_token": "agentcore-token", "expires_in": 60, "token_type": "Bearer"}
        )

    http = httpx.AsyncClient(transport=_MockTransport(handler))
    ex = OIDCTokenExchange.from_agentcore(region="us-east-1", http=http)
    token = await ex.exchange(subject_token="initial", audience="api.bank", scope="read")
    assert token == "agentcore-token"
    assert "us-east-1.amazonaws.com" in captured["url"]
    assert captured["body"]["grant_type"] == "urn:ietf:params:oauth:grant-type:token-exchange"


def test_configure_for_agentcore_returns_false_without_otel(monkeypatch):
    """When the [otel] extra is not installed, the helper is a graceful no-op."""
    # Force the import path to fail by stubbing one of the required modules.
    import sys

    saved = sys.modules.get("opentelemetry.exporter.otlp.proto.http.trace_exporter")
    sys.modules["opentelemetry.exporter.otlp.proto.http.trace_exporter"] = None  # type: ignore[assignment]
    try:
        # Re-import to pick up the stubbed missing module
        import importlib

        from eap_core.integrations import agentcore as ac_mod

        importlib.reload(ac_mod)
        result = ac_mod.configure_for_agentcore(service_name="test")
        assert result is False
    finally:
        if saved is None:
            del sys.modules["opentelemetry.exporter.otlp.proto.http.trace_exporter"]
        else:
            sys.modules["opentelemetry.exporter.otlp.proto.http.trace_exporter"] = saved
        # Restore the original module state for downstream tests.
        import importlib

        from eap_core.integrations import agentcore as ac_mod

        importlib.reload(ac_mod)


@pytest.mark.extras
def test_configure_for_agentcore_returns_true_with_otel_installed(monkeypatch):
    """When OTel SDK + OTLP exporter are installed, the helper configures
    a TracerProvider and returns True."""
    pytest.importorskip("opentelemetry.exporter.otlp.proto.http.trace_exporter")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://example.com/v1/traces")
    result = configure_for_agentcore(service_name="test-agent")
    assert result is True
