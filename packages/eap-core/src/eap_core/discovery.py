"""Agent / tool / MCP-server discovery and governance.

EAP-Core abstracts the discovery layer behind the ``AgentRegistry``
Protocol so the same code can publish-and-discover via:

- ``InMemoryAgentRegistry`` (here) — dict-backed, for tests and local dev.
- ``eap_core.integrations.agentcore.RegistryClient`` — AWS Agent Registry.
- ``eap_core.integrations.vertex.VertexAgentRegistry`` — GCP Agent Registry.

The Protocol is intentionally minimal: publish, get, search, list.
Cloud-managed implementations layer on governance (approval
workflows, IAM) and richer search (semantic + keyword hybrid).
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class AgentRegistry(Protocol):
    """Org-wide catalog of agents, tools, MCP servers, and skills."""

    async def publish(self, record: dict[str, Any]) -> str:
        """Publish a record. Returns the record id assigned by the backend.

        ``record`` must have at minimum ``name`` and ``record_type``
        (``AGENT``, ``MCP_SERVER``, ``TOOL``, ``SKILL``, or custom).
        Backends may require additional fields.
        """
        ...

    async def get(self, name: str) -> dict[str, Any] | None:
        """Fetch a record by name; returns ``None`` if absent."""
        ...

    async def search(self, query: str, *, max_results: int = 10) -> list[dict[str, Any]]:
        """Find records relevant to ``query`` (semantic + keyword hybrid)."""
        ...

    async def list_records(
        self,
        *,
        record_type: str | None = None,
        max_results: int = 100,
    ) -> list[dict[str, Any]]:
        """List records, optionally filtered by type."""
        ...


class InMemoryAgentRegistry:
    """Process-local registry for tests and local development.

    Stores records in a dict keyed by name. Each call assigns an
    auto-incrementing record id. Search is a simple substring match
    against name + description.
    """

    name: str = "in_memory_registry"

    def __init__(self) -> None:
        self._records: dict[str, dict[str, Any]] = {}
        self._next_id: int = 0

    async def publish(self, record: dict[str, Any]) -> str:
        if "name" not in record:
            raise ValueError("record must have a 'name' field")
        name = record["name"]
        self._next_id += 1
        record_id = f"rec-{self._next_id}"
        stored = dict(record)
        stored["record_id"] = record_id
        self._records[name] = stored
        return record_id

    async def get(self, name: str) -> dict[str, Any] | None:
        rec = self._records.get(name)
        return dict(rec) if rec is not None else None

    async def search(self, query: str, *, max_results: int = 10) -> list[dict[str, Any]]:
        q = query.lower()
        hits = [
            dict(r)
            for r in self._records.values()
            if q in r.get("name", "").lower() or q in r.get("description", "").lower()
        ]
        return hits[:max_results]

    async def list_records(
        self,
        *,
        record_type: str | None = None,
        max_results: int = 100,
    ) -> list[dict[str, Any]]:
        records = [
            dict(r)
            for r in self._records.values()
            if record_type is None or r.get("record_type") == record_type
        ]
        return records[:max_results]


__all__ = [
    "AgentRegistry",
    "InMemoryAgentRegistry",
]
