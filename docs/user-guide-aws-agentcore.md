# EAP-Core User Guide — AWS Bedrock AgentCore

This guide is for engineers **building** an agent on AWS Bedrock
AgentCore using EAP-Core. It assumes you're new to both — every step
spells out the exact command, the exact code, and what to look for if
it doesn't work.

If you're extending the SDK rather than building on it, read
[`docs/developer-guide.md`](developer-guide.md) instead. If you only
want positioning and the service-by-service map, read
[`docs/integrations/aws-bedrock-agentcore.md`](integrations/aws-bedrock-agentcore.md).

**Two parts:**

- **Part 1 — Tutorial.** End-to-end walkthrough from `eap init` to a
  deployed AgentCore Runtime agent that uses Memory, Code
  Interpreter, Browser, Gateway, Registry, Payments, and
  Evaluations.
- **Part 2 — Per-task reference.** Look up "how do I wire X" without
  re-reading the tutorial.
- **Part 3 — Production checklist.** What to verify before turning
  on live traffic.

> **Working reference project:** every snippet below is wired up
> end-to-end at
> [`examples/agentcore-bank-agent/`](../examples/agentcore-bank-agent/).
> It runs locally with no AWS credentials (stubs swap in via
> env-flag gating); set `EAP_ENABLE_REAL_RUNTIMES=1` to graduate.
> Read the guide and the example side by side.

---

## Part 1 — Tutorial: zero to deployed AgentCore agent

### 1.1 Prerequisites

- **Python 3.11+** (3.12 recommended).
- **uv** — `curl -LsSf https://astral.sh/uv/install.sh | sh`. The
  workspace assumes uv.
- **AWS account** with Bedrock AgentCore enabled in your region
  (default: `us-east-1`). You'll need:
  - An IAM principal (role for production, user for local dev) with
    Bedrock + Bedrock AgentCore access.
  - An ECR repository for the runtime image.
  - An AgentCore Workload Identity (set up in the AgentCore console
    under **Identity → Workload identities**).
- **Docker** (only for deploy). Local development needs no Docker.

You do not need any of the AWS pieces for the first few steps. We
build and run the agent locally first.

### 1.2 Install EAP-Core

From the SDK repo:

```bash
git clone https://github.com/narisun/ai-eap-sdk.git
cd ai-eap-sdk
uv sync --all-packages --group dev --extra aws --extra otel --extra mcp
```

The `[aws]` extra pulls `boto3`. The `[otel]` extra pulls the OTel
SDK + OTLP exporter (AgentCore Observability ingests OTLP). The
`[mcp]` extra pulls the official MCP SDK if you want to expose tools
as an MCP stdio server.

For a downstream project, depend on EAP-Core directly:

```toml
# pyproject.toml
[project]
dependencies = [
    "eap-core[aws,otel,mcp] @ git+https://github.com/narisun/ai-eap-sdk.git@v0.3.1#subdirectory=packages/eap-core",
    "eap-cli @ git+https://github.com/narisun/ai-eap-sdk.git@v0.3.1#subdirectory=packages/eap-cli",
]
```

### 1.3 Scaffold a project

```bash
uv run eap init my-bank-agent --runtime local
cd my-bank-agent
```

We pick `--runtime local` for now so the agent runs without AWS
credentials. We'll flip it to `bedrock` later. The scaffold produces:

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

That's the full loop with no AWS account: the middleware chain runs
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
- Over AgentCore Gateway once you publish (Step 1.13).

### 1.6 Wire AgentCore Identity (workload identity → tool tokens)

The agent needs to authenticate itself to downstream APIs.
AgentCore Identity issues OAuth tokens for workload identities via
**RFC 8693 token exchange**. EAP-Core has a one-liner for this:

