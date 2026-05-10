"""Tests for Vertex Phase A: Observability + Identity helpers."""

from __future__ import annotations

import pytest

from eap_core.integrations.vertex import (
    VertexAgentIdentityToken,
    configure_for_vertex_observability,
)


@pytest.fixture(autouse=True)
def _clear_real_flag(monkeypatch):
    monkeypatch.delenv("EAP_ENABLE_REAL_RUNTIMES", raising=False)


# ---- VertexAgentIdentityToken --------------------------------------------


def test_construction_does_not_hit_google_auth():
    """Building the helper must not import google.auth or fetch a token."""
    import sys

    sys.modules.pop("google.auth", None)
    _ = VertexAgentIdentityToken(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    assert "google.auth" not in sys.modules


def test_default_scopes_are_cloud_platform():
    t = VertexAgentIdentityToken()
    assert t._scopes == ["https://www.googleapis.com/auth/cloud-platform"]


def test_custom_scopes_are_kept():
    custom = ["https://www.googleapis.com/auth/bigquery"]
    t = VertexAgentIdentityToken(scopes=custom)
    assert t._scopes == custom


def test_get_token_gated_by_env_flag():
    t = VertexAgentIdentityToken()
    with pytest.raises(NotImplementedError, match="EAP_ENABLE_REAL_RUNTIMES"):
        t.get_token()


def test_get_token_accepts_audience_and_scope_for_compat():
    """The signature mirrors NonHumanIdentity.get_token even though Vertex
    doesn't use those args; compat lets users swap implementations."""
    t = VertexAgentIdentityToken()
    with pytest.raises(NotImplementedError):
        t.get_token(audience="ignored-by-vertex", scope="ignored-too")


def test_name_field_present():
    """All cross-vendor identity / sandbox / etc. impls expose a `name` attr."""
    assert VertexAgentIdentityToken.name == "vertex"


# ---- configure_for_vertex_observability -----------------------------------


def test_returns_false_without_otel_extra(monkeypatch):
    """When opentelemetry isn't importable, the helper is a graceful no-op."""
    import sys

    saved = sys.modules.get("opentelemetry.exporter.otlp.proto.http.trace_exporter")
    sys.modules["opentelemetry.exporter.otlp.proto.http.trace_exporter"] = None  # type: ignore[assignment]
    try:
        import importlib

        from eap_core.integrations import vertex as vmod

        importlib.reload(vmod)
        assert vmod.configure_for_vertex_observability(service_name="t") is False
    finally:
        if saved is None:
            del sys.modules["opentelemetry.exporter.otlp.proto.http.trace_exporter"]
        else:
            sys.modules["opentelemetry.exporter.otlp.proto.http.trace_exporter"] = saved
        import importlib

        from eap_core.integrations import vertex as vmod

        importlib.reload(vmod)


@pytest.mark.extras
def test_returns_true_with_otel_installed(monkeypatch):
    pytest.importorskip("opentelemetry.exporter.otlp.proto.http.trace_exporter")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://example.com/v1/traces")
    assert configure_for_vertex_observability(service_name="my-agent") is True


@pytest.mark.extras
def test_accepts_project_id_argument(monkeypatch):
    """When the project id is provided, the function still returns True
    (whether or not the global tracer provider can be re-set within the
    test process — OTel SDK >=1.40 warns and ignores re-sets, which is
    its own behavior)."""
    pytest.importorskip("opentelemetry.exporter.otlp.proto.http.trace_exporter")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://example.com/v1/traces")
    assert (
        configure_for_vertex_observability(service_name="my-agent", project_id="my-gcp-project")
        is True
    )
