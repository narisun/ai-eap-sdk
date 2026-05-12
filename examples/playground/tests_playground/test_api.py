"""Playground API integration tests.

Exercises the FastAPI app via :class:`fastapi.testclient.TestClient` —
no real uvicorn process, no network. Verifies:

- ``/api/agents`` auto-discovers the known examples
- ``POST /api/agents/{name}/chat`` returns text + a trace list
- unknown agents return 404
- ``POST /api/agents/{name}/tools/{tool}`` invokes a tool end-to-end
- unknown tools return 404 (regression test for v1.6.0 review H1)
- cross-agent isolation: loading A → B → A doesn't break A's state
- the playground trace records pipeline markers
- DNS-rebind protection: requests with a foreign Host header are 400ed

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

``TestClient`` defaults the ``Host`` header to ``testserver`` — which
the playground's ``TrustedHostMiddleware`` (added in v1.6.1 to close
review finding M3) refuses. We therefore pass ``Host: 127.0.0.1`` on
every TestClient instance below rather than embedding the
test-framework sentinel into the production allow-list (smell — would
weaken the rebind defense in real deployments).
"""

from __future__ import annotations

import sys
from pathlib import Path

PLAYGROUND_DIR = Path(__file__).resolve().parent.parent
if str(PLAYGROUND_DIR) not in sys.path:
    sys.path.insert(0, str(PLAYGROUND_DIR))

from fastapi.testclient import TestClient
from server import app

# Use ``127.0.0.1`` as the default Host so TrustedHostMiddleware lets
# requests through. Tests that need to assert the rebind defense
# (``test_dns_rebind_blocked``) construct their own client and override
# Host explicitly.
client = TestClient(app, headers={"Host": "127.0.0.1"})


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


def test_unknown_tool_returns_404() -> None:
    """Regression test for review finding H1 (v1.6.0).

    Posting to ``/api/agents/{name}/tools/{tool}`` with a tool name
    that isn't in the agent's registry must return 404, not 500. The
    v1.6.0 handler caught ``MCPError("tool not found in registry")``
    via its broad ``except Exception`` clause and emitted a 500 —
    asymmetric with the agent-not-found path (which returned 404). The
    v1.6.1 fix pre-checks ``registry.get(tool) is None`` and raises
    ``HTTPException(404, ...)`` directly.
    """
    resp = client.post(
        "/api/agents/transactional-agent/tools/no_such_tool",
        json={"arguments": {}},
    )
    assert resp.status_code == 404, resp.text
    body = resp.json()
    # The error message should reference the missing tool name so
    # operators see something actionable in logs.
    detail = body.get("detail", "")
    assert "no_such_tool" in detail, detail


def test_cross_agent_isolation() -> None:
    """Loading A → B → A must succeed for all three steps.

    The playground's ``_purge_sibling_modules`` helper evicts cached
    top-level packages (``tools/``, ``configs/``) so each agent's
    sibling imports bind to its own subpackages, not whichever example
    loaded first. This test interleaves loads to make sure that
    machinery still works after v1.6.1 patches — the H1 / M1 fixes
    touch the same area of the file (the isinstance check and tool
    registry pre-check) and any subtle regression in the purge logic
    would surface as A failing on its second load.
    """
    # First load — transactional-agent
    a1 = client.post(
        "/api/agents/transactional-agent/tools/get_account",
        json={"arguments": {"account_id": "acct-A"}},
    )
    assert a1.status_code == 200, a1.text
    assert a1.json()["result"].get("id") == "acct-A"

    # Switch to research-agent
    b = client.post(
        "/api/agents/research-agent/chat",
        json={"message": "hello"},
    )
    assert b.status_code == 200, b.text
    assert isinstance(b.json().get("text"), str)

    # Back to transactional-agent — must still work after the
    # cross-agent sibling-purge.
    a2 = client.post(
        "/api/agents/transactional-agent/tools/get_account",
        json={"arguments": {"account_id": "acct-A2"}},
    )
    assert a2.status_code == 200, a2.text
    assert a2.json()["result"].get("id") == "acct-A2"


def test_trace_contains_pipeline_markers() -> None:
    """The chat endpoint should surface ``request_start`` and
    ``response`` pipeline markers in its trace.

    A genuine ``tool_call`` entry would require the local runtime to
    return a canned response that triggers tool dispatch — neither
    ``examples/transactional-agent/responses.yaml`` nor
    ``examples/research-agent/responses.yaml`` does so today (they
    return plain text only), and the local runtime contract for
    multi-step tool-call dispatch isn't defined. We therefore assert
    the pipeline-marker subset that's always present, which is enough
    to prove ``PlaygroundTraceMiddleware`` is wired into the loaded
    client. See review finding NIT-2 (test 3 fallback) — the spec
    explicitly authorizes this substitution when no canned-response
    tool call is available.
    """
    resp = client.post(
        "/api/agents/transactional-agent/chat",
        json={"message": "ping"},
    )
    assert resp.status_code == 200, resp.text
    trace = resp.json().get("trace", [])
    assert isinstance(trace, list)
    kinds = {e.get("kind") for e in trace}
    assert "request_start" in kinds, f"missing request_start marker; trace={trace}"
    assert "response" in kinds, f"missing response marker; trace={trace}"


def test_dns_rebind_blocked() -> None:
    """Regression test for review finding M3 (v1.6.0).

    A request with a foreign ``Host`` header (simulating DNS rebind
    from a malicious page on ``evil.example.com`` whose A record
    resolves to 127.0.0.1) must be refused by the
    ``TrustedHostMiddleware`` added in v1.6.1. Starlette returns 400
    for disallowed hosts. The companion sanity check at the bottom
    confirms a request with the allowed Host still works — without
    that, a misconfiguration (e.g. removing the allow-list entry by
    accident) would only surface in production.
    """
    # Use a separate TestClient that does NOT default Host to
    # ``127.0.0.1`` so we control the header per-request.
    raw = TestClient(app)
    bad = raw.get("/api/agents", headers={"Host": "evil.example.com"})
    assert bad.status_code == 400, bad.text
    # And the same client with the right Host still works — proves the
    # 400 was from the middleware, not some other failure.
    good = raw.get("/api/agents", headers={"Host": "127.0.0.1"})
    assert good.status_code == 200, good.text