```python
# add to agent.py
from eap_core.identity import NonHumanIdentity, LocalIdPStub
from eap_core.integrations.agentcore import OIDCTokenExchange

# 1. NonHumanIdentity owns the workload's signing key. Its IdP
#    issues short-lived assertions on each `get_token` call.
nhi = NonHumanIdentity(
    client_id="my-bank-agent",
    idp=LocalIdPStub(for_testing=True),  # swap to real IdP signer in production
    default_audience="https://api.bank.example",
)

# 2. OIDCTokenExchange swaps the assertion for a tool-callable
#    Bearer token via AgentCore Identity (RFC 8693). Hold the
#    exchange separately and round-trip when you need a downstream
#    token:
exchange = OIDCTokenExchange.from_agentcore(
    region="us-east-1",
    workload_identity_id="my-bank-agent",  # or set AGENTCORE_WORKLOAD_IDENTITY_ID env var
)
```

`OIDCTokenExchange.from_agentcore()` just fills in the AgentCore
Identity token endpoint URL for the region. The 5-second TTL buffer
on `NonHumanIdentity._cache` is unchanged. For tool dispatchers and
gateway clients that take an `identity=` argument, pass the NHI
directly — they call `identity.get_token(...)` for the assertion
and (when configured) use `OIDCTokenExchange.exchange(...)` to swap
it. For your own code that needs a tool-callable Bearer:

```python
assertion = nhi.get_token(audience="https://api.bank.example", scope="read")
bearer = await exchange.exchange(
    subject_token=assertion,
    audience="https://api.bank.example",
    scope="read",
)
```

For dev, `LocalIdPStub` signs assertions locally. For production,
replace it with a real signer — see [§2.1](#21-authentication-and-credentials)
below.

### 1.7 Wire AgentCore Observability (OTel → CloudWatch)

When your agent runs **inside** AgentCore Runtime, AgentCore
auto-injects the OTLP env vars and you don't need to call anything.
For local dev (or any other deploy target), wire it explicitly:

```python
# add to agent.py — call once at startup
from eap_core.integrations.agentcore import configure_for_agentcore

configure_for_agentcore(
    service_name="my-bank-agent",
    # endpoint=...  # optional; defaults to OTEL_EXPORTER_OTLP_ENDPOINT
)
```

This wires the OTel SDK to a CloudWatch-compatible OTLP endpoint and
sets the `service.name` resource attribute. Returns `False` if the
`[otel]` extra isn't installed (the middleware still writes
`gen_ai.*` attributes to `ctx.metadata`, so audit and trajectory
recording keep working).

### 1.8 Wire AgentCore Memory

AgentCore Memory persists per-session short-term memory and long-term
cross-session facts. Plug it in via the `MemoryStore` Protocol:

```python
from eap_core.integrations.agentcore import AgentCoreMemoryStore

memory = AgentCoreMemoryStore(memory_id="my-bank-memory", region="us-east-1")

# In your tool / handler:
await memory.remember(session_id="user-123", key="last_balance", value="$1,234.56")
balance = await memory.recall(session_id="user-123", key="last_balance")
```

Construction is cheap — no boto3 import, no network call. All live
calls are gated by `EAP_ENABLE_REAL_RUNTIMES=1`; until you set it,
methods raise `NotImplementedError` with a clear "wire credentials"
message. This keeps unit tests deterministic.

The class satisfies `eap_core.memory.MemoryStore`. Drop it anywhere a
`MemoryStore` is expected (`Context.memory_store`, your own
middleware, etc.).

### 1.9 Add Code Interpreter tools

AgentCore Code Interpreter is a sandboxed Python/JS/TS execution
environment. Register the three default tools on your registry:

```python
from eap_core.mcp import default_registry
from eap_core.integrations.agentcore import register_code_interpreter_tools

register_code_interpreter_tools(default_registry(), region="us-east-1")
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

### 1.10 Add Browser tools

Same pattern for AgentCore Browser:

```python
from eap_core.integrations.agentcore import register_browser_tools

