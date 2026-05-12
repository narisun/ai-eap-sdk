"""Regression tests for SyncProxy event-loop safety (P0-2)."""

from __future__ import annotations

import asyncio

import pytest

from eap_core.client import EnterpriseLLM
from eap_core.config import RuntimeConfig


def _local_client() -> EnterpriseLLM:
    return EnterpriseLLM(RuntimeConfig(provider="local", model="echo"))


def test_sync_proxy_works_in_script_context() -> None:
    """Outside any event loop, `client.sync.generate_text` runs normally."""
    client = _local_client()
    resp = client.sync.generate_text("hello")
    assert resp.text


async def test_sync_proxy_raises_actionable_error_inside_event_loop() -> None:
    """Inside an active loop, `client.sync.generate_text` raises a clear RuntimeError."""
    client = _local_client()
    with pytest.raises(RuntimeError, match="cannot be used inside an active event loop"):
        client.sync.generate_text("hello")


def test_sync_proxy_error_does_not_include_bare_asyncio_run_traceback() -> None:
    """The actionable error must NOT be the bare `asyncio.run() cannot be called…` form."""
    client = _local_client()

    async def _inner() -> None:
        try:
            client.sync.generate_text("hello")
        except RuntimeError as exc:
            # The bare asyncio.run() error mentions "asyncio.run() cannot be called"
            assert "asyncio.run()" not in str(exc), (
                "SyncProxy must wrap the bare RuntimeError with a directive message; "
                "got the unwrapped asyncio.run() error which is user-hostile"
            )

    asyncio.run(_inner())
