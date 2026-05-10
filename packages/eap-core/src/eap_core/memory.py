"""Memory abstraction for agentic AI applications.

EAP-Core does not prescribe a memory implementation; bring your own.
This module defines the ``MemoryStore`` Protocol that any backing
store satisfies, plus an ``InMemoryStore`` default for tests, local
development, and unit testing of agent code.

Backends ship as separate modules / extras:

- ``InMemoryStore`` (here) — process-local dict, useful for dev/tests.
- ``eap_core.integrations.agentcore.AgentCoreMemoryStore`` —
  AWS Bedrock AgentCore Memory; lazy-imports boto3.

A ``MemoryStore`` instance can be attached to the per-request
``Context.memory_store``. Middleware and tool implementations read /
write to it via the same Protocol regardless of backend.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol, runtime_checkable


@runtime_checkable
class MemoryStore(Protocol):
    """Protocol every memory backend implements.

    Operations are async to accommodate I/O-bound backends. The Protocol
    is intentionally small — most agentic memory operations reduce to
    ``remember`` (write a key/value scoped to a session) and ``recall``
    (read it back). Backends that support richer semantics (semantic
    search, summarization, vector recall) expose those via methods
    beyond this Protocol; tools using those features depend on the
    concrete class rather than the Protocol.
    """

    async def remember(self, session_id: str, key: str, value: str) -> None:
        """Store ``value`` under ``key`` in the session's namespace."""
        ...

    async def recall(self, session_id: str, key: str) -> str | None:
        """Return the value previously stored, or ``None`` if absent."""
        ...

    async def list_keys(self, session_id: str) -> Iterable[str]:
        """Return all keys currently stored for the session."""
        ...

    async def forget(self, session_id: str, key: str) -> None:
        """Remove a single key. No-op if absent."""
        ...

    async def clear(self, session_id: str) -> None:
        """Remove every key for the session."""
        ...


class InMemoryStore:
    """Process-local memory backed by a dict.

    Use for tests, local development, and unit testing of agent code.
    Does not persist across processes. Each instance is independent.
    Per-session isolation is enforced via the ``session_id`` argument.
    """

    name: str = "in_memory"

    def __init__(self) -> None:
        self._store: dict[str, dict[str, str]] = {}

    async def remember(self, session_id: str, key: str, value: str) -> None:
        self._store.setdefault(session_id, {})[key] = value

    async def recall(self, session_id: str, key: str) -> str | None:
        return self._store.get(session_id, {}).get(key)

    async def list_keys(self, session_id: str) -> list[str]:
        return list(self._store.get(session_id, {}).keys())

    async def forget(self, session_id: str, key: str) -> None:
        self._store.get(session_id, {}).pop(key, None)

    async def clear(self, session_id: str) -> None:
        self._store.pop(session_id, None)


__all__ = ["InMemoryStore", "MemoryStore"]
