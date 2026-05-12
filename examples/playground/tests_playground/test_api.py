"""Playground API integration tests.

Exercises the FastAPI app via :class:`fastapi.testclient.TestClient` —
no real uvicorn process, no network. Verifies:

- ``/api/agents`` auto-discovers the known examples
- ``POST /api/agents/{name}/chat`` returns text + a trace list
- unknown agents return 404
- ``POST /api/agents/{name}/tools/{tool}`` invokes a tool end-to-end

The ``transactional-agent`` is used as the load-bearing example
because its ``get_account`` tool has no external dependencies — it
returns a sensible default for any account id, so the test stays
hermetic.

The directory is named ``tests_playground`` (not ``tests``) so the
top-level non-extras pytest run skips it: the playground requires
FastAPI + uvicorn + the example agents' transitive deps to be on
``sys.path``, and forcing the SDK's bare test gauntlet to install
those just to collect the file would mix concerns. The playground
suite is run explicitly via:

    uv run --with eap-core --with fastapi --with uvicorn --with httpx \\
        pytest examples/playground/tests_playground -q
"""

from __future__ import annotations

import sys
from pathlib import Path

PLAYGROUND_DIR = Path(__file__).resolve().parent.parent
if str(PLAYGROUND_DIR) not in sys.path:
    sys.path.insert(0, str(PLAYGROUND_DIR))

from fastapi.testclient import TestClient
from server import app

client = TestClient(app)


def test_list_agents_discovers_at_least_one_example() -> None:
    """Discovery should find at least the three example agents we ship
    with ``build_client()``. Asserting a known subset (rather than an
    exact match) lets future examples be added without churning this
    test.
    """
    resp = client.get("/api/agents")
    assert resp.status_code == 200, resp.text
    agents = resp.json()
    assert isinstance(agents, list)
    names = {a["name"] for a in agents}
    expected_subset = {"transactional-agent", "research-agent", "cross-domain-agent"}
    assert expected_subset.issubset(names), (
        f"missing expected agents: {expected_subset - names}; got {names}"
    )


def test_chat_returns_text_and_trace_shape() -> None:
    """The chat endpoint should round-trip via the local runtime —
    no LLM credentials required — and surface a tool-call trace list.
    """
    resp = client.post(
        "/api/agents/transactional-agent/chat",
        json={"message": "hello"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert "text" in data
    assert isinstance(data["text"], str)
    assert "trace" in data
    assert isinstance(data["trace"], list)


def test_unknown_agent_returns_404() -> None:
    """Unknown agent names produce a 404 rather than a 500."""
    resp = client.post(
        "/api/agents/nonexistent-agent/chat",
        json={"message": "hi"},
    )
    assert resp.status_code == 404


def test_invoke_tool_returns_result() -> None:
    """The tool-invocation endpoint should bypass the LLM and call the
    tool directly. ``get_account`` is hermetic — it returns a stub
    record for any account id — so this asserts the full SDK code path
    (middleware pipeline + identity dispatcher + tool registry) fires
    without any external dependencies.
    """
    resp = client.post(
        "/api/agents/transactional-agent/tools/get_account",
        json={"arguments": {"account_id": "acct-1"}},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert "result" in data
    # ``get_account`` returns a dict with ``id`` matching the input.
    assert isinstance(data["result"], dict)
    assert data["result"].get("id") == "acct-1"