register_browser_tools(default_registry(), region="us-east-1")
```

Registers five MCP tools: `browser_navigate`, `browser_click`,
`browser_fill`, `browser_extract_text`, `browser_screenshot`. Each
traverses the chain — policy can deny `browser_navigate` to specific
hostnames, observability records every action as a span.

### 1.11 Wire the Inbound JWT Verifier

This step is **optional** when the agent runs inside AgentCore
Runtime (AgentCore's configured inbound authorizer already verified
the token before the request reached you). Wire it when you:

- Run the agent outside AgentCore Runtime (Lambda, Cloud Run, your own infra).
- Want defense-in-depth: re-verify inside the agent even though
  AgentCore already did.

```python
from eap_core.integrations.agentcore import InboundJwtVerifier, jwt_dependency
from fastapi import FastAPI, Depends

verifier = InboundJwtVerifier(
    discovery_url="https://agentcore-identity.us-east-1.amazonaws.com/.well-known/openid-configuration",
    issuer="https://agentcore-identity.us-east-1.amazonaws.com",
    allowed_audiences=["my-bank-agent"],
    allowed_scopes=["agent:invoke"],
)

app = FastAPI()

@app.post("/invocations")
async def invocations(payload: dict, claims: dict = Depends(jwt_dependency(verifier))):
    # claims contains validated JWT contents (sub, client_id, scope, ...)
    ...
```

The verifier caches JWKS (10-minute default) and validates audience,
scope, and client id. Failures raise PyJWT exceptions with rich
detail.

### 1.12 Connect to AgentCore Gateway (outbound MCP calls)

AgentCore Gateway exposes APIs and Lambdas as MCP tools. From the
agent's side, the gateway is an MCP-HTTP endpoint that speaks
JSON-RPC 2.0. EAP-Core has a thin client:

```python
from eap_core.integrations.agentcore import GatewayClient, add_gateway_to_registry
from eap_core.mcp import default_registry

gw = GatewayClient(
    gateway_url="https://my-gw.bedrock-agentcore.us-east-1.amazonaws.com/mcp",
    identity=nhi,                       # the NonHumanIdentity from step 1.6
    audience="my-gateway",
    scope="tools:invoke",
)

# Pull the remote tools and register them as local proxies.
specs = await gw.list_tools()
add_gateway_to_registry(default_registry(), gw, specs)
```

After this, `client.invoke_tool("remote_tool", {...})` dispatches
through your middleware chain **locally** (sanitize / PII / policy /
observability) and then forwards to the gateway. The agent code that
calls these proxies is identical to the code that calls local tools.

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
request body) plus a `README.md` walking through the AWS console
upload + Gateway-target registration.

### 1.14 Publish to AgentCore Agent Registry

The Agent Registry is the org-wide catalog of agents, tools, MCP
servers, and skills. Publish your A2A AgentCard:

```python
from eap_core import build_card
from eap_core.integrations.agentcore import RegistryClient
from eap_core.mcp import default_registry

card = build_card(
    name="my-bank-agent",
    description="Bank account assistant.",
    skills_from=default_registry(),
)

registry = RegistryClient(registry_name="bank-platform", region="us-east-1")
record_id = await registry.publish_agent_card(card)

# Discover others:
hits = await registry.search("retrieval", max_results=10)
```

`skills_from=default_registry()` reads the live tool registry, so the
advertised AgentCard skills can never drift from the agent's actual
tools. `RegistryClient` satisfies the `AgentRegistry` Protocol — same
shape as `InMemoryAgentRegistry` and `VertexAgentRegistry`, so the
calling code stays portable.

### 1.15 Add x402 payments

If your agent calls APIs that respond with HTTP 402 (the x402
microtransactions standard), EAP-Core handles the sign-and-retry:

```python
from eap_core.integrations.agentcore import PaymentClient
from eap_core.payments import PaymentRequired

pay = PaymentClient(
    wallet_provider_id="my-cdp-wallet",
    max_spend_cents=100,          # $1.00 budget
    session_ttl_seconds=3600,
)
await pay.start_session()

