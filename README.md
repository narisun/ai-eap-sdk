# EAP-Core — Enterprise Agentic AI Platform SDK

A **thin, standard-first** Python SDK and CLI for building agentic AI
solutions where **safety, observability, and compliance are the
default**, not an afterthought. EAP-Core wraps low-level cloud LLM
runtimes (AWS Bedrock, Google Vertex AI, plus a runnable local
runtime) behind a single client interface and routes every request
through a middleware chain that enforces cross-cutting concerns
automatically. The same agent code runs unmodified against
**AWS Bedrock AgentCore** and **GCP Vertex Agent Engine** — both
cloud platforms are wired through vendor-neutral Protocols.

You write business logic in plain Python. EAP-Core handles the rest.

---

## Why a thin SDK?

Most AI platforms are either too thick (a walled garden that locks you
into one vendor) or too thin (a transport library that leaves every
team to re-invent safety, eval, audit, and policy on its own).

EAP-Core is deliberately positioned as a **thin bridge** — the
"paved road" that:

- **Centralizes cross-cutting concerns** in a single, swappable
  middleware chain so every team gets prompt-injection sanitization,
  PII masking, OTel observability, policy enforcement, and output
  schema validation **for free**, the same way, every time.
- **Stays out of the way of innovation**. The SDK is built on open
  protocols — **MCP** for tools, **A2A** for agent cards, **OTel
  GenAI** for tracing, **OAuth 2.1 / RFC 8693** for identity. Every
  heavyweight integration (Presidio, OpenTelemetry SDK, AWS, GCP,
  Cedar, Ragas, the official MCP SDK, FastAPI) is an optional extra,
  lazy-imported behind a clean interface, and trivially replaceable.
- **Lets each team pick its own framework**. EAP-Core does not
  prescribe LangChain or LangGraph or pydantic-ai or anything else for
  business logic. Build state graphs in pure Python, use whatever
  reasoning library you like — the middleware chain doesn't care.

## What you get out of the box

```dot
                   user code (agent.py, tools/*.py)
                              │
                              ▼
   ┌──────────────────────────────────────────────────────────┐
   │                 EnterpriseLLM client                     │
   │    generate_text() │ stream_text() │ invoke_tool()       │
   └──────────────────────────────────────────────────────────┘
                              │
                              ▼
   ┌──────────────────────────────────────────────────────────┐
   │              MiddlewarePipeline (onion model)            │
   │ ┌──────────────────────────────────────────────────────┐ │
   │ │  PromptInjectionMiddleware  (regex + classifier)     │ │
   │ │  PiiMaskingMiddleware       (regex / Presidio)       │ │
   │ │  ObservabilityMiddleware    (OTel GenAI semconv)     │ │
   │ │  PolicyMiddleware           (JSON / Cedar)           │ │
   │ │  OutputValidationMiddleware (Pydantic v2 schemas)    │ │
   │ │  TrajectoryRecorder         (eval/audit, opt-in)     │ │
   │ └──────────────────────────────────────────────────────┘ │
   └──────────────────────────────────────────────────────────┘
                              │
                              ▼
   ┌──────────────────────────────────────────────────────────┐
   │            BaseRuntimeAdapter (Strategy)                 │
   │    LocalRuntimeAdapter ▪ BedrockRuntimeAdapter ▪         │
   │    VertexRuntimeAdapter ▪ <your custom adapter>          │
   └──────────────────────────────────────────────────────────┘
```

Cross-cutting concerns are real, replaceable middleware:

| Middleware | Default | Optional extra | Standard |
|---|---|---|---|
| `PromptInjectionMiddleware` | regex patterns | plug your own classifier | — |
| `PiiMaskingMiddleware` | regex tokenizer + per-request vault | `[pii]` → Microsoft Presidio | — |
| `ObservabilityMiddleware` | metadata-only (no-op) | `[otel]` → OpenTelemetry SDK + OTLP exporter | **OTel GenAI semconv** |
| `PolicyMiddleware` | minimal JSON evaluator (Cedar-shaped) | `[policy-cedar]` → real Cedar bindings | — |
| `OutputValidationMiddleware` | Pydantic v2 (always required) | — | — |
| `TrajectoryRecorder` | JSONL writer (always available) | — | — |

