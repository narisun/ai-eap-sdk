# Integrating EAP-Core with AWS Bedrock AgentCore

This document explains how EAP-Core integrates with each
[AWS Bedrock AgentCore](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/what-is-bedrock-agentcore.html)
service, what's shipped today (Phase A), and what's planned (Phases B–D).

## TL;DR — positioning

EAP-Core sits **inside** AgentCore-deployed agents. AgentCore provides
the managed platform (deployment runtime, multi-tenant Gateway,
managed Memory, Browser/Code Interpreter sandboxes, OTel-backed
Observability, Cedar-based Policy, etc.). EAP-Core provides the
**in-process, vendor-neutral cross-cutting layer** that:

- Enforces sanitization, PII masking, policy, schema validation **in
  the agent's own process** before any data crosses the trust
  boundary to AgentCore-managed services. (Defense in depth — same
  Cedar policy can run both at the Gateway tier and inside the agent.)
- Stays portable: the same `agent.py` runs on AgentCore Runtime, on
  bare Lambda, on Cloud Run, or on a VM. The deploy command picks the
  packaging.
- Picks the same open standards AgentCore picks (MCP, A2A, OTel
  GenAI, Cedar, OAuth 2.1) so most of the integration is *swap an
  endpoint*, not write new code.

## Service-by-service mapping

| AgentCore service | EAP-Core position | Status |
|---|---|---|
| **Runtime** (microVM serverless host) | Deploy target via `eap deploy --runtime agentcore` | **Phase A — shipped** |
| **Observability** (OTel → CloudWatch) | `ObservabilityMiddleware` already emits OTel GenAI spans; `configure_for_agentcore()` helper wires the OTLP exporter | **Phase A — shipped** |
| **Identity** (workload IDs, OAuth in/out) | `OIDCTokenExchange.from_agentcore()` factory points at AgentCore Identity | **Phase A — shipped** |
| **Policy** (Cedar at the Gateway tier) | Our `PolicyMiddleware` runs the same Cedar policy in-process. Defense in depth. | **Aligned** (no new code) |
| **Memory** (short/long-term, cross-session) | `MemoryStore` Protocol + `InMemoryStore` (default) + `AgentCoreMemoryStore` | **Phase B — shipped** |
| **Gateway** (APIs/Lambdas → MCP) | `GatewayClient` for outbound; `eap publish-to-gateway` (OpenAPI export) for inbound | **Phase C — shipped** |
| **Code Interpreter** (Python/JS sandboxes) | `register_code_interpreter_tools()` — three MCP tools | **Phase B — shipped** |
| **Browser** (cloud browser) | `register_browser_tools()` — five MCP tools | **Phase B — shipped** |
| **Inbound JWT verification** | `InboundJwtVerifier` + `jwt_dependency()` (FastAPI) | **Phase B — shipped** |
| **Payments** (x402 microtransactions) | `PaymentRequired` exception + `PaymentClient` | **Phase D — shipped** |
| **Evaluations** (trace-based eval) | `to_agentcore_eval_dataset` (export) + `AgentCoreEvalScorer` (import) | **Phase D — shipped** |
| **Registry** (org-wide tool/agent catalog) | `RegistryClient` — publish/discover AgentCards | **Phase D — shipped** |

## Phase A — what's shipped

### 1. `eap deploy --runtime agentcore`

Packages your project as an AgentCore Runtime artifact: an ARM64
Docker container that exposes the
[AgentCore HTTP protocol contract](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-http-protocol-contract.html)
(`POST /invocations`, `GET /ping`, `WebSocket /ws`) on port 8080.

```bash
eap deploy --runtime agentcore [--entry agent.py:answer]
```

**Output** at `dist/agentcore/`:

```
dist/agentcore/
├── Dockerfile          # ARM64 base, installs FastAPI + uvicorn + your project
├── handler.py          # Imports your entry point; serves /invocations + /ping
├── README.md           # How to build and push the image
└── <your project files>
```

**`handler.py` semantics:**

- `POST /invocations` accepts `{"prompt": "string"}`. The handler
  calls your entry function (`agent.py:answer` by default) with the
  prompt string and returns `{"response": "<result>", "status":
  "success"}`. Async functions are awaited.
- `GET /ping` returns `{"status": "Healthy", "time_of_last_update":
  <unix-ts>}` per the AgentCore healthcheck contract.
- The middleware chain in your `agent.py` runs unchanged on every
  call. Sanitize / PII / OTel / Policy / Validate fire before any
  AgentCore-managed service ever sees your data.

**Build and push (manual; live deploy gated):**

```bash
cd dist/agentcore
docker buildx build --platform linux/arm64 -t my-agent:latest .
# tag and push to your ECR repo, then register with AgentCore Runtime
```