try:
    result = await tool_that_might_require_payment()
except PaymentRequired as pr:
    receipt = await pay.authorize_and_retry(pr)
    # retry the original call with X-Payment-Receipt header
```

`PaymentClient.authorize_and_retry` checks `can_afford`, signs via
the configured wallet, deducts from the session budget, and returns
the cryptographic receipt. Over budget → `RuntimeError`. Over TTL →
session expires.

### 1.16 Add AgentCore Evaluations

AgentCore Evaluations runs LLM-as-judge scoring against your
trajectories. Plug it into the eval framework:

```python
from eap_core.eval import EvalRunner, FaithfulnessScorer
from eap_core.integrations.agentcore import AgentCoreEvalScorer

runner = EvalRunner(
    scorers=[
        FaithfulnessScorer(),                              # local LLM judge
        AgentCoreEvalScorer(                                # AgentCore-hosted
            evaluator_arn="arn:aws:bedrock-agentcore:::evaluator/Builtin.Faithfulness",
            region="us-east-1",
        ),
    ],
)
report = await runner.run(trajectories)
```

`AgentCoreEvalScorer` returns the same `FaithfulnessResult` shape as
local scorers, so reports stay homogeneous and the rest of your
eval pipeline doesn't need to know which scorer is which.

Run it in CI with `eap eval --dataset tests/golden_set.json
--threshold 0.7`. Non-zero exit code on regression → failed build.

### 1.17 Package and deploy

`eap deploy --runtime agentcore` refuses to scaffold an unauthenticated
handler. Pass the OIDC details so the generated `handler.py` wires
`InboundJwtVerifier` + `jwt_dependency` into `POST /invocations`:

```bash
uv run eap deploy --runtime agentcore \
    --service my-bank-agent \
    --region us-east-1 \
    --auth-discovery-url https://agentcore-identity.us-east-1.amazonaws.com/.well-known/openid-configuration \
    --auth-issuer        https://agentcore-identity.us-east-1.amazonaws.com \
    --auth-audience      my-bank-agent
```

For local smoke testing only, you may pass `--allow-unauthenticated` to
skip auth wiring; the CLI then emits a loud warning and the generated
`handler.py` carries a `WARNING` comment at the top — never use this
mode in production.

This produces `dist/agentcore/`:

```
dist/agentcore/
├── Dockerfile     # ARM64 base, installs FastAPI + uvicorn + your project
├── handler.py     # POST /invocations (with jwt_dependency) + GET /ping
├── README.md      # ECR push + AgentCore Runtime register steps
└── <your project files>
```

The handler imports your `agent.py:answer` entry function and serves
it on port 8080 per the
[AgentCore HTTP protocol contract](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-http-protocol-contract.html).

**Review `dist/agentcore/.eap-manifest.txt` before pushing the image —**
it lists every file staged for deployment. The packager already excludes
`.env`, `.git`, `*.pem`, `*.key`, `credentials*.json`, `*.tfstate`,
`.aws/`, `.ssh/`, and SSH private keys by default; add a project-level
`.eapignore` (one glob per line, `#` comments allowed) to exclude
additional files such as internal docs or scratch state.

By default `eap deploy` only packages — to actually `docker build`,
set `EAP_ENABLE_REAL_DEPLOY=1`. Then follow `dist/agentcore/README.md`
for the ECR push + AgentCore Runtime registration:

```bash
export EAP_ENABLE_REAL_DEPLOY=1
uv run eap deploy --runtime agentcore --service my-bank-agent \
    --auth-discovery-url https://agentcore-identity.us-east-1.amazonaws.com/.well-known/openid-configuration \
    --auth-issuer        https://agentcore-identity.us-east-1.amazonaws.com \
    --auth-audience      my-bank-agent
# → Built image: <image>:<tag>
# → Push to ECR and register with AgentCore Runtime — see dist/agentcore/README.md
```

