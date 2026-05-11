"""Shared mocked-client fixtures for agentcore + vertex integration tests.

These tests run with ``EAP_ENABLE_REAL_RUNTIMES=1`` to bypass the env-flag
gate, but inject fake ``boto3`` / ``google.cloud.aiplatform_v1beta1`` modules
into ``sys.modules`` so no real cloud client library or credentials are
needed. Each fixture returns a ``MagicMock`` configured to return stubbed
responses for the calls we exercise.

``boto3`` is not installed in the non-extras venv; injecting a fake module
makes the ``_client()`` ``lazy-import`` succeed and lets the integration
make the same call pattern it would in production. Same shape for the
Google clients.
"""

from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def mock_boto3_client(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Patch ``boto3.client`` so any integration calling ``boto3.client(...)``
    receives a single shared ``MagicMock``.

    Returns the MagicMock the integration will receive — configure
    per-test by setting return values on its method attributes (e.g.
    ``mock_boto3_client.create_registry_record.return_value = {...}``).

    Also stores the call args on the factory so tests can assert the
    ``service_name`` / ``region_name`` that the integration passed to
    ``boto3.client(...)``. Access via
    ``mock_boto3_client._factory.call_args``.
    """
    client_mock = MagicMock(name="boto3_client_mock")
    factory = MagicMock(name="boto3_client_factory", return_value=client_mock)

    fake_boto3 = types.ModuleType("boto3")
    fake_boto3.client = factory  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "boto3", fake_boto3)

    # Expose the factory so tests can assert how boto3.client was called.
    client_mock._factory = factory
    return client_mock


@pytest.fixture
def mock_google_aiplatform(monkeypatch: pytest.MonkeyPatch) -> dict[str, MagicMock]:
    """Patch ``google.cloud.aiplatform_v1beta1`` so any integration calling
    one of its service-client constructors receives a ``MagicMock``.

    The integrations under test instantiate one of four clients:

    - ``MemoryBankServiceClient`` — Phase B memory
    - ``SandboxServiceClient`` — Phase B sandbox / browser
    - ``AgentRegistryServiceClient`` — Phase D registry
    - ``PaymentServiceClient`` — Phase D payments (AP2)
    - ``EvaluationServiceClient`` — Phase D eval scorer

    Returns a dict mapping the class names to the ``MagicMock`` instance
    that constructor returns, so tests can pre-configure method returns
    and later assert calls.
    """
    clients: dict[str, MagicMock] = {}
    constructors: dict[str, MagicMock] = {}

    for class_name in (
        "MemoryBankServiceClient",
        "SandboxServiceClient",
        "AgentRegistryServiceClient",
        "PaymentServiceClient",
        "EvaluationServiceClient",
    ):
        client = MagicMock(name=f"{class_name}_instance")
        ctor = MagicMock(name=f"{class_name}_ctor", return_value=client)
        clients[class_name] = client
        constructors[class_name] = ctor

    fake_module = types.ModuleType("google.cloud.aiplatform_v1beta1")
    for cls_name, ctor in constructors.items():
        setattr(fake_module, cls_name, ctor)

    # google.cloud is a namespace package; make sure both layers exist.
    fake_google = types.ModuleType("google")
    fake_google_cloud = types.ModuleType("google.cloud")
    fake_google.cloud = fake_google_cloud  # type: ignore[attr-defined]
    fake_google_cloud.aiplatform_v1beta1 = fake_module  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "google", fake_google)
    monkeypatch.setitem(sys.modules, "google.cloud", fake_google_cloud)
    monkeypatch.setitem(sys.modules, "google.cloud.aiplatform_v1beta1", fake_module)

    # Stash the constructors so tests can assert no-args / etc.
    for cls_name, ctor in constructors.items():
        clients[cls_name]._ctor = ctor

    return clients


@pytest.fixture
def real_runtimes_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bypass the ``EAP_ENABLE_REAL_RUNTIMES`` env-flag gate."""
    monkeypatch.setenv("EAP_ENABLE_REAL_RUNTIMES", "1")


__all__ = ["mock_boto3_client", "mock_google_aiplatform", "real_runtimes_enabled"]
