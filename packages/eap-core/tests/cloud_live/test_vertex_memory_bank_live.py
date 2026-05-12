"""Live GCP Vertex AI Memory Bank integration tests.

Skipped unless ``EAP_LIVE_GCP=1`` + valid GCP ADC creds + ``GCP_PROJECT_ID``
(or ``GOOGLE_CLOUD_PROJECT``) + ``VERTEX_MEMORY_BANK_ID`` env var pointing
at an existing Vertex Memory Bank. Each test tags its session id with
``unique_test_id`` to keep concurrent runs isolated; teardown is
best-effort via ``store.clear(session_id)``.

These are the v1.5.0 representative smoke tests — Memory round-trip,
missing-key behavior, ``list_keys`` aggregation, and ``forget``. Exhaustive
coverage of every code path lives in ``test_integrations_vertex_mocked.py``;
this file validates the integration end-to-end against real Vertex AI.
"""

from __future__ import annotations

import contextlib
import os

import pytest

# Module short-circuits if the [gcp] extra is absent — same shape as the
# AWS-side `importorskip("boto3")` guard in the AgentCore tests.
pytest.importorskip("google.cloud.aiplatform_v1beta1")

pytestmark = pytest.mark.cloud_live


@pytest.fixture
def vertex_memory_bank_id() -> str:
    """The Vertex Memory Bank id to target. Skips if unset."""
    val = os.environ.get("VERTEX_MEMORY_BANK_ID")
    if not val:
        pytest.skip(
            "Set VERTEX_MEMORY_BANK_ID to the id of an existing Vertex AI "
            "Memory Bank to run live Memory Bank tests."
        )
    return val


@pytest.fixture
def vertex_location() -> str:
    """The Vertex AI location to use for live tests. Defaults to ``us-central1``."""
    return os.environ.get("VERTEX_LOCATION", "us-central1")


async def test_vertex_memory_bank_roundtrip(
    live_gcp_enabled: None,
    gcp_project_id: str,
    vertex_memory_bank_id: str,
    vertex_location: str,
    unique_test_id: str,
) -> None:
    """Remember a value, recall it, assert equality. Teardown clears the session."""
    from eap_core.integrations.vertex import VertexMemoryBankStore

    store = VertexMemoryBankStore(
        project_id=gcp_project_id,
        location=vertex_location,
        memory_bank_id=vertex_memory_bank_id,
    )
    session_id = f"sess-{unique_test_id}-roundtrip"
    key = "favorite_color"
    value = "azul"

    try:
        await store.remember(session_id, key, value)
        recalled = await store.recall(session_id, key)
        assert recalled == value
    finally:
        with contextlib.suppress(Exception):
            await store.clear(session_id)


async def test_vertex_memory_bank_recall_missing_key_returns_none(
    live_gcp_enabled: None,
    gcp_project_id: str,
    vertex_memory_bank_id: str,
    vertex_location: str,
    unique_test_id: str,
) -> None:
    """Recall against an unknown key returns ``None`` (NotFound is swallowed)."""
    from eap_core.integrations.vertex import VertexMemoryBankStore

    store = VertexMemoryBankStore(
        project_id=gcp_project_id,
        location=vertex_location,
        memory_bank_id=vertex_memory_bank_id,
    )
    session_id = f"sess-{unique_test_id}-missing"
    try:
        result = await store.recall(session_id, "nonexistent-key")
        assert result is None
    finally:
        with contextlib.suppress(Exception):
            await store.clear(session_id)


async def test_vertex_memory_bank_list_keys_returns_remembered_keys(
    live_gcp_enabled: None,
    gcp_project_id: str,
    vertex_memory_bank_id: str,
    vertex_location: str,
    unique_test_id: str,
) -> None:
    """After remembering three keys, ``list_keys`` returns all three."""
    from eap_core.integrations.vertex import VertexMemoryBankStore

    store = VertexMemoryBankStore(
        project_id=gcp_project_id,
        location=vertex_location,
        memory_bank_id=vertex_memory_bank_id,
    )
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


async def test_vertex_memory_bank_forget_removes_single_key(
    live_gcp_enabled: None,
    gcp_project_id: str,
    vertex_memory_bank_id: str,
    vertex_location: str,
    unique_test_id: str,
) -> None:
    """``forget`` removes one key without disturbing the others."""
    from eap_core.integrations.vertex import VertexMemoryBankStore

    store = VertexMemoryBankStore(
        project_id=gcp_project_id,
        location=vertex_location,
        memory_bank_id=vertex_memory_bank_id,
    )
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