### 1.18 Smoke-test the deployed agent

```bash
# After the AgentCore Runtime is registered, exercise it through the
# AgentCore endpoint. The handler matches the AgentCore contract:
curl -X POST https://your-runtime-url/invocations \
     -H "Authorization: Bearer $(your-token)" \
     -H "Content-Type: application/json" \
     -d '{"prompt": "Hello"}'

# Healthcheck:
curl https://your-runtime-url/ping
# → {"status": "Healthy", "time_of_last_update": 1715472000}
```

Watch the CloudWatch Trace dashboard — every request is a span with
the standard `gen_ai.*` attributes plus your own `policy.*`, `pii.*`,
and tenant tags.

**That's the full loop.** Local dev → identity → memory →
sandboxes → gateway → registry → payments → eval → deployed and
observable.

---

## Part 2 — Per-task reference

Look up "how do I wire X" without re-reading the tutorial. Every
snippet assumes you've installed `eap-core[aws]` and have AWS
credentials available via the standard chain (env / `~/.aws/config`
/ IAM role).

### 2.1 Authentication and credentials

**Local dev** — use `LocalIdPStub` for fast iteration:

```python
from eap_core.identity import LocalIdPStub, NonHumanIdentity
from eap_core.integrations.agentcore import OIDCTokenExchange

nhi = NonHumanIdentity(
    client_id="my-agent",
    idp=LocalIdPStub(for_testing=True),
    default_audience="https://api.bank.example",
)
exchange = OIDCTokenExchange.from_agentcore(region="us-east-1")
# Use `nhi` wherever an `identity=` is accepted; round-trip through
# `exchange.exchange(subject_token=nhi.get_token(...), ...)` when
# you need a downstream tool-callable Bearer.
```

**Production** — implement the `IdentityProvider` Protocol against
your real signer (e.g. KMS-held private key, AgentCore Identity-
issued workload identity assertion):

```python
class KMSIdentityProvider:
    def issue(self, *, client_id, audience, scope, roles=None):
        # call KMS to sign a JWT and return it
        return signed_jwt_string
```

Then point the NHI at it. The cache (5-second buffer before expiry)
works the same.

**Boto3 credentials** — `AgentCoreMemoryStore`, `PaymentClient`,
`RegistryClient`, and `AgentCoreEvalScorer` use boto3's default chain
(env vars, `~/.aws/credentials`, IAM role). No EAP-Core-specific
config needed.

### 2.2 Memory: short-term vs long-term

`AgentCoreMemoryStore` doesn't distinguish — it's a key/value store
scoped by `session_id`. Convention:

- **Short-term** — keys like `current_intent`, `last_balance`,
  `pending_transfer_id`. Reset per session.
- **Long-term** — keys like `pref_lang`, `verified_phone_e164`. Use a
  stable `session_id` like the user's id, not a request id.

Use the same store for both; pick the `session_id` to control scope.

### 2.3 Code execution (Code Interpreter)

Two ways to invoke:

**MCP tool path** (LLM-driven, traverses middleware):

```python
register_code_interpreter_tools(default_registry(), region="us-east-1")
# LLM can now call execute_python with generated code.
```

**Direct path** (your code, not LLM-driven) — use the `CodeSandbox`
Protocol:

```python
# The AgentCore code-interpreter doesn't ship a CodeSandbox impl
# directly (it's wired as MCP tools). For direct execution, call the
# registered tool through the registry:
result = await default_registry().invoke("execute_python", {"code": "print(2+2)"})
# {"stdout": "4\n", "stderr": "", "exit_code": 0}
```

### 2.4 Browser automation

Five tools after `register_browser_tools(...)`. Each is one HTTP call
to the AgentCore Browser session:

| Tool | Args | Returns |
|---|---|---|
| `browser_navigate` | `url: str` | `{"url": ..., "status": ...}` |
| `browser_click` | `selector: str` | `{"selector": ..., "status": ...}` |
| `browser_fill` | `selector: str, value: str` | `{...}` |
| `browser_extract_text` | `selector: str = "body"` | `str` |
| `browser_screenshot` | — | `{"png_base64": ...}` |

