import pytest

pytest.importorskip("fastapi")
pytestmark = pytest.mark.extras

import httpx
from fastapi import FastAPI

from eap_core.a2a.card import AgentCard, Skill
from eap_core.a2a.server import mount_card_route


async def test_well_known_endpoint_returns_card():
    card = AgentCard(
        name="test-agent",
        description="t",
        skills=[Skill(name="echo", description="echo", input_schema={}, output_schema=None)],
    )
    app = FastAPI()
    mount_card_route(app, card)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as ac:
        resp = await ac.get("/.well-known/agent-card.json")
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "test-agent"
    assert body["skills"][0]["name"] == "echo"
