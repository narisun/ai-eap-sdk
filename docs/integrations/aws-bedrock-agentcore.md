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
| **Memory** (short/long-term, cross-session) | `MemoryStore` Protocol + `[agentcore-memory]` extra | Phase B |
| **Gateway** (APIs/Lambdas → MCP) | `GatewayClient` for outbound; `eap publish-to-gateway` for inbound | Phase C |
| **Code Interpreter** (Python/JS sandboxes) | `[agentcore-code-interpreter]` extra wraps as MCP tools | Phase B |
| **Browser** (cloud browser) | `[agentcore-browser]` extra wraps as MCP tools | Phase B |
| **Payments** (x402 microtransactions) | `PaymentMiddleware` intercepts 402 responses | Phase D |
| **Evaluations** (trace-based eval) | Bidirectional adapters: export `Trajectory` → AgentCore Eval; consume AgentCore Eval results as a `Scorer` | Phase D |
| **Registry** (org-wide tool/agent catalog) | `RegistryClient` to publish/pull `AgentCard` | Phase D |

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

## Phase B — planned (in-process AgentCore service adapters)

- **`[agentcore-memory]` extra** — `MemoryStore` Protocol with an
  AgentCore-backed implementation. Plumbs through `Context` so
  middleware can read/write conversation and long-term memory.
  Compatible with the SDK's existing "BYO state" stance — Memory is
  opt-in.
- **`[agentcore-code-interpreter]` extra** — wraps the Code
  Interpreter sandbox as `@mcp_tool` functions (`execute_python`,
  `execute_javascript`, `execute_typescript`). Tool calls go through
  the normal `client.invoke_tool` path so they're sanitized,
  policy-gated, and recorded in the trajectory.
- **`[agentcore-browser]` extra** — wraps the Browser tool API as
  MCP tools. Exposes Playwright-equivalent primitives
  (`navigate`, `click`, `fill`, `extract_text`, etc.).
- **`InboundJwtMiddleware`** — verifies the caller JWT on incoming
  requests to the agent. Pairs with AgentCore Identity's inbound JWT
  authorizer for cases where the agent is invoked outside AgentCore
  Runtime (e.g. behind another API gateway).

## Phase C — planned (Gateway integration)

- **`GatewayClient`** — outbound: dispatches `client.invoke_tool` to
  a remote AgentCore Gateway over MCP-HTTP. Lets your agent use
  AgentCore-hosted tools (Salesforce, Slack, JIRA, etc.) without
  caring about the Gateway URL.
- **`eap publish-to-gateway`** — inbound: takes your project's
  `@mcp_tool`-decorated functions and registers them with AgentCore
  Gateway as MCP tools. Other AgentCore-deployed agents can then call
  them via Gateway.

## Phase D — planned

- **`RegistryClient`** — publishes your `AgentCard` (auto-built from
  your tool registry via `build_card`) to AgentCore Registry; pulls
  other agents' cards for discovery.
- **`PaymentMiddleware`** — intercepts 402 responses from tool calls
  and pays via AgentCore Payments (x402 protocol). Configurable
  per-tool spending limits.
- **AgentCore Eval bidirectional adapters** — export our
  `Trajectory` JSONL to AgentCore Eval input format; ingest AgentCore
  Eval results into our `EvalReport` so a single dashboard shows
  scores from both systems.

## Why phase this way

The phases are ordered so each phase is **independently shippable**
and adds value on its own:

- **Phase A** alone gets you to "EAP-Core agents run on AgentCore
  Runtime with OTel and Identity wired." Most enterprise teams can
  stop here and be productive.
- **Phase B** unlocks the agent-superpowers AgentCore offers (Memory,
  Code Interpreter, Browser) as middleware-gated, policy-enforced
  tools rather than direct calls.
- **Phase C** turns your agent into a citizen of an AgentCore tool
  ecosystem (consumes Gateway-hosted tools; publishes its own).
- **Phase D** is the polish that closes feature parity (Registry
  discoverability, microtransactions, eval integration).

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