Live deploy via the CLI (`docker buildx`, `aws ecr push`,
`aws bedrock-agentcore create-runtime`) is gated behind
`EAP_ENABLE_REAL_DEPLOY=1` and not yet automated. Phase A produces
the artifact; you push it. Future phases will automate.

### 2. Observability — `configure_for_agentcore()`

AgentCore Observability ingests
[OTel-compatible](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/observability.html)
telemetry into CloudWatch. Our `ObservabilityMiddleware` already
emits OTel GenAI semantic-convention spans (per the design spec
§2). The integration is a one-line helper:

```python
from eap_core.integrations.agentcore import configure_for_agentcore

configure_for_agentcore()  # reads OTEL_EXPORTER_OTLP_ENDPOINT etc.
```

What it does:

- If the `[otel]` extra is installed, sets up a `TracerProvider` with
  an OTLP exporter using the standard env vars
  (`OTEL_EXPORTER_OTLP_ENDPOINT`, `OTEL_EXPORTER_OTLP_HEADERS`,
  `OTEL_RESOURCE_ATTRIBUTES`). AgentCore Runtime injects these for
  you when your agent runs there.
- If `[otel]` isn't installed, it's a no-op — the middleware still
  writes the same `gen_ai.*` attributes to `ctx.metadata` so the
  trajectory recorder and audit log work without OTel.
- Sets `service.name` to your agent's name if you pass it explicitly,
  or reads `AGENT_NAME` env var.

**Outside AgentCore** (local dev, other clouds), set the env vars to
point at any OTLP-compatible backend (Honeycomb, Tempo, Datadog,
OpenObserve, etc.). The agent code doesn't change.

### 3. Identity — `OIDCTokenExchange.from_agentcore()`

AgentCore Identity is RFC 8693-compatible. Our `OIDCTokenExchange`
already implements the `urn:ietf:params:oauth:grant-type:token-exchange`
grant. The integration is a factory that fills in the AgentCore
endpoint:

```python
from eap_core.integrations.agentcore import OIDCTokenExchange

ex = OIDCTokenExchange.from_agentcore(
    region="us-east-1",
    workload_identity_id="my-agent-workload-id",
)
token = await ex.exchange(
    subject_token=initial_jwt,
    audience="https://my-tool.example.com",
    scope="read:accounts",
)
```

Everything downstream (the `NonHumanIdentity` cache, the per-tool
token attachment in `client.invoke_tool`) works unchanged. The same
code points at Okta / Auth0 / Cognito by changing the factory call.

### 4. Policy — already aligned