Vendor-neutral Protocols at the top level — pick the in-process
default for tests/dev, swap in a cloud-backed implementation for
production. The same agent code works on either cloud:

| Protocol | In-process default | AWS Bedrock AgentCore impl | GCP Vertex Agent Engine impl |
|---|---|---|---|
| `MemoryStore` | `InMemoryStore` | `AgentCoreMemoryStore` | `VertexMemoryBankStore` |
| `CodeSandbox` | `InProcessCodeSandbox` | (AgentCore Code Interpreter) | `VertexCodeSandbox` |
| `BrowserSandbox` | `NoopBrowserSandbox` | (AgentCore Browser) | `VertexBrowserSandbox` |
| `AgentRegistry` | `InMemoryAgentRegistry` | `RegistryClient` | `VertexAgentRegistry` |
| `PaymentBackend` | `InMemoryPaymentBackend` | `PaymentClient` (x402) | `AP2PaymentClient` (AP2) |
| `ThreatDetector` | `RegexThreatDetector` | — | — |
| `NonHumanIdentity`-shaped | `LocalIdPStub` | `OIDCTokenExchange` | `VertexAgentIdentityToken` |

**End-to-end user guides for each cloud** (zero to deployed agent,
with every command and every snippet):

- [`docs/user-guide-aws-agentcore.md`](docs/user-guide-aws-agentcore.md)
  — AWS Bedrock AgentCore.
- [`docs/user-guide-gcp-vertex.md`](docs/user-guide-gcp-vertex.md)
  — GCP Vertex Agent Engine.

**Reference docs** (positioning + service-by-service mapping):

- [`docs/integrations/aws-bedrock-agentcore.md`](docs/integrations/aws-bedrock-agentcore.md)
- [`docs/integrations/gcp-vertex-agent-engine.md`](docs/integrations/gcp-vertex-agent-engine.md)

Other cross-cutting concerns wired at the SDK level:

- **Non-Human Identity (NHI)** — `NonHumanIdentity` issues
  short-lived JWTs per tool call, cached by `(audience, scope)`.
  `OIDCTokenExchange` implements **RFC 8693** so the same code points
  at Okta / Auth0 / Cognito by config change.
- **MCP tool registry** — `@mcp_tool` decorates a Python function and
  generates a JSON Schema from its type hints. The same registry is
  reachable two ways: in-process via `client.invoke_tool()` and over
  MCP stdio via `eap_core.mcp.server.run_stdio()` (or the standalone
  `eap create-mcp-server` project).
- **A2A AgentCard** — `build_card(skills_from=registry, ...)` reads
  the live registry so the advertised skills can never drift from the
  agent's actual tools. Optional FastAPI helper to serve
  `GET /.well-known/agent-card.json`.
- **Eval framework** — `EvalRunner` drives a JSON golden-set through
  the agent, scores each case via a swappable `Judge` (deterministic
  for tests, LLM-backed for production), and emits JSON / HTML / JUnit
  reports. The same JSONL trajectory written by `TrajectoryRecorder`
  is also a Ragas-compatible dataset via the optional `[eval]` extra.

## Flexibility — pick your stack, swap the parts

Everything below is genuinely swappable:

- **Cloud runtime**: change `RuntimeConfig(provider=...)` from
  `local` → `bedrock` → `vertex`. Third-party adapters discoverable
  via the `eap_core.runtimes` entry-point group — ship
  `eap-runtime-azure` without forking the core.
- **Middleware chain**: pass your own ordered list to
  `EnterpriseLLM(middlewares=[...])`. Drop ones you don't want, insert
  custom ones, reorder freely.
- **Policy engine**: the `PolicyEvaluator` Protocol accepts the JSON
  evaluator (default), `cedarpy` (extra), or anything else you write.
- **Eval scorer / judge**: implement the `Judge` Protocol, or bring
  in Ragas / DeepEval and feed the recorded trajectories.
- **PII detection**: regex by default; flip to Presidio with one
  argument; or write your own.
