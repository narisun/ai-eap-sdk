"""Live AWS Bedrock AgentCore Registry integration tests.

Skipped unless ``EAP_LIVE_AWS=1`` + valid AWS creds + ``AGENTCORE_REGISTRY_NAME``
env var pointing at an existing registry. Each test tags its published
records with ``unique_test_id`` to avoid collisions with concurrent runs;
teardown is best-effort via the AWS control-plane ``delete_registry_record``
call (made directly through the underlying boto3 client since the SDK
class doesn't expose a delete helper at this layer).

Coverage: publish an MCP-server record, fetch it via ``get_record``, find
it via ``search``, and confirm it appears in ``list_records``.
"""

from __future__ import annotations

import os
from typing import Any

import pytest

pytest.importorskip("boto3")

pytestmark = pytest.mark.cloud_live


@pytest.fixture
def agentcore_registry_name() -> str:
    """The AgentCore registry name to target. Skips if unset."""
    val = os.environ.get("AGENTCORE_REGISTRY_NAME")
    if not val:
        pytest.skip(
            "Set AGENTCORE_REGISTRY_NAME to the name of an existing AgentCore "
            "registry to run live Registry tests."
        )
    return val


def _delete_record_best_effort(registry_name: str, region: str, record_name: str) -> None:
    """Best-effort cleanup of a published registry record.

    The SDK's ``RegistryClient`` doesn't expose a ``delete_record`` method
    at this layer, so we go through boto3 directly. Any exception is
    swallowed — cleanup must never fail a test.

    **API-name assumption.** ``delete_registry_record`` is the upstream
    operation name we *believe* AWS Bedrock AgentCore Control exposes for
    this purpose; we haven't verified it against a live account during
    plan drafting. If AWS named it differently (``delete_record`` /
    ``deregister_record`` / ``remove_registry_entry``), the call below
    raises ``AttributeError`` and the broad ``except Exception: pass``
    swallows it — orphaned test records would then accumulate in
    long-running shared registries. When v1.5's first user runs these
    tests against real AWS and confirms (or refutes) the operation
    name, swap the literal here for the verified one (and consider
    promoting it to an ``SDK RegistryClient.delete_record`` helper so
    the contract gets pinned by static typing rather than runtime
    swallowing).
    """
    try:
        import boto3

        client = boto3.client("bedrock-agentcore-control", region_name=region)
        client.delete_registry_record(registryName=registry_name, name=record_name)
    except Exception:
        pass


async def test_publish_and_get_record_roundtrip(
    live_aws_enabled: None,
    aws_region: str,
    agentcore_registry_name: str,
    unique_test_id: str,
) -> None:
    """Publish an MCP-server record, fetch it back, verify metadata."""
    from eap_core.integrations.agentcore import RegistryClient

    client = RegistryClient(registry_name=agentcore_registry_name, region=aws_region)
    record_name = f"eap-live-mcp-{unique_test_id}"
    description = f"live test record {unique_test_id}"
    endpoint = "https://example.invalid/mcp"

    try:
        record_id = await client.publish_mcp_server(
            record_name, description=description, mcp_endpoint=endpoint
        )
        assert isinstance(record_id, str)

        record = await client.get_record(record_name)
        assert record is not None
        # The API returns either the full record or its metadata; both
        # shapes are normalized to dict by RegistryClient.get_record.
        # Verify content (not just dictness) — an empty {} would pass
        # the isinstance check but fail the round-trip property.
        assert isinstance(record, dict)
        assert record  # not empty
    finally:
        _delete_record_best_effort(agentcore_registry_name, aws_region, record_name)


async def test_get_record_missing_returns_none(
    live_aws_enabled: None,
    aws_region: str,
    agentcore_registry_name: str,
    unique_test_id: str,
) -> None:
    """``get_record`` against an unknown name returns ``None``, not an exception."""
    from eap_core.integrations.agentcore import RegistryClient

    client = RegistryClient(registry_name=agentcore_registry_name, region=aws_region)
    result = await client.get_record(f"nonexistent-{unique_test_id}")
    assert result is None


async def test_search_finds_published_record(
    live_aws_enabled: None,
    aws_region: str,
    agentcore_registry_name: str,
    unique_test_id: str,
) -> None:
    """After publishing, hybrid search returns the new record in the result set."""
    from eap_core.integrations.agentcore import RegistryClient

    client = RegistryClient(registry_name=agentcore_registry_name, region=aws_region)
    record_name = f"eap-live-search-{unique_test_id}"
    description = f"unique-marker-{unique_test_id}"

    try:
        await client.publish_mcp_server(
            record_name,
            description=description,
            mcp_endpoint="https://example.invalid/mcp",
        )

        # Search by the unique marker in the description.
        results: list[dict[str, Any]] = await client.search(description, max_results=20)
        names = {r.get("name") for r in results}
        assert record_name in names, (
            f"Expected to find {record_name} in search results; got {names}"
        )
    finally:
        _delete_record_best_effort(agentcore_registry_name, aws_region, record_name)


async def test_list_records_includes_published(
    live_aws_enabled: None,
    aws_region: str,
    agentcore_registry_name: str,
    unique_test_id: str,
) -> None:
    """A freshly published record appears in ``list_records`` output."""
    from eap_core.integrations.agentcore import RegistryClient

    client = RegistryClient(registry_name=agentcore_registry_name, region=aws_region)
    record_name = f"eap-live-list-{unique_test_id}"

    try:
        await client.publish_mcp_server(
            record_name,
            description=f"list-test-{unique_test_id}",
            mcp_endpoint="https://example.invalid/mcp",
        )

        records = await client.list_records(record_type="MCP_SERVER", max_results=100)
        names = {r.get("name") for r in records}
        assert record_name in names, f"Expected {record_name} in list_records output; got {names}"
    finally:
        _delete_record_best_effort(agentcore_registry_name, aws_region, record_name)