AgentCore Policy uses [Cedar](https://www.cedarpolicy.com/en) at the
Gateway tier. Our `PolicyMiddleware` runs Cedar-shaped JSON rules
(default) or real Cedar via the `[policy-cedar]` extra in the agent's
process. **Use both** — same `configs/policy.json` (or `.cedar` file)
enforced at two tiers. If the Gateway is bypassed (direct call,
internal API), the agent still enforces the policy.

## Phase B — what's shipped

Phase B is in. EAP-Core now exposes in-process abstractions for
AgentCore Memory, Code Interpreter, Browser, and an inbound JWT
verifier. Live AgentCore calls are gated behind
`EAP_ENABLE_REAL_RUNTIMES=1` (same env-flag pattern as the Bedrock /
Vertex runtime adapters); without the flag, every method raises a
clear `NotImplementedError` with a "wire credentials" message.

### Memory — `eap_core.memory` + `AgentCoreMemoryStore`

```python
from eap_core.memory import InMemoryStore, MemoryStore
from eap_core.integrations.agentcore import AgentCoreMemoryStore
from eap_core.types import Context

# Dev / tests — process-local dict.
ctx = Context(memory_store=InMemoryStore(), session_id="session-1")

# Production — AgentCore Memory.
ctx = Context(
    memory_store=AgentCoreMemoryStore(memory_id="my-memory-id", region="us-east-1"),
    session_id="session-1",
)

await ctx.memory_store.remember("session-1", "favorite_seat", "window")
seat = await ctx.memory_store.recall("session-1", "favorite_seat")
```

The `MemoryStore` Protocol has five methods (`remember`, `recall`,
`list_keys`, `forget`, `clear`). Both backends are
`runtime_checkable` Protocol-conformant. `Context` now carries an
optional `memory_store` field and `session_id` for per-session
isolation.

### Code Interpreter — `register_code_interpreter_tools()`

```python
from eap_core.integrations.agentcore import register_code_interpreter_tools
from eap_core.mcp import default_registry

register_code_interpreter_tools(default_registry(), region="us-east-1")

# Now ``client.invoke_tool("execute_python", {"code": "..."})`` runs
# through the full middleware chain (sanitize / PII / policy / OTel /
# validate) before hitting the AgentCore Code Interpreter sandbox.
```

Registers three `@mcp_tool` functions: `execute_python`,
`execute_javascript`, `execute_typescript`. Each returns
`{"stdout": str, "stderr": str, "exit_code": int}`.

### Browser — `register_browser_tools()`

```python
from eap_core.integrations.agentcore import register_browser_tools

register_browser_tools(default_registry(), region="us-east-1")
```

Registers five tools: `browser_navigate`, `browser_click`,
`browser_fill`, `browser_extract_text`, `browser_screenshot`. Every
browser action runs through the same middleware chain as any other
tool — policy rules can deny `browser_navigate` to specific
hostnames, observability records every action.

### Inbound JWT verification — `InboundJwtVerifier`

```python
from eap_core.integrations.agentcore import InboundJwtVerifier, jwt_dependency
from fastapi import Depends

verifier = InboundJwtVerifier(
    discovery_url="https://your-idp.example/.well-known/openid-configuration",
    allowed_audiences=["my-agent"],
    allowed_scopes=["agent:invoke"],
)

@app.post("/invocations", dependencies=[Depends(jwt_dependency(verifier))])
async def invocations(req: InvocationRequest): ...
```

`InboundJwtVerifier` fetches JWKS from the OIDC discovery URL,
caches keys, and validates audience / scope / client claims against
the configured allow-lists. The `jwt_dependency()` factory builds a
FastAPI `Depends`-friendly callable that pulls the bearer token from
the `Authorization` header.

**When to use this:** outside AgentCore Runtime (your own infra,
Lambda, Cloud Run) and you want the same auth model; or as defense
in depth inside AgentCore Runtime (the configured inbound
authorizer already verifies upstream, but a second check inside the
agent makes audit replay simpler and protects against
misconfiguration).

## Phase C — what's shipped

### Outbound — call Gateway-hosted tools from your agent

`GatewayClient` is an MCP-over-HTTP client (plain JSON-RPC 2.0 — the
shape Gateway speaks). `add_gateway_to_registry` registers the
gateway's tools as proxy specs in your local `McpToolRegistry`, so
`client.invoke_tool("name", args)` runs through the agent's full
middleware chain locally before forwarding to the gateway.

```python
from eap_core.integrations.agentcore import (
    GatewayClient,
    add_gateway_to_registry,
)
from eap_core.mcp import default_registry

gw = GatewayClient(
    gateway_url="https://your-gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp",
    identity=nhi,            # NonHumanIdentity — supplies OAuth tokens
    audience="my-gateway",   # token audience for scope binding
)
specs = await gw.list_tools()
add_gateway_to_registry(default_registry(), gw, specs)

# Now any tool the gateway hosts is reachable through the local
# registry with full middleware chain enforcement.
result = await client.invoke_tool("lookup_account", {"id": "acct-1"})
```

Auth is pluggable: pass an `httpx` `auth=` object for AWS SigV4, or
use an `identity` for OAuth Bearer tokens. The `MCPError` exception
fires on JSON-RPC errors and HTTP 4xx/5xx with the gateway's
`tool_name` attached so audit replay knows which tool failed.

### Inbound — publish your tools to Gateway

`eap publish-to-gateway` generates an OpenAPI 3.1 spec from your
project's `@mcp_tool` registrations. Gateway accepts OpenAPI as an
HTTP target type, so this is the lowest-friction way to make your
tools discoverable through Gateway:

```bash
eap publish-to-gateway --title "my-bank-tools" \\
                       --server-url https://my-agent.example
```

Output at `dist/gateway/`:

- `openapi.json` — every `@mcp_tool` becomes a `POST /tools/<name>`
  operation. Input schema comes from the tool's type hints (via the
  same Pydantic `TypeAdapter` path as the decorator); the
  `x-mcp-tool.requires_auth` extension flags auth-required tools so
  Gateway can apply outbound auth.
- `README.md` — upload-to-S3 / register-with-Gateway commands.

Live API registration (creating a Gateway target via the AWS API) is
not yet automated — Phase D refinement. The OpenAPI artifact is the
hand-off point.

## Phase D — what's shipped

Phase D closes feature parity. Live AgentCore API calls in each
section are gated behind `EAP_ENABLE_REAL_RUNTIMES=1`.

### Registry — `RegistryClient`

```python
from eap_core.integrations.agentcore import RegistryClient
from eap_core.a2a import build_card

rc = RegistryClient(registry_name="org-registry", region="us-east-1")
card = build_card(name="bank-agent", description="...", skills_from=registry)
record_id = await rc.publish_agent_card(card)

# Discover an agent by name, or search semantically:
record = await rc.get_record("bank-agent")
hits = await rc.search("agents that handle account transfers")
```