- **Identity provider**: `LocalIdPStub` for dev, real OIDC IdPs for
  production via `OIDCTokenExchange` — same code, config change.
- **State / memory**: nothing here. Bring your own. The SDK is
  stateless except for the per-request `Context` object.

## Open protocols — what you bet on

EAP-Core picks open standards everywhere a vendor-neutral choice
exists:

- **MCP** — every tool registered via `@mcp_tool` is automatically a
  valid MCP tool, callable in-process *and* over stdio.
- **A2A AgentCard** — `/.well-known/agent-card.json` served from your
  FastAPI app, auto-built from the live tool registry.
- **OTel GenAI** — every LLM and tool call is a span with the
  `gen_ai.*` semantic-convention attributes. Drop in any compatible
  exporter.
- **OAuth 2.1 / RFC 8693** — token-exchange grant for delegated
  access from agent identity to tool resource.
- **Pydantic v2** — every public data structure is a Pydantic model
  or dataclass; JSON Schema for free.
- **x402 / AP2** — agent-payment protocols for microtransactions,
  abstracted behind the `PaymentBackend` Protocol.

Vendor-specific glue (Bedrock client, Vertex client, Presidio, Cedar)
lives behind optional extras, **never on the hot path of an
unconfigured install**.

---

## Install

EAP-Core is an internal SDK; it is **not published to public PyPI**.
Install from source.

