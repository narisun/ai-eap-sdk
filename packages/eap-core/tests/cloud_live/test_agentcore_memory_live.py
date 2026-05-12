"""Live AWS Bedrock AgentCore Memory integration tests.

Skipped unless ``EAP_LIVE_AWS=1`` + valid AWS creds + ``AGENTCORE_MEMORY_ID``
env var pointing at an existing AgentCore Memory bank. Each test tags its
session with ``unique_test_id`` so concurrent runs don't collide; teardown
is best-effort via ``store.clear(session_id)``.

These are the v1.5.0 representative smoke tests — Memory round-trip,
missing-key behavior, and ``list_keys`` aggregation. Exhaustive coverage
of every code path lives in ``test_integrations_agentcore_mocked.py``;
this file validates the integration end-to-end against real AWS.
"""

from __future__ import annotations

import contextlib
import os

import pytest

pytest.importorskip("boto3")

pytestmark = pytest.mark.cloud_live


@pytest.fixture
def agentcore_memory_id() -> str:
    """The AgentCore Memory bank id to target. Skips if unset."""
    val = os.environ.get("AGENTCORE_MEMORY_ID")
    if not val:
        pytest.skip(
            "Set AGENTCORE_MEMORY_ID to the id of an existing AgentCore "
            "Memory bank to run live Memory tests."
        )
    return val


async def test_remember_recall_roundtrip(
    live_aws_enabled: None,
    aws_region: str,
    agentcore_memory_id: str,
    unique_test_id: str,
) -> None:
    """Remember a value, recall it, assert equality. Teardown clears the session."""
    from eap_core.integrations.agentcore import AgentCoreMemoryStore

    store = AgentCoreMemoryStore(memory_id=agentcore_memory_id, region=aws_region)
    session_id = f"sess-{unique_test_id}-roundtrip"
    key = "favorite_color"
    value = "vermillion"

    try:
        await store.remember(session_id, key, value)
        recalled = await store.recall(session_id, key)
        assert recalled == value
    finally:
        with contextlib.suppress(Exception):
            await store.clear(session_id)


async def test_recall_missing_key_returns_none(
    live_aws_enabled: None,
    aws_region: str,
    agentcore_memory_id: str,
    unique_test_id: str,
) -> None:
    """Recall against an unknown key returns ``None`` (not an exception)."""
    from eap_core.integrations.agentcore import AgentCoreMemoryStore

    store = AgentCoreMemoryStore(memory_id=agentcore_memory_id, region=aws_region)
    session_id = f"sess-{unique_test_id}-missing"
    try:
        result = await store.recall(session_id, "nonexistent-key")
        assert result is None
    finally:
        with contextlib.suppress(Exception):
            await store.clear(session_id)


async def test_list_keys_returns_remembered_keys(
    live_aws_enabled: None,
    aws_region: str,
    agentcore_memory_id: str,
    unique_test_id: str,
) -> None:
    """After remembering three keys, ``list_keys`` returns all three."""
    from eap_core.integrations.agentcore import AgentCoreMemoryStore

    store = AgentCoreMemoryStore(memory_id=agentcore_memory_id, region=aws_region)
    session_id = f"sess-{unique_test_id}-listkeys"
    try:
        await store.remember(session_id, "k1", "v1")
        await store.remember(session_id, "k2", "v2")
        await store.remember(session_id, "k3", "v3")
        keys = sorted(await store.list_keys(session_id))
        assert keys == ["k1", "k2", "k3"]
    finally:
        with contextlib.suppress(Exception):
            await store.clear(session_id)


async def test_forget_removes_single_key(
    live_aws_enabled: None,
    aws_region: str,
    agentcore_memory_id: str,
    unique_test_id: str,
) -> None:
    """``forget`` removes one key without disturbing the others."""
    from eap_core.integrations.agentcore import AgentCoreMemoryStore

    store = AgentCoreMemoryStore(memory_id=agentcore_memory_id, region=aws_region)
    session_id = f"sess-{unique_test_id}-forget"
    try:
        await store.remember(session_id, "keep", "kept")
        await store.remember(session_id, "drop", "dropped")
        await store.forget(session_id, "drop")
        assert await store.recall(session_id, "drop") is None
        assert await store.recall(session_id, "keep") == "kept"
    finally:
        with contextlib.suppress(Exception):
            await store.clear(session_id)