Use a stable `session_id` if you want the browser session to persist
across calls. Pass `session_id=` to `register_browser_tools(...)`.

### 2.5 Inbound JWT verification

Default usage (inside AgentCore Runtime): skip. AgentCore Runtime
verifies tokens at the edge.

Defense-in-depth or outside-Runtime usage:

```python
from eap_core.integrations.agentcore import InboundJwtVerifier, jwt_dependency
from fastapi import Depends

verifier = InboundJwtVerifier(
    discovery_url="https://agentcore-identity.us-east-1.amazonaws.com/.well-known/openid-configuration",
    issuer="https://agentcore-identity.us-east-1.amazonaws.com",
    allowed_audiences=["my-agent"],
    allowed_scopes=["agent:invoke"],
    allowed_clients=["specific-client-id"],   # optional, narrow further
    jwks_cache_ttl_seconds=600,                # default; tune for your IdP
    clock_skew_seconds=30,                     # default; tune for your IdP
)

@app.post("/invocations")
async def handle(claims: dict = Depends(jwt_dependency(verifier))):
    user_id = claims["sub"]
    ...
```

Validates RS256/RS384/RS512 signatures, audience, scope, and (if
configured) client id. Rejected tokens raise PyJWT exceptions that
FastAPI maps to 401.

### 2.6 Outbound Gateway calls

```python
gw = GatewayClient(
    gateway_url="https://my-gw.bedrock-agentcore.us-east-1.amazonaws.com/mcp",
    identity=nhi,
    audience="my-gateway",
    scope="tools:invoke",
    timeout_seconds=30.0,           # default
)

tools = await gw.list_tools()
result = await gw.invoke("remote_tool", {"arg": "value"})

# Always close:
await gw.aclose()
```

Use `add_gateway_to_registry(default_registry(), gw, tools)` to
register remote tools as local proxies. After that, your agent code
treats them like any other tool — the middleware chain runs locally
before each forward.

For SigV4 instead of OAuth (the AWS-native option), pass `auth=` (an
httpx auth object) instead of `identity=`.

### 2.7 Publishing tools to Gateway

```bash
uv run eap publish-to-gateway \
    --entry agent.py \
    --title "my-agent tools" \
    --server-url https://my-agent.example.com \
    [--dry-run]
```

Produces `dist/gateway/openapi.json` + a `README.md`. Upload the
OpenAPI to AgentCore Gateway as a **HTTP target**. Each
`@mcp_tool` becomes a `POST /tools/<name>` operation with the input
JSON Schema as the request body.

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

### 2.8 Registry — discovery and publishing

```python
from eap_core.integrations.agentcore import RegistryClient

registry = RegistryClient(registry_name="bank-platform", region="us-east-1")

# Publish your agent's card:
record_id = await registry.publish_agent_card(card)

# Publish a standalone MCP server:
await registry.publish_mcp_server(
    "doc-search-mcp",
    description="Internal documentation search via MCP.",
    mcp_endpoint="stdio://internal/doc-search",
)

# Discover others:
hits = await registry.search("payments", max_results=10)
record = await registry.get("doc-search-mcp")
all_servers = await registry.list_records(record_type="MCP_SERVER")
```

### 2.9 Payments — x402 microtransactions

The pattern: open a session with a budget, catch `PaymentRequired`
from tools that hit 402, sign and retry.

```python
from eap_core.integrations.agentcore import PaymentClient
from eap_core.payments import PaymentRequired

pay = PaymentClient(
    wallet_provider_id="my-cdp-wallet",
    max_spend_cents=500,
    currency="USD",
    session_ttl_seconds=3600,
    region="us-east-1",
)
await pay.start_session()

# Budget bookkeeping is available before any call:
if pay.can_afford(amount_cents=50):
    ...

# After payments are made:
pay.spent_cents       # e.g. 50
pay.remaining_cents   # e.g. 450
```