You need Python 3.11 or newer. We use [`uv`](https://docs.astral.sh/uv/)
for dependency management — the workspace and lockfile assume it.

### Working from the repository

```bash
git clone https://github.com/narisun/ai-eap-sdk.git
cd ai-eap-sdk
uv sync --all-packages --group dev
```

This installs:

- `eap-core` — the SDK (importable as `eap_core`)
- `eap-cli` — the `eap` CLI (entry point: `eap`)
- dev tooling (pytest, ruff, mypy)

### Adding to a downstream project

In your project's `pyproject.toml`, depend on EAP-Core directly from
the git repo (pin to a tag for stability):

```toml
[project]
dependencies = [
    "eap-core @ git+https://github.com/narisun/ai-eap-sdk.git@v0.3.0#subdirectory=packages/eap-core",
]
```

Or via `uv add`:

```bash
uv add "eap-core @ git+https://github.com/narisun/ai-eap-sdk.git@v0.3.0#subdirectory=packages/eap-core"
uv add "eap-cli  @ git+https://github.com/narisun/ai-eap-sdk.git@v0.3.0#subdirectory=packages/eap-cli"
```

If you operate a private package index (e.g. AWS CodeArtifact, Azure
Artifacts, an internal devpi), upload built wheels there and depend on
them by name. The build is `uv build` from each `packages/<name>/`
directory.

To enable an optional extra at install time:

```bash
uv sync --all-packages --group dev --extra otel          # OTel SDK + OTLP exporter
uv sync --all-packages --group dev --extra pii           # Microsoft Presidio
uv sync --all-packages --group dev --extra mcp           # official MCP SDK (stdio server)
uv sync --all-packages --group dev --extra a2a           # FastAPI for AgentCard route
uv sync --all-packages --group dev --extra eval          # Ragas adapter
uv sync --all-packages --group dev --extra aws           # boto3 — AgentCore integration
uv sync --all-packages --group dev --extra gcp           # google-cloud-aiplatform — Vertex integration
uv sync --all-packages --group dev --extra policy-cedar  # cedarpy — production policy engine
```

Or install everything:

```bash
uv sync --all-packages --all-extras --group dev
```

Verify:

```bash
uv run eap --help
uv run python -c "import eap_core; print(eap_core.__version__)"
```

If you don't have `uv`:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

---

## Quick start (5 minutes)

Scaffold a new agent project and run it end-to-end with no cloud
credentials:

```bash
# 1. Create a project (uses LocalRuntimeAdapter — no creds needed)
uv run eap init my-agent --name my-agent --runtime local
cd my-agent

# 2. Run it
python agent.py
# → "[local-runtime] received N tokens, model=echo-1"

# 3. Eval it against a golden dataset
uv run eap eval --dataset tests/golden_set.json --report json
```

That's the full loop: scaffold → run → eval. Cross-cutting concerns
(prompt-injection sanitization, PII masking, OTel attributes, policy
enforcement, output validation) are already wired into `agent.py` and
running on every call.

To swap to a real cloud LLM later, change one line in `agent.py`:

```python
RuntimeConfig(provider="bedrock", model="anthropic.claude-3-5-sonnet")
# or provider="vertex", model="gemini-1.5-pro"
```

…and set `EAP_ENABLE_REAL_RUNTIMES=1` in your environment.

---

## CLI reference

The `eap` CLI is the **golden path** for new artifacts. Each command
delegates to a pure-Python scaffolder so you can use the library
directly if you want.

### `eap init`

```bash
eap init <DIR> [--name NAME] [--runtime local|bedrock|vertex] [--force]
```

Scaffolds a new EAP-Core agent project. Lays down `agent.py`, the
default middleware chain, an example MCP tool, a JSON policy file, an
A2A AgentCard, and a minimal golden-set for eval. The result runs
end-to-end out of the box on the local runtime.

### `eap create-agent`

```bash
eap create-agent --name NAME --template research|transactional
```

Overlays an agent template onto the current project:

- **`research`** — retrieval-style: calls a `search_docs` tool, then
  asks the LLM to summarize with the docs as context. Useful starting
  point for QA, research-agent, RAG-backed assistants.
- **`transactional`** — action-style: `get_account` + `transfer_funds`
  with explicit policy gates and idempotency-key handling. Good
  starting point for any agent that performs writes.

### `eap create-tool`

```bash
eap create-tool --name <name> --mcp [--auth-required]
```

Adds a typed Python function under `tools/<name>.py` decorated with
`@mcp_tool`. JSON Schema is generated from your type hints.
`--auth-required` marks the tool as requiring an OAuth token (the NHI
flow handles token acquisition; the `PolicyMiddleware` rejects calls
that lack the right scope).

### `eap create-mcp-server`

```bash
eap create-mcp-server <DIR> [--name NAME] [--force]
```

Scaffolds a **standalone** MCP-stdio server project (no LLM, just
tools). Generates `server.py` that calls
`eap_core.mcp.server.run_stdio()` against the registry. Other agents,
Claude Desktop, Claude Code, or your IDE connect to it via the MCP
protocol. The same `eap create-tool` command works inside this
project to add more tools.

### `eap eval`

```bash
eap eval --dataset PATH \
         [--agent agent.py:answer] \
         [--report json|html|junit] \
         [--threshold 0.7] \
         [--output FILE]
```

Loads a golden-set JSON, drives each case through your agent, scores
the trajectory via the configured scorers (default: `FaithfulnessScorer`
with `DeterministicJudge`), and emits a report. Exits non-zero if any
case scores below `--threshold` — drop into CI to turn quality
regressions into failed builds.

Golden-set format:

```json
[
  {
    "id": "case-001",
    "input": "What is the capital of France?",
    "expected_contexts": ["Paris is the capital of France."],
    "expected_answer_substrings": ["Paris"]
  }
]
```

### `eap deploy`

```bash
eap deploy --runtime aws|gcp|agentcore|vertex-agent-engine \
           [--bucket BUCKET | --service NAME] \
           [--entry agent.py:answer] [--region REGION] [--dry-run]
```

Packages your project for deployment:

- **`--runtime aws`** — produces `dist/agent.zip` matching the Lambda
  handler layout. Prints the `aws s3 cp` command to upload.
- **`--runtime gcp`** — produces `dist/agent/` with a `Dockerfile` and
  `cloudbuild.yaml` for Cloud Run. Prints the `gcloud run deploy`
  command.
- **`--runtime agentcore`** — produces `dist/agentcore/` with an
  ARM64 Dockerfile + FastAPI handler that satisfies AgentCore
  Runtime's contract (`POST /invocations`, `GET /ping`, port 8080).
  README walks through ECR push and AgentCore Runtime registration.
- **`--runtime vertex-agent-engine`** — produces
  `dist/vertex-agent-engine/` with a Cloud Run-compatible image
  (`linux/amd64`, `PORT` env, `EXPOSE 8080`) and a FastAPI handler
  exposing `POST /invocations` + `GET /health`. README walks through
  Artifact Registry push and Vertex Agent Engine registration.

Live cloud calls are gated behind `EAP_ENABLE_REAL_DEPLOY=1` —
packaging is safe in CI, deploy is opt-in.

---

## What a scaffolded project looks like

After `eap init my-agent && eap create-agent --template research --name my-agent`:

```
my-agent/
├── .claude.md              # context for AI coding agents
├── .gitignore
├── README.md
├── pyproject.toml
├── agent.py                # business logic — uses EnterpriseLLM
├── responses.yaml          # canned local-runtime responses (deterministic dev)
├── configs/
│   ├── policy.json         # JSON policy (Cedar-shaped)
│   └── agent_card.json     # A2A AgentCard
├── tests/
│   └── golden_set.json     # eval cases
└── tools/
    ├── __init__.py
    ├── example_tool.py     # echo MCP tool
    └── search_docs.py      # research-template tool
```

**`agent.py` is ~40 lines**. It builds an `EnterpriseLLM` with the
default middleware chain, loads the policy, and exposes an `answer()`
function. That is **all** you need to maintain — every cross-cutting
concern is owned by the SDK.

The scaffolded `.claude.md` tells future AI coding agents (Claude
Code, Copilot) where the boundaries are, so they don't try to reinvent
PII masking or OTel spans inside your business logic.

---

## Authoring custom pieces

### Custom middleware

```python
from eap_core.middleware import PassthroughMiddleware
from eap_core.types import Context, Request

class TenantStamper(PassthroughMiddleware):
    name = "tenant_stamper"

    def __init__(self, tenant_id: str) -> None:
        self._tenant_id = tenant_id

    async def on_request(self, req: Request, ctx: Context) -> Request:
        ctx.metadata["tenant_id"] = self._tenant_id
        return req
```

Pass it into `EnterpriseLLM(middlewares=[..., TenantStamper("acme"), ...])`.

### Custom runtime adapter

```python
from eap_core.runtimes import BaseRuntimeAdapter, RawResponse

class MyLLMAdapter(BaseRuntimeAdapter):
    name = "my-llm"
    async def generate(self, req): ...
    async def stream(self, req): ...
    async def list_models(self): ...
```

Register via the entry-point group `eap_core.runtimes` in your
`pyproject.toml`:

```toml
[project.entry-points."eap_core.runtimes"]
my-llm = "my_pkg.adapter:MyLLMAdapter"
```

`RuntimeConfig(provider="my-llm", ...)` then resolves your adapter.

### Custom eval scorer

```python
from eap_core.eval import EvalRunner

class AnswerLengthScorer:
    name = "answer_length"
    async def score(self, traj):
        return ...  # FaithfulnessResult-shaped

runner = EvalRunner(agent=my_agent, scorers=[AnswerLengthScorer()])
```

### Custom policy evaluator

Implement the `PolicyEvaluator` Protocol; pass your evaluator to
`PolicyMiddleware(evaluator)`. Use the JSON evaluator for fast iteration,
swap to `cedarpy` (or your own) for production.

---

## Observability — what you get for free

Every request emits an OTel GenAI span with the standard
`gen_ai.*` attributes (`gen_ai.request.model`,
`gen_ai.operation.name`, `gen_ai.usage.input_tokens`,
`gen_ai.usage.output_tokens`, `gen_ai.response.finish_reason`,
`gen_ai.error.type`).

Without the `[otel]` extra installed, the same attributes still land
on the per-request `ctx.metadata` so downstream consumers (audit log,
trajectory recorder, custom middleware) keep working — they just
don't get exported off-process.

With the `[otel]` extra installed, drop in any OTLP-compatible backend
(Honeycomb, Tempo, Datadog, OpenObserve, etc.) by configuring the
exporter. No SDK code changes.

---

## Production checklist

- [ ] Set `EAP_ENABLE_REAL_RUNTIMES=1` and configure cloud creds.
- [ ] Replace `LocalIdPStub` with `OIDCTokenExchange` pointed at your
      real IdP (set `IdentityConfig.idp_url` and `private_key_pem`).
- [ ] Tighten `configs/policy.json` — start with explicit `permit`
      rules for each tool action you intend to allow.
- [ ] Install `[otel]` and configure OTLP exporter env vars.
- [ ] Install `[pii]` if you're handling regulated data; the regex
      tokenizer is a starter, Presidio is the production-grade choice.
- [ ] Run `eap eval` in CI against your golden set with a meaningful
      `--threshold`. Fail the build on regressions.
- [ ] If deploying to **AWS Bedrock AgentCore**: install `[aws]`,
      call `configure_for_agentcore()` at startup, and follow
      `dist/agentcore/README.md` for ECR push + AgentCore Runtime
      registration. See [`docs/integrations/aws-bedrock-agentcore.md`](docs/integrations/aws-bedrock-agentcore.md).
- [ ] If deploying to **GCP Vertex Agent Engine**: install `[gcp]`,
      call `configure_for_vertex_observability()` at startup, and
      follow `dist/vertex-agent-engine/README.md` for Artifact Registry
      push + Vertex registration. See
      [`docs/integrations/gcp-vertex-agent-engine.md`](docs/integrations/gcp-vertex-agent-engine.md).

---

## Repository layout

```
ai-eap-sdk/
├── packages/
│   ├── eap-core/                            # the SDK
│   │   └── src/eap_core/
│   │       ├── integrations/                # cloud-platform adapters
│   │       │   ├── agentcore.py             # AWS Bedrock AgentCore (11 services)
│   │       │   └── vertex.py                # GCP Vertex Agent Engine
│   │       ├── sandbox.py                   # CodeSandbox / BrowserSandbox Protocols
│   │       ├── discovery.py                 # AgentRegistry Protocol
│   │       ├── payments.py                  # PaymentBackend + PaymentRequired
│   │       ├── security.py                  # ThreatDetector Protocol
│   │       └── memory.py                    # MemoryStore Protocol
│   └── eap-cli/                             # the `eap` CLI
├── examples/
│   ├── research-agent/                      # retrieval-style reference project
│   ├── transactional-agent/                 # action-style with auth-required tools
│   └── mcp-server-example/                  # standalone MCP-stdio server
├── docs/
│   ├── developer-guide.md                   # for engineers extending the SDK
│   ├── integrations/
│   │   ├── aws-bedrock-agentcore.md         # full AgentCore positioning + per-phase usage
│   │   └── gcp-vertex-agent-engine.md       # full Vertex positioning + per-phase usage
│   └── superpowers/
│       ├── specs/                           # full design spec
│       └── plans/                           # implementation plans (Plans 1–4)
└── pyproject.toml                           # uv workspace root
```

**Status:** v0.3.0 — full integrations with AWS Bedrock AgentCore
(11 services) and GCP Vertex Agent Engine. The same agent code runs
unmodified on both clouds via the vendor-neutral Protocols above. All
live cloud calls are gated behind `EAP_ENABLE_REAL_RUNTIMES=1` (and
`EAP_ENABLE_REAL_DEPLOY=1` for `eap deploy` packaging) — flip the
flags and configure creds when you're ready to hit production.

---

## Extending EAP-Core

Adding a new middleware, runtime adapter, eval scorer, policy
evaluator, CLI template, or optional extra? Read
[**`docs/developer-guide.md`**](docs/developer-guide.md) first. It
covers the load-bearing design principles, the middleware contract,
the extension cookbook (with code examples for each extension point),
how to evolve the SDK as the open source ecosystem changes, the
anti-patterns to refuse, and the future-proofing checklist to walk
before merging non-trivial changes.

---

## Development

```bash
uv sync --all-packages --all-extras --group dev   # install everything
uv run pytest --cov                                # 342 tests, ≥90% coverage
uv run ruff check && uv run ruff format --check    # lint + format
uv run mypy                                        # strict type-check
```

The full design and the original four implementation plans live under
`docs/superpowers/`. Read those, plus `docs/developer-guide.md`,
before making non-trivial changes.

## Changelog

See [`CHANGELOG.md`](CHANGELOG.md) for what's shipped in each version.

## License

MIT.