`publish_agent_card`, `publish_mcp_server`, `get_record`, `search`,
and `list_records` map directly to AWS Agent Registry's control-plane
API. The registry can also be reached via its MCP endpoint — use
`GatewayClient` for that path.

### Payments — `PaymentRequired` + `PaymentClient`

Two pieces. A tool wrapper raises `PaymentRequired` when it sees an
upstream `HTTP 402`. The `PaymentClient` opens a budget-limited
`PaymentSession` and signs payments via the configured wallet
provider (Coinbase CDP or Stripe/Privy).

```python
from eap_core.integrations.agentcore import (
    PaymentClient, PaymentRequired,
)

pc = PaymentClient(
    wallet_provider_id="my-cdp-wallet",
    max_spend_cents=100,        # $1.00 hard cap
    session_ttl_seconds=3600,
)
await pc.start_session()

try:
    result = await client.invoke_tool("paid_data", {"query": "..."})
except PaymentRequired as pr:
    if pc.can_afford(pr.amount_cents):
        receipt = await pc.authorize_and_retry(pr)
        # caller uses `receipt` to retry the original tool call with
        # X-Payment-Receipt header set
```

Budget bookkeeping is enforced in-process (`can_afford`,
`remaining_cents`) so an agent can pre-check before any AWS call;
the actual payment authorization (which deducts from the budget)
happens through AgentCore Payments.

### Evaluations — bidirectional adapters

**Export:** `to_agentcore_eval_dataset(trajectories)` converts our
`Trajectory` records to AgentCore Eval's question/answer/contexts/
trace_id/steps shape:

```python
from eap_core.integrations.agentcore import to_agentcore_eval_dataset

rows = to_agentcore_eval_dataset(recorder.trajectories)
# `rows` is a list of plain dicts; upload to S3 or pass to boto3
```

**Import:** `AgentCoreEvalScorer` implements our `_ScorerProto` so
it plugs into `EvalRunner.scorers` alongside our deterministic
scorer:

```python
from eap_core.eval import EvalRunner, FaithfulnessScorer, DeterministicJudge
from eap_core.integrations.agentcore import AgentCoreEvalScorer

helpfulness = AgentCoreEvalScorer(
    evaluator_arn="arn:aws:bedrock-agentcore:::evaluator/Builtin.Helpfulness",
    scorer_name="helpfulness",
)
faithfulness = FaithfulnessScorer(judge=DeterministicJudge())

runner = EvalRunner(agent=my_agent, scorers=[helpfulness, faithfulness])
report = await runner.run(cases)
# report.aggregate now has both "helpfulness" and "faithfulness" entries
```

A single `EvalReport` carries scores from both our deterministic
in-process scorer and AgentCore's managed evaluator. Useful for
side-by-side comparison or for using AgentCore's LLM-as-judge
evaluators as the primary metric while keeping our deterministic
scorer as a CI smoke check.

## Why phase this way

The phases are ordered so each phase is **independently shippable**
and adds value on its own:

- **Phase A** gets you to "EAP-Core agents run on AgentCore Runtime
  with OTel and Identity wired." Most enterprise teams can stop here
  and be productive.
- **Phase B** unlocks the agent-superpowers AgentCore offers (Memory,
  Code Interpreter, Browser) as middleware-gated, policy-enforced
  tools rather than direct calls. Plus inbound JWT verification.
- **Phase C** turns your agent into a citizen of an AgentCore tool
  ecosystem (consumes Gateway-hosted tools via `GatewayClient`;
  publishes its own via `eap publish-to-gateway`).
- **Phase D** closes feature parity: Registry discoverability,
  x402 microtransactions, bidirectional Evaluations integration.

All four phases are shipped as of v0.1.0+. Every live AgentCore call
is gated behind `EAP_ENABLE_REAL_RUNTIMES=1` so tests stay
deterministic and CI doesn't need AWS credentials.

## Defense in depth

The single most important architectural property of this
integration: **EAP-Core's middleware chain runs in the agent's own
process, before any AgentCore-managed service sees the data**. That
means:

- PII masked locally before it goes to AgentCore Memory storage.
- Prompt injection blocked before the user-supplied text reaches
  Code Interpreter.
- Policy denials happen even if the call bypasses Gateway (internal
  test, direct invoke).
- OTel spans capture the agent's reasoning before the data leaves
  the agent boundary.

If a future incident exposes an AgentCore-managed service, your
agent's data was already minimized and policy-checked at the source.
That's the load-bearing claim of EAP-Core, and it composes
multiplicatively with everything AgentCore provides.
