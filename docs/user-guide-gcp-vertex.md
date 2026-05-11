# EAP-Core User Guide — GCP Vertex AI Agent Engine

This guide is for engineers **building** an agent on GCP Vertex
Agent Engine using EAP-Core. It assumes you're new to both — every
step spells out the exact command, the exact code, and what to look
for if it doesn't work.

If you're extending the SDK rather than building on it, read
[`docs/developer-guide.md`](developer-guide.md) instead. If you only
want positioning and the service-by-service map, read
[`docs/integrations/gcp-vertex-agent-engine.md`](integrations/gcp-vertex-agent-engine.md).

**Two parts:**

- **Part 1 — Tutorial.** End-to-end walkthrough from `eap init` to a
  deployed Vertex Agent Engine agent that uses Memory Bank, Code
  Sandbox, Browser Sandbox, Gateway, Registry, AP2 Payments, and
  Gen AI Eval.
- **Part 2 — Per-task reference.** Look up "how do I wire X" without
  re-reading the tutorial.
- **Part 3 — Production checklist.** What to verify before turning
  on live traffic.

> **Already on AgentCore?** The guide structure mirrors
> [`docs/user-guide-aws-agentcore.md`](user-guide-aws-agentcore.md)
> step-for-step. Each EAP-Core abstraction (`MemoryStore`,
> `CodeSandbox`, `BrowserSandbox`, `AgentRegistry`, `PaymentBackend`)
> swaps to its Vertex implementation by constructor change alone —
> your business logic stays the same.

> **Working reference project:** every snippet below is wired up
> end-to-end at
> [`examples/vertex-bank-agent/`](../examples/vertex-bank-agent/).
> It runs locally with no GCP credentials (stubs swap in via
> env-flag gating); set `EAP_ENABLE_REAL_RUNTIMES=1` and configure
> ADC to graduate. The AWS counterpart at
> [`examples/agentcore-bank-agent/`](../examples/agentcore-bank-agent/)
> has the same `agent.py` — only `cloud_wiring.py` differs.

---

## Part 1 — Tutorial: zero to deployed Vertex agent

### 1.1 Prerequisites

- **Python 3.11+** (3.12 recommended).
- **uv** — `curl -LsSf https://astral.sh/uv/install.sh | sh`. The
  workspace assumes uv.
- **GCP project** with Vertex AI API enabled. You'll need:
  - A service account with `roles/aiplatform.user` (and
    `roles/aiplatform.admin` for first-time setup).
  - An Artifact Registry repository for the runtime image.
  - A Memory Bank (set up in the Vertex console under **Agent
    Builder → Memory Banks**) once you reach Step 1.8.
  - **Application Default Credentials** wired locally:
    `gcloud auth application-default login`. In production, use a
    service account JSON or workload identity federation.
- **Docker** (only for deploy). Local development needs no Docker.

You do not need any of the GCP pieces for the first few steps. We
build and run the agent locally first.

### 1.2 Install EAP-Core

From the SDK repo:

```bash
git clone https://github.com/narisun/ai-eap-sdk.git
cd ai-eap-sdk
uv sync --all-packages --group dev --extra gcp --extra otel --extra mcp
```

The `[gcp]` extra pulls `google-cloud-aiplatform` (which transitively
brings in `google-auth` + `google-auth-transport-requests`). The
`[otel]` extra pulls the OTel SDK + OTLP exporter (Vertex Agent
Observability ingests OTLP into Cloud Trace). The `[mcp]` extra
pulls the official MCP SDK if you want to expose tools as an MCP
stdio server.

For a downstream project, depend on EAP-Core directly:

```toml
# pyproject.toml
[project]
dependencies = [
    "eap-core[gcp,otel,mcp] @ git+https://github.com/narisun/ai-eap-sdk.git@v0.3.1#subdirectory=packages/eap-core",
    "eap-cli @ git+https://github.com/narisun/ai-eap-sdk.git@v0.3.1#subdirectory=packages/eap-cli",
]
```

### 1.3 Scaffold a project

```bash
uv run eap init my-bank-agent --runtime local
cd my-bank-agent
```

We pick `--runtime local` for now so the agent runs without GCP
credentials. We'll flip it to `vertex` later. The scaffold produces:

```
my-bank-agent/
├── agent.py                # business logic — uses EnterpriseLLM
├── responses.yaml          # canned local-runtime responses
├── configs/
│   ├── policy.json         # JSON policy (Cedar-shaped)
│   └── agent_card.json     # A2A AgentCard
├── tests/
│   └── golden_set.json     # eval cases
├── tools/                  # MCP tools (empty until you add some)
└── pyproject.toml
```

The whole thing is ~40 lines of business logic. The middleware chain
(prompt-injection sanitization, PII masking, OTel attributes, policy
enforcement, output validation) is already wired into `agent.py`.

### 1.4 Run it locally

```bash
python agent.py
# → "[local-runtime] received N tokens, model=echo-1"
```