`authorize_and_retry(req)` returns the signed receipt. The caller
re-issues the original HTTP request with an `X-Payment-Receipt`
header carrying that receipt.

### 2.10 Evaluations

Two flows:

**In-flow scoring** — drop `AgentCoreEvalScorer` into `EvalRunner`:

```python
runner = EvalRunner(scorers=[
    AgentCoreEvalScorer(
        evaluator_arn="arn:aws:bedrock-agentcore:::evaluator/Builtin.Helpfulness",
        scorer_name="helpfulness",     # optional; overrides default "agentcore_eval"
    ),
])
report = await runner.run(trajectories)
```

**Export-and-upload** — convert Trajectory rows for AgentCore
Evaluations batch jobs:

```python
from eap_core.integrations.agentcore import to_agentcore_eval_dataset

rows = to_agentcore_eval_dataset(trajectories)
# rows is a list of dicts: trace_id / question / answer / contexts / steps
# Upload to S3 or feed directly to boto3 evaluate_* calls.
```

Built-in evaluator ARNs include `Builtin.Helpfulness`,
`Builtin.Faithfulness`, `Builtin.Coherence`. Custom evaluators take
the form `arn:aws:bedrock-agentcore:<region>:<account>:evaluator/<name>`.

### 2.11 Observability — what shows up in CloudWatch

After `configure_for_agentcore()` (or when running inside AgentCore
Runtime, which auto-injects OTLP), every request is a span with:

- `gen_ai.request.model` — the model id from `RuntimeConfig`.
- `gen_ai.operation.name` — e.g. `chat`, `tool.invoke`.
- `gen_ai.usage.input_tokens` / `gen_ai.usage.output_tokens` — usage.
- `gen_ai.response.finish_reason` — `stop`, `length`, `tool_call`, etc.
- `gen_ai.error.type` — present on errors.

Plus EAP-Core-specific attributes:

- `policy.matched_rule` — which rule allowed the call.
- `pii.masked_count` — tokens that went into the per-request vault.
- `tool.name` — for `tool.invoke` spans.

Custom middleware can add namespaced attrs via `ctx.metadata["myns.key"] = ...`
— they'll show up automatically.

In CloudWatch you'll see them under **AgentCore → Observability →
Traces**.

### 2.12 Deploy

```bash
# Default: package only (no docker build).
uv run eap deploy --runtime agentcore --service my-agent

# With Docker build (still local, no push):
EAP_ENABLE_REAL_DEPLOY=1 uv run eap deploy --runtime agentcore --service my-agent --region us-east-1
# → Built image: <local-image>:<tag>

# After build, push to ECR and register manually (see dist/agentcore/README.md):
aws ecr get-login-password --region us-east-1 | \
    docker login --username AWS --password-stdin <account>.dkr.ecr.us-east-1.amazonaws.com
docker tag my-agent:latest <account>.dkr.ecr.us-east-1.amazonaws.com/my-agent:v1
docker push <account>.dkr.ecr.us-east-1.amazonaws.com/my-agent:v1
# Then register the image with AgentCore Runtime via the console or
# `aws bedrock-agentcore-control create-runtime`.
```

Inside the AgentCore Runtime, the handler `dist/agentcore/handler.py`
exposes:

- `POST /invocations` — accepts `{"prompt": "..."}`, calls your entry
  function, returns `{"response": "...", "status": "success"}`.
- `GET /ping` — AgentCore healthcheck.

Both honor port 8080 per the AgentCore HTTP contract.

---

## Part 3 — Production checklist

Before flipping live traffic on:

- [ ] `EAP_ENABLE_REAL_RUNTIMES=1` set in the AgentCore Runtime env.
- [ ] AWS region matches where your AgentCore tenancy lives.
- [ ] `LocalIdPStub` replaced with a real signer for `NonHumanIdentity`.
- [ ] AgentCore Workload Identity exists and matches the
      `workload_identity_id` you pass to `OIDCTokenExchange.from_agentcore`.
- [ ] `configs/policy.json` tightened — start with explicit `permit`
      rules per `(action, resource, role)` combination. Default-deny
      is the safe baseline.
- [ ] If hitting regulated data, install `[pii]` and use
      `PiiMaskingMiddleware(engine="presidio")`. The regex tokenizer
      is a starter, not a finish line.
- [ ] `AgentCoreMemoryStore.memory_id` is the right one — accidentally
      pointing at staging memory from prod is a quietly catastrophic
      bug.
- [ ] If you're publishing tools to Gateway, the OpenAPI's
      `server_url` is the production hostname, not localhost.
- [ ] If using `InboundJwtVerifier` for defense-in-depth,
      `allowed_audiences` is set (otherwise audience validation is a
      no-op).
- [ ] If using `PaymentClient`, `max_spend_cents` is the budget you
      actually want — not the default 100¢.
- [ ] `eap eval` runs in CI against `tests/golden_set.json` with
      `--threshold` set high enough to catch real regressions.
- [ ] CloudWatch trace search returns hits when you exercise the
      runtime URL. If not, OTLP env vars didn't propagate.

---

## Troubleshooting

**`NotImplementedError: AgentCore adapter requires the [aws] extra
and AWS credentials. Set EAP_ENABLE_REAL_RUNTIMES=1 once configured.`**

You forgot the env flag. The flag is intentional — it prevents tests
from accidentally hitting AWS. Set `EAP_ENABLE_REAL_RUNTIMES=1` in
your runtime env (not in pytest).

**`ImportError: ... requires the [aws] extra: pip install eap-core[aws]`**

The `[aws]` extra isn't installed. With uv:
`uv sync --all-packages --group dev --extra aws`.

**`MCPError: gateway returned HTTP 403`**

The bearer token is missing or invalid. Check that your `NonHumanIdentity`
has the right `default_audience` (or that you're passing
`audience=` explicitly to `GatewayClient`). Token TTL is short by
design — re-acquire if you've been holding one.

**`jwt.InvalidTokenError: no JWKS key matches kid=...`**

The token's `kid` header doesn't match any key in the IdP's JWKS.
Usually this means token-vs-IdP mismatch (a token from staging IdP
hitting prod verifier). Double-check `discovery_url`.

**CloudWatch shows no traces.**

The OTLP env vars didn't propagate. Run `env | grep OTEL` in your
AgentCore Runtime container. If they're missing, AgentCore's
auto-injection didn't fire — `configure_for_agentcore()` explicitly
in `agent.py` is the workaround.

**`RuntimeError: payment of N USD would exceed remaining budget`**

You hit the `max_spend_cents` ceiling. Either raise the ceiling at
`PaymentClient` construction time, or surface the payment-required
error to the user and let them top up.

**Tools registered but the LLM doesn't call them.**

Check the AgentCard — `build_card(skills_from=default_registry())`
reads the live registry, so if a tool registered after the card was
built it won't be advertised. Rebuild and re-publish the card after
adding tools.

---

## What's next

- For a project where you start from AgentCore and want to also run
  on GCP without rewriting business logic, see
  [`docs/user-guide-gcp-vertex.md`](user-guide-gcp-vertex.md). The
  Protocol seams (`MemoryStore`, `CodeSandbox`, `BrowserSandbox`,
  `AgentRegistry`, `PaymentBackend`) mean swapping clouds is a
  constructor change.
- For the full AgentCore service-by-service mapping, see
  [`docs/integrations/aws-bedrock-agentcore.md`](integrations/aws-bedrock-agentcore.md).
- For extending the SDK itself (adding middleware, runtime
  adapters, new cloud integrations), see
  [`docs/developer-guide.md`](developer-guide.md).
