# Integrating EAP-Core with GCP Vertex AI Agent Engine

This document explains how EAP-Core integrates with each
[Gemini Enterprise Agent Platform / Vertex AI Agent Engine](https://cloud.google.com/blog/products/ai-machine-learning/introducing-gemini-enterprise-agent-platform)
service, the cross-cloud architecture that motivated the integration,
and what's shipped across Phases A–D.

## TL;DR — positioning

AgentCore and Vertex Agent Engine ship the same set of cross-cutting
concerns under different brands: managed code/browser sandboxes,
managed memory, agent gateways, agent registries, payments, eval,
observability, and IAM. EAP-Core treats them as **interchangeable
backends** behind vendor-neutral Protocols.

EAP-Core sits **inside** the Vertex-deployed agent. Vertex provides
the platform (Agent Runtime, Agent Sandbox, Memory Bank, Agent
Gateway, Agent Registry, AP2 payments, Gen AI Eval, Cloud Trace).
EAP-Core provides the in-process layer that:

- Enforces sanitization, PII masking, policy, schema validation **in
  the agent's own process** before any data crosses the trust
  boundary to Vertex-managed services. Defense in depth — the same
  policy runs in-process even when calls bypass the Gateway.
- Stays portable: the same `agent.py` runs on Vertex Agent Runtime,
  Cloud Run, AWS Bedrock AgentCore, or a VM. `eap deploy` picks
  the packaging.
- Picks the open standards Vertex picks (MCP, A2A, OTel GenAI,
  OAuth 2.1, AP2) so the integration is mostly *swap an endpoint*.

## Cross-cloud equivalence

Every Vertex feature has an AgentCore counterpart and an EAP-Core
Protocol. The Protocol is the **interchange shape**; the cloud-backed
classes are drop-in implementations.

| EAP-Core Protocol | AWS Bedrock AgentCore impl | GCP Vertex Agent Engine impl |
|---|---|---|
| `MemoryStore` | `AgentCoreMemoryStore` | `VertexMemoryBankStore` |
| `CodeSandbox` | (AgentCore Code Interpreter) | `VertexCodeSandbox` |
| `BrowserSandbox` | (AgentCore Browser) | `VertexBrowserSandbox` |
| `AgentRegistry` | `RegistryClient` (AWS Agent Registry) | `VertexAgentRegistry` |
| `PaymentBackend` | `PaymentClient` (x402) | `AP2PaymentClient` (AP2) |
| `Scorer` (eval) | `AgentCoreEvalScorer` | `VertexEvalScorer` |
| `NonHumanIdentity`-shaped | `OIDCTokenExchange` | `VertexAgentIdentityToken` |
| Outbound MCP-HTTP client | `agentcore.GatewayClient` | `VertexGatewayClient` |

If your agent depends on the Protocol (not the concrete class), it
runs unmodified on either cloud.

## Service-by-service mapping

| Vertex service | EAP-Core position | Status |
|---|---|---|
| **Agent Runtime** (managed serving) | Deploy target via `eap deploy --runtime vertex-agent-engine` | **Phase A — shipped** |
| **Cloud Trace / Agent Observability** | `ObservabilityMiddleware` emits OTel GenAI spans; `configure_for_vertex_observability()` wires OTLP exporter | **Phase A — shipped** |
| **IAM / Workload Identity** | `VertexAgentIdentityToken` wraps Application Default Credentials → workload identity federation | **Phase A — shipped** |
| **Memory Bank** | `MemoryStore` Protocol + `VertexMemoryBankStore` | **Phase B — shipped** |
| **Agent Sandbox (code)** | `CodeSandbox` Protocol + `VertexCodeSandbox` + `register_code_sandbox_tools()` | **Phase B — shipped** |
| **Agent Sandbox (browser)** | `BrowserSandbox` Protocol + `VertexBrowserSandbox` + `register_browser_sandbox_tools()` | **Phase B — shipped** |
| **Agent Gateway** (MCP/HTTP) | `VertexGatewayClient` (outbound MCP); `eap publish-to-gateway` (OpenAPI export, shared with AgentCore) | **Phase C — shipped** |
| **Agent Registry** | `AgentRegistry` Protocol + `VertexAgentRegistry` | **Phase D — shipped** |
| **AP2 (Agent Payment Protocol)** | `PaymentBackend` Protocol + `AP2PaymentClient` | **Phase D — shipped** |
| **Gen AI Eval Service** | `to_vertex_eval_dataset` (export) + `VertexEvalScorer` (in-flow scorer) | **Phase D — shipped** |

## Phase A — what's shipped

### 1. `eap deploy --runtime vertex-agent-engine`

Packages your project as a Cloud Run-compatible image (Vertex Agent
Runtime extends Cloud Run): `linux/amd64`, listens on the `PORT` env
var, exposes `EXPOSE 8080`, and serves `POST /invocations` plus
`GET /health`.

```bash
eap deploy --runtime vertex-agent-engine [--entry agent.py:answer]
```

**Output** at `dist/vertex-agent-engine/`:

```
dist/vertex-agent-engine/
├── Dockerfile          # linux/amd64 base, PORT env, EXPOSE 8080
├── handler.py          # FastAPI: POST /invocations + GET /health
├── README.md           # Build/push to Artifact Registry; register with Vertex
└── <your project files>
```

`handler.py` honors the Cloud Run convention: it binds to
`0.0.0.0:${PORT:-8080}`, calls your entry function with the prompt,
and returns the standard `{"response": ..., "status": "success"}`
shape. The middleware chain runs unchanged.

Live `docker build` is gated behind `EAP_ENABLE_REAL_DEPLOY=1`. Without
the flag, the README walks through the manual build/push to Artifact
Registry + Vertex registration.

### 2. `configure_for_vertex_observability()`

Wires the OTel SDK to a Cloud Trace OTLP-compatible endpoint:

```python
from eap_core.integrations.vertex import configure_for_vertex_observability

configure_for_vertex_observability(
    project_id="my-gcp-project",          # → gcp.project_id resource attr
    service_name="my-agent",              # → service.name resource attr
    endpoint="https://telemetry.googleapis.com",
)
```

Returns `True` when configured, `False` when the `[otel]` extra is
missing (the middleware still writes `gen_ai.*` attributes to
`ctx.metadata`, so downstream layers can read them regardless).

When the agent runs *inside* Vertex Agent Runtime, the platform
auto-injects OTLP env vars and this helper is unnecessary. Outside
Vertex, call it once during setup.

### 3. `VertexAgentIdentityToken`

Wraps the standard Google auth chain (Application Default Credentials
→ workload identity federation → IAM service account) with a
`get_token(audience=..., scope=...)` signature that matches
`NonHumanIdentity`. Drop it into `VertexGatewayClient` or pass it
where any `NonHumanIdentity`-shaped object is expected:

```python
from eap_core.integrations.vertex import VertexAgentIdentityToken, VertexGatewayClient

identity = VertexAgentIdentityToken(
    scopes=["https://www.googleapis.com/auth/cloud-platform"],
)
client = VertexGatewayClient(
    gateway_url="https://my-gw.example.com/mcp",
    identity=identity,
)
```

Live `google.auth` calls are gated by `EAP_ENABLE_REAL_RUNTIMES=1`.
Construction is cheap and does not import the GCP SDK.

## Phase B — Memory + Sandboxes

### Memory Bank

```python
from eap_core.integrations.vertex import VertexMemoryBankStore

mem = VertexMemoryBankStore(
    project_id="my-proj",
    location="us-central1",
    memory_bank_id="my-bank",
)
# mem satisfies the MemoryStore Protocol:
await mem.remember("session-1", "user_pref_lang", "en")
val = await mem.recall("session-1", "user_pref_lang")
```

Use anywhere a `MemoryStore` is accepted (e.g. `Context.memory_store`).
Swapping to AgentCore is a single constructor change.

### Code Sandbox

```python
from eap_core.integrations.vertex import (
    VertexCodeSandbox,
    register_code_sandbox_tools,
)
from eap_core.mcp import McpToolRegistry

# Direct use of the CodeSandbox Protocol:
sb = VertexCodeSandbox(project_id="my-proj")
result = await sb.execute("python", "print(2+2)")
print(result.stdout)    # "4\n"
print(result.exit_code) # 0

# Register MCP tools the agent can call:
registry = McpToolRegistry()
register_code_sandbox_tools(registry, project_id="my-proj")
# Now the LLM can call execute_python / execute_javascript / execute_typescript.
```

Tool invocations traverse the middleware chain (sanitize / PII /
policy / observability) **before** the code reaches the Vertex
Sandbox. This is intentional: code execution is one of the highest-
risk agentic capabilities and must flow through the safety chain.

### Browser Sandbox

```python
from eap_core.integrations.vertex import register_browser_sandbox_tools

register_browser_sandbox_tools(registry, project_id="my-proj")
# Agent can call: browser_navigate, browser_click, browser_fill,
# browser_extract_text, browser_screenshot.
```

`VertexBrowserSandbox` also satisfies the `BrowserSandbox` Protocol if
your code wants to drive the browser directly without going through
the MCP tool layer.

## Phase C — Agent Gateway

`VertexGatewayClient` is a JSON-RPC 2.0 MCP client suitable for any
MCP-HTTP endpoint; the supported Google deployment is the Vertex
Agent Gateway.

```python
from eap_core.integrations.vertex import VertexGatewayClient, VertexAgentIdentityToken

identity = VertexAgentIdentityToken()
gw = VertexGatewayClient(
    gateway_url="https://my-gw.example.com/mcp",
    identity=identity,
)

specs = await gw.list_tools()
result = await gw.invoke("search_internal_docs", {"q": "EAP"})
```

The wire shape is identical to `agentcore.GatewayClient` — pointing at
either gateway is a constructor swap. Inbound publishing
(`eap publish-to-gateway`) reuses the same OpenAPI exporter for both
clouds.

## Phase D — Registry, Payments (AP2), Eval

### Agent Registry

```python
from eap_core.integrations.vertex import VertexAgentRegistry

reg = VertexAgentRegistry(project_id="my-proj", registry_id="default")
await reg.publish({
    "name": "doc-search-agent",
    "record_type": "AGENT",
    "description": "Internal documentation search agent.",
})
hits = await reg.search("documentation")
```

Implements the `AgentRegistry` Protocol — feed it to `RegistryMiddleware`
or any code that depends on the Protocol.

### Payments — AP2 (Agent Payment Protocol)

```python
from eap_core.integrations.vertex import AP2PaymentClient
from eap_core.payments import PaymentRequired

pay = AP2PaymentClient(
    wallet_provider_id="my-cdp-wallet",
    project_id="my-proj",
    max_spend_cents=100,          # $1.00 budget
    session_ttl_seconds=3600,
)
await pay.start_session()

try:
    result = await my_tool_that_might_require_payment()
except PaymentRequired as pr:
    receipt = await pay.authorize(pr)
    # retry the original call with X-Payment-Receipt header
```

`PaymentRequired` is the same exception class used for AWS x402.
`AP2PaymentClient` shares method shape with `agentcore.PaymentClient`
so the flow is identical regardless of cloud.

### Evaluations

```python
from eap_core.integrations.vertex import VertexEvalScorer, to_vertex_eval_dataset
from eap_core.eval.runner import EvalRunner

# Drop a Vertex-backed scorer into the runner alongside any local scorers.
runner = EvalRunner(
    scorers=[VertexEvalScorer(project_id="my-proj", metric="faithfulness")],
)
report = await runner.run(trajectories)

# Or export Trajectory records as a Vertex Eval dataset (e.g. for upload):
rows = to_vertex_eval_dataset(trajectories)
```

`VertexEvalScorer` returns `FaithfulnessResult` in the same shape as
local scorers and `AgentCoreEvalScorer`, so reports stay homogeneous.

## Choosing between AgentCore and Vertex

The honest answer: pick the cloud you already run. EAP-Core's
abstractions let you delay the decision until cost or feature parity
tips it one way. A practical heuristic:

- **Memory** — both managed stores are roughly comparable. If you're
  on GCP already, `VertexMemoryBankStore` removes the IAM/network
  detour to AWS.
- **Code/Browser sandboxes** — both expose roughly the same primitives.
  Vertex Agent Sandbox is closer to Cloud Run's billing model
  (per-request); AgentCore Code Interpreter is closer to
  Bedrock's (per-second).
- **Gateway** — AgentCore Gateway has richer per-target auth; Vertex
  Agent Gateway integrates more smoothly with existing Apigee.
- **Payments** — `x402` (AWS) and `AP2` (Google) are competing IETF-
  track specs. EAP-Core's `PaymentBackend` Protocol abstracts them.
- **Eval** — AgentCore Evaluations has more built-in evaluator ARNs
  out of the box today; Vertex Gen AI Eval has more native metric
  variety. Both feed the same `FaithfulnessResult` shape.

The point of EAP-Core is that "agnostic across both" is a default
posture, not a project.

## Live calls and gating

Every live call to GCP is gated behind `EAP_ENABLE_REAL_RUNTIMES=1`
(for runtime APIs) or `EAP_ENABLE_REAL_DEPLOY=1` (for `docker build`
during deploy). Without the flag, the integration raises
`NotImplementedError` with a setup hint. CI runs the full test suite
without GCP credentials this way.

## Extras

Install the GCP extra to get the Google SDKs:

```bash
pip install 'eap-core[gcp]'
# or via uv:
uv pip install -e 'packages/eap-core[gcp]'
```

The extra adds `google-cloud-aiplatform`, which transitively brings in
`google-auth` and `google-auth-transport-requests`. Without the extra,
every Vertex class is still importable (lazy imports inside methods);
construction just doesn't do any GCP I/O.