That's the full loop with no GCP account: the middleware chain runs
on every call, the local runtime returns canned responses from
`responses.yaml`, and your output goes through schema validation.

### 1.5 Add a tool

```bash
uv run eap create-tool --name lookup_account --mcp --auth-required
```

This creates `tools/lookup_account.py` with a typed Python function
decorated with `@mcp_tool`. JSON Schema is generated from the type
hints. `--auth-required` marks the tool as needing an OAuth token —
the NHI flow handles token acquisition, and the policy middleware
rejects unscoped calls.

Open `tools/lookup_account.py` and replace the body with real logic.
The tool is now callable in three ways:

- In-process: `await client.invoke_tool("lookup_account", {"id": 42})`.
- Over MCP-stdio: `uv run python -m eap_core.mcp.server`.
- Over Vertex Agent Gateway once you publish (Step 1.13).

### 1.6 Wire workload identity (Application Default Credentials → tool tokens)

The agent needs to authenticate itself to downstream APIs. On GCP,
you don't use RFC 8693 token exchange — you use the standard Google
auth chain (Application Default Credentials → workload identity
federation → IAM service account). EAP-Core wraps it:

```python
# add to agent.py
from eap_core.integrations.vertex import VertexAgentIdentityToken

identity = VertexAgentIdentityToken(
    scopes=["https://www.googleapis.com/auth/cloud-platform"],
)
# identity.get_token(audience=..., scope=...) returns a Google access token.
```

`VertexAgentIdentityToken` shares the `get_token(audience=, scope=)`
shape with `NonHumanIdentity` from the AgentCore guide, so anywhere
the SDK accepts an identity (`GatewayClient`, etc.) you can pass it
directly. The `audience` and `scope` arguments are accepted for
signature compatibility but ignored — Google tokens are scoped at
credential-creation time via `scopes`.

For dev, ADC from `gcloud auth application-default login` works.
For production, attach the service account to the workload (Cloud
Run revision, GKE pod, etc.) and let workload identity federation
do the rest. See [§2.1](#21-authentication-and-credentials) below.

### 1.7 Wire Vertex Agent Observability (OTel → Cloud Trace)

When your agent runs **inside** Vertex Agent Runtime, the platform
auto-injects the OTLP env vars and you don't need to call anything.
For local dev (or any other deploy target), wire it explicitly:

```python
# add to agent.py — call once at startup
from eap_core.integrations.vertex import configure_for_vertex_observability

configure_for_vertex_observability(
    project_id="my-gcp-project",
    service_name="my-bank-agent",
    endpoint="https://telemetry.googleapis.com",   # Cloud Trace OTLP-compatible
)
```

Sets the `service.name` and `gcp.project_id` resource attributes and
wires the OTel SDK to a Cloud Trace OTLP endpoint. Returns `False`
if the `[otel]` extra isn't installed (the middleware still writes
`gen_ai.*` attributes to `ctx.metadata`, so audit and trajectory
recording keep working).

### 1.8 Wire Vertex Memory Bank

Vertex AI Memory Bank persists per-session short-term memory and
long-term cross-session facts. Plug it in via the `MemoryStore`
Protocol:

```python
from eap_core.integrations.vertex import VertexMemoryBankStore

memory = VertexMemoryBankStore(
    project_id="my-gcp-project",
    location="us-central1",
    memory_bank_id="my-bank-memory",
)

# In your tool / handler:
await memory.remember(session_id="user-123", key="last_balance", value="$1,234.56")
balance = await memory.recall(session_id="user-123", key="last_balance")
```

Construction is cheap — no GCP SDK import, no network call. All live
calls are gated by `EAP_ENABLE_REAL_RUNTIMES=1`; until you set it,
methods raise `NotImplementedError` with a clear "wire credentials"
message. This keeps unit tests deterministic.

The class satisfies `eap_core.memory.MemoryStore`. Drop it anywhere a
`MemoryStore` is expected (`Context.memory_store`, your own
middleware, etc.). Swapping to AgentCore is a one-line constructor
change to `AgentCoreMemoryStore(memory_id=..., region=...)`.

### 1.9 Add Code Sandbox tools

Vertex Agent Sandbox is the managed code-execution environment.
Register the three default tools on your registry:

```python
from eap_core.mcp import default_registry
from eap_core.integrations.vertex import register_code_sandbox_tools

register_code_sandbox_tools(
    default_registry(),
    project_id="my-gcp-project",
    location="us-central1",
)
```

This adds three MCP tools: `execute_python`, `execute_javascript`,
`execute_typescript`. The LLM can now call them like any other tool;
each call traverses your middleware chain **before** the code
reaches the sandbox.

**This matters.** Code execution is one of the highest-risk agentic
capabilities. Running it through your sanitize / PII / policy /
observability middleware means:

- Prompt injection in the generated code is rejected at the agent
  edge, not the sandbox.
