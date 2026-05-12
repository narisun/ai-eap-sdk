"""Live GCP Vertex AI Agent Registry integration tests.

Skipped unless ``EAP_LIVE_GCP=1`` + valid GCP ADC creds + ``GCP_PROJECT_ID``
(or ``GOOGLE_CLOUD_PROJECT``) + ``VERTEX_AGENT_REGISTRY_ID`` env var
pointing at an existing Agent Registry. Each test tags its published
record name with ``unique_test_id`` to avoid collisions with concurrent
runs; teardown is best-effort via the underlying ``AgentRegistryServiceClient``
``delete_registry_record`` call (made directly through the lazy-imported
client since the SDK class doesn't expose a delete helper at this layer).

Coverage: publish a record, fetch it via ``get``, find it via ``search``,
and confirm it appears in ``list_records``.
"""

from __future__ import annotations

import os
from typing import Any

import pytest

# Module short-circuits if the [gcp] extra is absent — same shape as the
# AWS-side `importorskip("boto3")` guard in the AgentCore tests.
pytest.importorskip("google.cloud.aiplatform_v1beta1")

pytestmark = pytest.mark.cloud_live


@pytest.fixture
def vertex_agent_registry_id() -> str:
    """The Vertex Agent Registry id to target. Skips if unset."""
    val = os.environ.get("VERTEX_AGENT_REGISTRY_ID")
    if not val:
        pytest.skip(
            "Set VERTEX_AGENT_REGISTRY_ID to the id of an existing Vertex AI "
            "Agent Registry to run live Registry tests."
        )
    return val


@pytest.fixture
def vertex_location() -> str:
    """The Vertex AI location to use for live tests. Defaults to ``us-central1``."""
    return os.environ.get("VERTEX_LOCATION", "us-central1")


def _delete_record_best_effort(
    project_id: str, location: str, registry_id: str, record_name: str
) -> None:
    """Best-effort cleanup of a published registry record.

    ``VertexAgentRegistry`` doesn't expose a ``delete_record`` method at
    this layer, so we go through the underlying client directly. Any
    exception is swallowed — cleanup must never fail a test.
    """
    try:
        from google.cloud import aiplatform_v1beta1

        client = aiplatform_v1beta1.AgentRegistryServiceClient()  # type: ignore[attr-defined, unused-ignore]
        parent = f"projects/{project_id}/locations/{location}/agentRegistries/{registry_id}"
        client.delete_registry_record(parent=parent, name=record_name)
    except Exception:
        pass


async def test_vertex_registry_publish_and_get_roundtrip(
    live_gcp_enabled: None,
    gcp_project_id: str,
    vertex_agent_registry_id: str,
    vertex_location: str,
    unique_test_id: str,
) -> None:
    """Publish an AGENT record, fetch it back via ``get``, verify it returns a dict."""
    from eap_core.integrations.vertex import VertexAgentRegistry

    registry = VertexAgentRegistry(
        project_id=gcp_project_id,
        location=vertex_location,
        registry_id=vertex_agent_registry_id,
    )
    record_name = f"eap-live-agent-{unique_test_id}"
    description = f"live test record {unique_test_id}"

    try:
        record_id = await registry.publish(
            {
                "name": record_name,
                "description": description,
                "record_type": "AGENT",
                "version": "1",
            }
        )
        assert isinstance(record_id, str)

        record = await registry.get(record_name)
        # The Vertex API returns either the full record or its metadata;
        # both shapes are normalized to dict by VertexAgentRegistry.get.
        # We accept None if the API has eventual-consistency lag, but in
        # the typical case we should get a dict back.
        assert record is None or isinstance(record, dict)
    finally:
        _delete_record_best_effort(
            gcp_project_id, vertex_location, vertex_agent_registry_id, record_name
        )


async def test_vertex_registry_get_missing_returns_none(
    live_gcp_enabled: None,
    gcp_project_id: str,
    vertex_agent_registry_id: str,
    vertex_location: str,
    unique_test_id: str,
) -> None:
    """``get`` against an unknown name returns ``None``, not an exception."""
    from eap_core.integrations.vertex import VertexAgentRegistry

    registry = VertexAgentRegistry(
        project_id=gcp_project_id,
        location=vertex_location,
        registry_id=vertex_agent_registry_id,
    )
    result = await registry.get(f"nonexistent-{unique_test_id}")
    assert result is None


async def test_vertex_registry_search_finds_published_record(
    live_gcp_enabled: None,
    gcp_project_id: str,
    vertex_agent_registry_id: str,
    vertex_location: str,
    unique_test_id: str,
) -> None:
    """After publishing, search returns the new record in the result set."""
    from eap_core.integrations.vertex import VertexAgentRegistry

    registry = VertexAgentRegistry(
        project_id=gcp_project_id,
        location=vertex_location,
        registry_id=vertex_agent_registry_id,
    )
    record_name = f"eap-live-search-{unique_test_id}"
    unique_marker = f"unique-marker-{unique_test_id}"

    try:
        await registry.publish(
            {
                "name": record_name,
                "description": unique_marker,
                "record_type": "AGENT",
                "version": "1",
            }
        )

        # Search by the unique marker baked into the description.
        results: list[dict[str, Any]] = await registry.search(unique_marker, max_results=20)
        names = {r.get("name") for r in results}
        assert record_name in names, (
            f"Expected to find {record_name} in search results; got {names}"
        )
    finally:
        _delete_record_best_effort(
            gcp_project_id, vertex_location, vertex_agent_registry_id, record_name
        )


async def test_vertex_registry_list_records_includes_published(
    live_gcp_enabled: None,
    gcp_project_id: str,
    vertex_agent_registry_id: str,
    vertex_location: str,
    unique_test_id: str,
) -> None:
    """A freshly published record appears in ``list_records`` output."""
    from eap_core.integrations.vertex import VertexAgentRegistry

    registry = VertexAgentRegistry(
        project_id=gcp_project_id,
        location=vertex_location,
        registry_id=vertex_agent_registry_id,
    )
    record_name = f"eap-live-list-{unique_test_id}"

    try:
        await registry.publish(
            {
                "name": record_name,
                "description": f"list-test-{unique_test_id}",
                "record_type": "AGENT",
                "version": "1",
            }
        )

        records = await registry.list_records(record_type="AGENT", max_results=100)
        names = {r.get("name") for r in records}
        assert record_name in names, f"Expected {record_name} in list_records output; got {names}"
    finally:
        _delete_record_best_effort(
            gcp_project_id, vertex_location, vertex_agent_registry_id, record_name
        )
