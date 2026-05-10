"""A2A FastAPI server helpers — exposes `/.well-known/agent-card.json`."""
from __future__ import annotations

from typing import TYPE_CHECKING

from eap_core.a2a.card import AgentCard

if TYPE_CHECKING:
    from fastapi import FastAPI


def mount_card_route(app: "FastAPI", card: AgentCard) -> None:
    """Register GET /.well-known/agent-card.json on the given FastAPI app."""
    try:
        from fastapi import APIRouter
    except ImportError as e:
        raise ImportError(
            "mount_card_route requires the [a2a] extra: pip install eap-core[a2a]"
        ) from e

    router = APIRouter()

    @router.get("/.well-known/agent-card.json")
    async def _agent_card() -> dict:
        return card.model_dump()

    app.include_router(router)