- Policy can deny `execute_python` for unprivileged identities.
- Every execution is a span with the standard `gen_ai.*` attrs.

For direct (non-LLM) invocation, `VertexCodeSandbox` also satisfies
the `CodeSandbox` Protocol:

```python
from eap_core.integrations.vertex import VertexCodeSandbox

sb = VertexCodeSandbox(project_id="my-gcp-project")
result = await sb.execute("python", "print(2+2)")
# result.stdout == "4\n", result.exit_code == 0
```

### 1.10 Add Browser Sandbox tools

Same pattern for Vertex Browser Sandbox:

```python
from eap_core.integrations.vertex import register_browser_sandbox_tools

register_browser_sandbox_tools(
    default_registry(),
    project_id="my-gcp-project",
    location="us-central1",
)
```

Registers five MCP tools: `browser_navigate`, `browser_click`,
`browser_fill`, `browser_extract_text`, `browser_screenshot`. Each
traverses the chain — policy can deny `browser_navigate` to specific
hostnames, observability records every action as a span.

`VertexBrowserSandbox` also satisfies the `BrowserSandbox` Protocol if
you want to drive the browser directly without going through the MCP
tool layer.

### 1.11 Wire inbound JWT verification (optional)

Vertex Agent Runtime verifies inbound JWTs at the edge if you've
configured an OIDC authorizer in front of it. You only need to wire
verification inside the agent when you:

- Run the agent outside Vertex Agent Runtime (Cloud Run with a
  custom front, GKE, your own infra) and want the same auth model.
- Want defense-in-depth: re-verify inside the agent even though the
  edge already did.

EAP-Core's `InboundJwtVerifier` works against any OIDC IdP. For
Google-issued tokens, point it at Google's discovery URL:

```python
from eap_core.integrations.agentcore import InboundJwtVerifier, jwt_dependency
from fastapi import FastAPI, Depends

verifier = InboundJwtVerifier(
    discovery_url="https://accounts.google.com/.well-known/openid-configuration",
    issuer="https://accounts.google.com",
    allowed_audiences=["my-bank-agent.example.com"],
    # allowed_clients=["service-account-numeric-id"],   # tighten further
)

app = FastAPI()

@app.post("/invocations")
async def invocations(payload: dict, claims: dict = Depends(jwt_dependency(verifier))):
    # claims contains validated JWT contents (sub, aud, iss, iat, exp, ...)
    ...
```

(The verifier lives under `integrations.agentcore` for historical
reasons — it works against any OIDC IdP including Google's, and is
not AWS-specific.)

The verifier caches JWKS (10-minute default) and validates audience,
scope, and client id. Failures raise PyJWT exceptions with rich
detail.

### 1.12 Connect to Vertex Agent Gateway (outbound MCP calls)

Vertex Agent Gateway exposes APIs and Cloud Run services as MCP
tools. From the agent's side, the gateway is an MCP-HTTP endpoint
that speaks JSON-RPC 2.0. EAP-Core has a thin client:

```python
from eap_core.integrations.vertex import VertexGatewayClient

gw = VertexGatewayClient(
    gateway_url="https://my-gw.example.com/mcp",
    identity=identity,                  # the VertexAgentIdentityToken from step 1.6
    audience="https://my-gw.example.com",
    scope="tools.invoke",
)

# Pull the remote tools.
specs = await gw.list_tools()
result = await gw.invoke("remote_tool", {"arg": "value"})
```

To register every remote tool as a local middleware-traversing
proxy, the same `add_gateway_to_registry` helper used for AgentCore
works for Vertex via duck typing — both gateway clients expose
identical `invoke(name, args)` signatures:

```python
from eap_core.integrations.agentcore import add_gateway_to_registry
from eap_core.mcp import default_registry

add_gateway_to_registry(default_registry(), gw, specs)
```

After this, `client.invoke_tool("remote_tool", {...})` dispatches
through your middleware chain **locally** (sanitize / PII / policy /
observability) and then forwards to the Vertex gateway. The agent
code that calls these proxies is identical to the code that calls
local tools.

### 1.13 Publish your tools to Gateway (inbound)

To make **your** tools callable by other agents through the gateway,
export them as OpenAPI:

```bash
uv run eap publish-to-gateway \
    --entry agent.py \
    --title "my-bank-agent tools" \
    --server-url https://my-bank-agent.example.com
```

This generates `dist/gateway/openapi.json` (every `@mcp_tool` becomes
a `POST /tools/<name>` operation with the input JSON Schema as the
request body) plus a `README.md`. Upload the OpenAPI to Vertex Agent
Gateway as a target via the console or `gcloud beta ai agents
gateways targets create`.

### 1.14 Publish to Vertex Agent Registry

Vertex Agent Registry is the org-wide catalog of agents, tools, MCP
servers, and skills. Publish your A2A AgentCard:

```python
from eap_core import build_card
from eap_core.integrations.vertex import VertexAgentRegistry
from eap_core.mcp import default_registry

card = build_card(
    name="my-bank-agent",
    description="Bank account assistant.",
    skills_from=default_registry(),
)

registry = VertexAgentRegistry(
    project_id="my-gcp-project",
    location="us-central1",
    registry_id="bank-platform",
)
record_id = await registry.publish({
    "name": card.name,
    "record_type": "AGENT",
    "description": card.description,
    "metadata": card.model_dump(),
})

# Discover others:
hits = await registry.search("retrieval", max_results=10)
```

`skills_from=default_registry()` reads the live tool registry, so the
advertised AgentCard skills can never drift from the agent's actual
tools. `VertexAgentRegistry` satisfies the `AgentRegistry` Protocol —
same shape as `InMemoryAgentRegistry` and AgentCore's
`RegistryClient`, so the calling code stays portable.

> **Note:** `VertexAgentRegistry.publish(record)` requires a
> top-level `name` field — the SDK validates this *before* the env
> flag check, so config bugs surface even without
> `EAP_ENABLE_REAL_RUNTIMES`. This is intentional.

### 1.15 Add AP2 payments

If your agent calls APIs that respond with HTTP 402, EAP-Core's
AP2-backed `PaymentClient` handles the sign-and-retry. AP2 is
Google's standardized agent-payments scheme — conceptually parallel
to AWS's x402:

```python
from eap_core.integrations.vertex import AP2PaymentClient
from eap_core.payments import PaymentRequired

pay = AP2PaymentClient(
    wallet_provider_id="my-cdp-wallet",
    project_id="my-gcp-project",
    location="us-central1",
    max_spend_cents=100,          # $1.00 budget
    session_ttl_seconds=3600,
)
await pay.start_session()

try:
    result = await tool_that_might_require_payment()
except PaymentRequired as pr:
    receipt = await pay.authorize(pr)
    # retry the original call with X-Payment-Receipt header
```

`AP2PaymentClient.authorize` checks `can_afford`, signs via the
configured wallet, deducts from the session budget, and returns the
cryptographic receipt. Over budget → `RuntimeError`. Over TTL →
session expires.

The `PaymentRequired` exception is the same class used by AgentCore's
x402 integration, and the `AP2PaymentClient` shares method shape
with `agentcore.PaymentClient` — the calling code is identical
regardless of cloud.

### 1.16 Add Vertex Gen AI Evaluations

Vertex Gen AI Eval Service runs LLM-as-judge scoring against your
trajectories. Plug it into the eval framework:

```python
from eap_core.eval import EvalRunner, FaithfulnessScorer
from eap_core.integrations.vertex import VertexEvalScorer

runner = EvalRunner(
    scorers=[
        FaithfulnessScorer(),                     # local LLM judge
        VertexEvalScorer(                          # Vertex Gen AI Eval-hosted
            project_id="my-gcp-project",
            location="us-central1",
            metric="faithfulness",
        ),
    ],
)
report = await runner.run(trajectories)
```

`VertexEvalScorer` returns the same `FaithfulnessResult` shape as
local scorers and AgentCore's `AgentCoreEvalScorer`, so reports stay
homogeneous and the rest of your eval pipeline doesn't need to know
which scorer is which.

Built-in `metric` values include `faithfulness`, `groundedness`,
`coherence`, `helpfulness`. See the
[Vertex Gen AI Eval docs](https://cloud.google.com/vertex-ai/generative-ai/docs/models/evaluation-overview)
for the full catalog.

For batch jobs, convert Trajectory rows for upload:

```python
from eap_core.integrations.vertex import to_vertex_eval_dataset

rows = to_vertex_eval_dataset(trajectories)
# rows is a list of dicts: trace_id / prompt / response / context / steps
# Upload to GCS or feed directly to the Vertex Eval API.
```

Run the runner in CI with `eap eval --dataset tests/golden_set.json
--threshold 0.7`. Non-zero exit code on regression → failed build.

### 1.17 Package and deploy

`eap deploy --runtime vertex-agent-engine` refuses to scaffold an
unauthenticated handler. Pass the OIDC details so the generated
`handler.py` wires `InboundJwtVerifier` + `jwt_dependency` into
`POST /invocations`:

```bash
uv run eap deploy --runtime vertex-agent-engine \
    --service my-bank-agent \
    --region us-central1 \
    --auth-discovery-url https://accounts.google.com/.well-known/openid-configuration \
    --auth-issuer        https://accounts.google.com \
    --auth-audience      my-bank-agent
```

For local smoke testing only, you may pass `--allow-unauthenticated` to
skip auth wiring; the CLI then emits a loud warning and the generated
`handler.py` carries a `WARNING` comment at the top — never use this
mode in production.

This produces `dist/vertex-agent-engine/`:

```
dist/vertex-agent-engine/
├── Dockerfile     # linux/amd64 base, PORT env, EXPOSE 8080
├── handler.py     # FastAPI: POST /invocations (with jwt_dependency) + GET /health
├── README.md      # Artifact Registry push + Vertex registration steps
└── <your project files>
```

The handler imports your `agent.py:answer` entry function, binds to
`0.0.0.0:${PORT:-8080}` per Cloud Run convention, and serves the
standard `{"response": ..., "status": "success"}` shape.

**Review `dist/vertex-agent-engine/.eap-manifest.txt` before pushing the**
image — it lists every file staged for deployment. The packager already
excludes `.env`, `.git`, `*.pem`, `*.key`, `credentials*.json`,
`*.tfstate`, `.aws/`, `.ssh/`, and SSH private keys by default; add a
project-level `.eapignore` (one glob per line, `#` comments allowed) to
exclude additional files such as internal docs or scratch state.

By default `eap deploy` only packages — to actually `docker build`,
set `EAP_ENABLE_REAL_DEPLOY=1`. Then follow
`dist/vertex-agent-engine/README.md` for the Artifact Registry push +
Vertex Agent Engine registration:

```bash
export EAP_ENABLE_REAL_DEPLOY=1
export GOOGLE_CLOUD_PROJECT=my-gcp-project
uv run eap deploy --runtime vertex-agent-engine --service my-bank-agent --region us-central1 \
    --auth-discovery-url https://accounts.google.com/.well-known/openid-configuration \
    --auth-issuer        https://accounts.google.com \
    --auth-audience      my-bank-agent
# → Built image: <image>:<tag>
# → Push to Artifact Registry and register with Vertex Agent Engine — see dist/vertex-agent-engine/README.md
```

### 1.18 Smoke-test the deployed agent

```bash
# After the Vertex Agent Runtime is registered, exercise it through
# the Vertex endpoint. The handler matches Cloud Run conventions:
curl -X POST https://your-runtime-url/invocations \
     -H "Authorization: Bearer $(gcloud auth print-identity-token)" \
     -H "Content-Type: application/json" \
     -d '{"prompt": "Hello"}'

# Healthcheck:
curl https://your-runtime-url/health
# → {"status": "OK"}
```

Watch the Cloud Trace dashboard — every request is a span with the
standard `gen_ai.*` attributes plus your own `policy.*`, `pii.*`,
and tenant tags, all tagged with the `gcp.project_id` resource attr.

**That's the full loop.** Local dev → identity → memory →
sandboxes → gateway → registry → AP2 payments → eval → deployed
and observable.

---

## Part 2 — Per-task reference

Look up "how do I wire X" without re-reading the tutorial. Every
snippet assumes you've installed `eap-core[gcp]` and have GCP
credentials available via the standard chain (ADC / service account
JSON / workload identity).

### 2.1 Authentication and credentials

**Local dev** — Application Default Credentials:

```bash
gcloud auth application-default login
```

`VertexAgentIdentityToken()` with no args picks up ADC and acquires
tokens with the `cloud-platform` scope.

**Production** — workload identity:

- **Cloud Run / Cloud Functions / GKE** — attach a service account
  to the workload, set `EAP_ENABLE_REAL_RUNTIMES=1`, and the SDK
  picks up credentials automatically.
- **Outside GCP** — use workload identity federation (e.g. an OIDC
  provider issues a token, GCP exchanges it for a service account
  access token). Same code; configure the federation pool in
  IAM and the rest is environment-driven.
- **Service account JSON (last resort)** — set
  `GOOGLE_APPLICATION_CREDENTIALS=/path/to/key.json`. Rotate keys
  and prefer workload identity wherever possible.

**Custom scopes** — pass `scopes=` if `cloud-platform` is too broad:

```python
identity = VertexAgentIdentityToken(
    scopes=[
        "https://www.googleapis.com/auth/aiplatform.endpoints.predict",
        "https://www.googleapis.com/auth/bigquery.readonly",
    ],
)
```

`VertexMemoryBankStore`, `VertexCodeSandbox`, `VertexBrowserSandbox`,
`VertexAgentRegistry`, `AP2PaymentClient`, and `VertexEvalScorer` use
the standard Google client library auth chain (no EAP-Core-specific
config).

### 2.2 Memory: short-term vs long-term

`VertexMemoryBankStore` doesn't distinguish — it's a key/value store
scoped by `session_id`. Convention:

- **Short-term** — keys like `current_intent`, `last_balance`,
  `pending_transfer_id`. Reset per session.
- **Long-term** — keys like `pref_lang`, `verified_phone_e164`. Use a
  stable `session_id` like the user's id, not a request id.

Use the same store for both; pick the `session_id` to control scope.

### 2.3 Code execution (Vertex Agent Sandbox)

Two ways to invoke:

**MCP tool path** (LLM-driven, traverses middleware):

```python
register_code_sandbox_tools(default_registry(), project_id="...")
# LLM can now call execute_python with generated code.
```

**Direct path** (your code, not LLM-driven) — use the `CodeSandbox`
Protocol:

```python
from eap_core.integrations.vertex import VertexCodeSandbox

sb = VertexCodeSandbox(project_id="my-gcp-project")
result = await sb.execute("python", "print(2+2)")
# SandboxResult: stdout="4\n", stderr="", exit_code=0, artifacts={}
```

The `result.artifacts` dict carries any GCS artifact URIs the
sandbox produced.

### 2.4 Browser automation

Five tools after `register_browser_sandbox_tools(...)`. Each is one
HTTP call to the Vertex Browser Sandbox session:

| Tool | Args | Returns |
|---|---|---|
| `browser_navigate` | `url: str` | `{...}` |
| `browser_click` | `selector: str` | `{...}` |
| `browser_fill` | `selector: str, value: str` | `{...}` |
| `browser_extract_text` | `selector: str = "body"` | `str` |
| `browser_screenshot` | — | `{"png_base64": ...}` |

Use a stable `session_id` if you want the browser session to persist
across calls. Pass `session_id=` to `register_browser_sandbox_tools(...)`.

### 2.5 Inbound JWT verification

Default usage (inside Vertex Agent Runtime with edge auth): skip.

Defense-in-depth or outside-Runtime usage:

```python
from eap_core.integrations.agentcore import InboundJwtVerifier, jwt_dependency
from fastapi import Depends

verifier = InboundJwtVerifier(
    discovery_url="https://accounts.google.com/.well-known/openid-configuration",
    issuer="https://accounts.google.com",
    allowed_audiences=["my-agent.example.com"],
    allowed_clients=["service-account-numeric-id"],   # optional
    jwks_cache_ttl_seconds=600,
    clock_skew_seconds=30,
)

@app.post("/invocations")
async def handle(claims: dict = Depends(jwt_dependency(verifier))):
    user_id = claims["sub"]
    ...
```

The verifier validates RS256/RS384/RS512 signatures, audience,
scope, and (if configured) client id. Rejected tokens raise PyJWT
exceptions that FastAPI maps to 401.

### 2.6 Outbound Gateway calls

```python
gw = VertexGatewayClient(
    gateway_url="https://my-gw.example.com/mcp",
    identity=identity,                      # VertexAgentIdentityToken
    audience="https://my-gw.example.com",   # defaults to gateway_url
    scope="tools.invoke",
    timeout_seconds=30.0,                   # default
)

tools = await gw.list_tools()
result = await gw.invoke("remote_tool", {"arg": "value"})

# Always close:
await gw.aclose()
```

Use `add_gateway_to_registry(default_registry(), gw, tools)` (from
`eap_core.integrations.agentcore`) to register remote tools as local
proxies — both gateway clients have the same shape, so the helper
works against either cloud. After that, your agent code treats them
like any other tool — the middleware chain runs locally before each
forward.

For non-Bearer auth, pass `auth=` (an httpx auth object) instead of
`identity=`.

### 2.7 Publishing tools to Gateway

```bash
uv run eap publish-to-gateway \
    --entry agent.py \
    --title "my-agent tools" \
    --server-url https://my-agent.example.com \
    [--dry-run]
```

Produces `dist/gateway/openapi.json` + a `README.md`. Upload the
OpenAPI to Vertex Agent Gateway as a target. Each `@mcp_tool` becomes
a `POST /tools/<name>` operation with the input JSON Schema as the
request body.

To programmatically construct the OpenAPI:

```python
from eap_core.integrations.agentcore import export_tools_as_openapi
from eap_core.mcp import default_registry

spec = export_tools_as_openapi(
    default_registry(),
    title="my-agent tools",
    version="1.0.0",
    server_url="https://my-agent.example.com",
)
```

(The exporter lives under `integrations.agentcore` for historical
reasons; it's vendor-neutral and works against either gateway.)

### 2.8 Registry — discovery and publishing

```python
from eap_core.integrations.vertex import VertexAgentRegistry

registry = VertexAgentRegistry(
    project_id="my-gcp-project",
    location="us-central1",
    registry_id="bank-platform",
)

# Publish your agent's card:
record_id = await registry.publish({
    "name": "my-bank-agent",
    "record_type": "AGENT",
    "description": "Bank account assistant.",
    "metadata": card.model_dump(),
})

# Publish a standalone MCP server:
await registry.publish({
    "name": "doc-search-mcp",
    "record_type": "MCP_SERVER",
    "description": "Internal documentation search via MCP.",
    "metadata": {"mcp_endpoint": "stdio://internal/doc-search"},
})

# Discover others:
hits = await registry.search("payments", max_results=10)
record = await registry.get("doc-search-mcp")
all_servers = await registry.list_records(record_type="MCP_SERVER")
```

The Protocol-level `publish(record)` method is generic — `record` is
a dict. The required field is `name`; everything else is
backend-defined. Pass `record_type` to disambiguate (`AGENT`,
`MCP_SERVER`, `TOOL`, `SKILL`, or your own).

### 2.9 Payments — AP2 (Agent Payment Protocol)

The pattern: open a session with a budget, catch `PaymentRequired`
from tools that hit 402, sign and retry.

```python
from eap_core.integrations.vertex import AP2PaymentClient
from eap_core.payments import PaymentRequired

pay = AP2PaymentClient(
    wallet_provider_id="my-cdp-wallet",
    project_id="my-gcp-project",
    location="us-central1",
    max_spend_cents=500,
    currency="USD",
    session_ttl_seconds=3600,
)
await pay.start_session()

# Budget bookkeeping is available before any call:
if pay.can_afford(amount_cents=50):
    ...

# After payments are made:
pay.spent_cents       # e.g. 50
pay.remaining_cents   # e.g. 450
```

`pay.authorize(req)` returns the signed receipt. The caller re-issues
the original HTTP request with an `X-Payment-Receipt` header carrying
that receipt.

### 2.10 Evaluations

Two flows:

**In-flow scoring** — drop `VertexEvalScorer` into `EvalRunner`:

```python
runner = EvalRunner(scorers=[
    VertexEvalScorer(
        project_id="my-gcp-project",
        location="us-central1",
        metric="groundedness",
        scorer_name="groundedness",     # optional; overrides default "vertex_eval"
    ),
])
report = await runner.run(trajectories)
```

**Export-and-upload** — convert Trajectory rows for Vertex Gen AI
Eval batch jobs:

```python
from eap_core.integrations.vertex import to_vertex_eval_dataset

rows = to_vertex_eval_dataset(trajectories)
# rows is a list of dicts: trace_id / prompt / response / context / steps
# Upload to GCS or feed directly to Vertex Eval API calls.
```

Built-in metrics include `faithfulness`, `groundedness`, `coherence`,
`helpfulness`, plus model-specific evaluators.

### 2.11 Observability — what shows up in Cloud Trace

After `configure_for_vertex_observability()` (or when running inside
Vertex Agent Runtime, which auto-injects OTLP), every request is a
span with:

- `gen_ai.request.model` — the model id from `RuntimeConfig`.
- `gen_ai.operation.name` — e.g. `chat`, `tool.invoke`.
- `gen_ai.usage.input_tokens` / `gen_ai.usage.output_tokens` — usage.
- `gen_ai.response.finish_reason` — `stop`, `length`, `tool_call`, etc.
- `gen_ai.error.type` — present on errors.
- `service.name`, `gcp.project_id` — resource attributes from
  `configure_for_vertex_observability()`.

Plus EAP-Core-specific attributes:

- `policy.matched_rule` — which rule allowed the call.
- `pii.masked_count` — tokens that went into the per-request vault.
- `tool.name` — for `tool.invoke` spans.

Custom middleware can add namespaced attrs via `ctx.metadata["myns.key"] = ...`
— they'll show up automatically.

In Cloud Trace, filter by `service.name="my-bank-agent"` to find
your runs.

### 2.12 Deploy

```bash
# Default: package only (no docker build).
uv run eap deploy --runtime vertex-agent-engine --service my-agent

# With Docker build (still local, no push):
EAP_ENABLE_REAL_DEPLOY=1 GOOGLE_CLOUD_PROJECT=my-gcp-project \
  uv run eap deploy --runtime vertex-agent-engine --service my-agent --region us-central1
# → Built image: <local-image>:<tag>

# After build, push to Artifact Registry and register manually
# (see dist/vertex-agent-engine/README.md):
gcloud auth configure-docker us-central1-docker.pkg.dev
docker tag my-agent:latest \
  us-central1-docker.pkg.dev/my-gcp-project/agents/my-agent:v1
docker push \
  us-central1-docker.pkg.dev/my-gcp-project/agents/my-agent:v1
# Then register with Vertex Agent Engine:
gcloud beta ai agents create my-agent \
  --image us-central1-docker.pkg.dev/my-gcp-project/agents/my-agent:v1 \
  --region us-central1
```

Inside the Vertex Agent Runtime, the handler
`dist/vertex-agent-engine/handler.py` exposes:

- `POST /invocations` — accepts `{"prompt": "..."}`, calls your entry
  function, returns `{"response": "...", "status": "success"}`.
- `GET /health` — Cloud Run-style healthcheck.

Both honor the `PORT` env var (defaults to 8080) per Cloud Run
convention.

---

## Part 3 — Production checklist

Before flipping live traffic on:

- [ ] `EAP_ENABLE_REAL_RUNTIMES=1` set in the Vertex Agent Runtime env.
- [ ] `GOOGLE_CLOUD_PROJECT` env var matches the project the
      Memory Bank / Registry / Sandbox live in.
- [ ] Region matches across `configure_for_vertex_observability()`,
      `VertexMemoryBankStore`, `VertexAgentRegistry`,
      `AP2PaymentClient`, and `VertexEvalScorer`. Cross-region calls
      add latency and may break IAM bindings.
- [ ] The runtime's service account has the right IAM:
      `roles/aiplatform.user` (memory + eval + sandboxes),
      `roles/cloudtrace.agent` (observability), plus any
      data-plane roles your tools need.
- [ ] ADC works in your local dev shell (`gcloud auth
      application-default print-access-token` returns a token).
- [ ] `configs/policy.json` tightened — start with explicit `permit`
      rules per `(action, resource, role)` combination. Default-deny
      is the safe baseline.
- [ ] If hitting regulated data, install `[pii]` and use
      `PiiMaskingMiddleware(engine="presidio")`. The regex tokenizer
      is a starter, not a finish line.
- [ ] `VertexMemoryBankStore.memory_bank_id` is the right one —
      accidentally pointing at staging memory from prod is a quietly
      catastrophic bug.
- [ ] If you're publishing tools to Gateway, the OpenAPI's
      `server_url` is the production hostname, not localhost.
- [ ] If using `InboundJwtVerifier` for defense-in-depth,
      `allowed_audiences` is set (otherwise audience validation is a
      no-op).
- [ ] If using `AP2PaymentClient`, `max_spend_cents` is the budget
      you actually want — not the default 100¢.
- [ ] `eap eval` runs in CI against `tests/golden_set.json` with
      `--threshold` set high enough to catch real regressions.
- [ ] Cloud Trace search returns hits when you exercise the runtime
      URL. If not, OTLP env vars didn't propagate.

---

## Troubleshooting

**`NotImplementedError: Vertex adapter requires the [gcp] extra and
Google Cloud credentials. Set EAP_ENABLE_REAL_RUNTIMES=1 once
configured.`**

You forgot the env flag. The flag is intentional — it prevents tests
from accidentally hitting GCP. Set `EAP_ENABLE_REAL_RUNTIMES=1` in
your runtime env (not in pytest).

**`ImportError: ... requires the [gcp] extra: pip install eap-core[gcp]`**

The `[gcp]` extra isn't installed. With uv:
`uv sync --all-packages --group dev --extra gcp`.

**`google.auth.exceptions.DefaultCredentialsError: Could not
automatically determine credentials.`**

ADC isn't set up. Locally, run `gcloud auth application-default
login`. In production, attach a service account to the workload (or
set `GOOGLE_APPLICATION_CREDENTIALS=/path/to/key.json`).

**`google.api_core.exceptions.PermissionDenied: 403`**

The credentials are valid but lack IAM permission. Check the service
account has `roles/aiplatform.user` for the project. For
Memory Bank, you may also need `roles/aiplatform.memoryBankUser`
(or whichever specific role the API doc requires).

**`MCPError: gateway returned HTTP 403`**

The bearer token is missing or invalid. Check that your
`VertexAgentIdentityToken` was constructed with the right `scopes`
for the gateway's audience. Token TTL is short by design — Google's
auth library auto-refreshes; if you see persistent 403, the scope is
wrong, not the token.

**`jwt.InvalidTokenError: no JWKS key matches kid=...`**

The token's `kid` header doesn't match any key in the IdP's JWKS.
Usually this means token-vs-IdP mismatch (a token from a different
project hitting a verifier configured for accounts.google.com).
Double-check `discovery_url`.

**Cloud Trace shows no traces.**

The OTLP env vars didn't propagate. Inside Vertex Agent Runtime,
the platform should auto-inject — if not, call
`configure_for_vertex_observability(...)` explicitly in `agent.py`.
Outside Vertex Runtime, set `OTEL_EXPORTER_OTLP_ENDPOINT` yourself
and pass it to the helper.

**`RuntimeError: payment of N USD would exceed remaining budget`**

You hit the `max_spend_cents` ceiling. Either raise the ceiling at
`AP2PaymentClient` construction time, or surface the payment-required
error to the user and let them top up.

**Tools registered but the LLM doesn't call them.**

Check the AgentCard — `build_card(skills_from=default_registry())`
reads the live registry, so if a tool registered after the card was
built it won't be advertised. Rebuild and re-publish the card after
adding tools.

---

## What's next

- For a project where you start from Vertex and want to also run on
  AWS without rewriting business logic, see
  [`docs/user-guide-aws-agentcore.md`](user-guide-aws-agentcore.md).
  The Protocol seams (`MemoryStore`, `CodeSandbox`, `BrowserSandbox`,
  `AgentRegistry`, `PaymentBackend`) mean swapping clouds is a
  constructor change.
- For the full Vertex service-by-service mapping, see
  [`docs/integrations/gcp-vertex-agent-engine.md`](integrations/gcp-vertex-agent-engine.md).
- For extending the SDK itself (adding middleware, runtime
  adapters, new cloud integrations), see
  [`docs/developer-guide.md`](developer-guide.md).
