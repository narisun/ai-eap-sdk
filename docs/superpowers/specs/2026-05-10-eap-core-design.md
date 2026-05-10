# EAP-Core Design Spec

**Date:** 2026-05-10
**Status:** Approved (brainstorming)
**Scope:** Walking skeleton / reference architecture for the Enterprise Agentic AI Platform SDK.

## 1. Objective and scope

EAP-Core is a "standard-first" Python SDK and CLI that scaffolds and governs agentic AI solutions. It centralizes cross-cutting concerns (safety, observability, compliance) using open protocols (MCP, OTel GenAI, A2A) and is vendor-neutral across cloud runtimes.

This spec covers a **walking skeleton**:

- All public modules and interfaces exist with working in-memory implementations.
- Cloud adapters (AWS Bedrock AgentCore, GCP Vertex AI) are shape-correct stubs gated behind an env flag.
- Heavy external dependencies (Presidio, OTel, MCP SDK, cedarpy, Ragas) are real but installed via optional extras; the core install is slim.
- Scaffolded projects run end-to-end out of the box on a `LocalRuntimeAdapter`.
- The middleware chain is fully real: PII masking, OTel spans, policy checks, schema validation all execute on every call.

Out of scope: production cloud wiring, multi-tenant deployment infra, GUI tools, marketplace, billing.

## 2. Decisions locked in (from brainstorming)

| Axis | Decision |
|---|---|
| Scope | Walking skeleton; cloud adapters mocked, gated by `EAP_ENABLE_REAL_RUNTIMES=1` |
| Dependencies | Real deps as optional extras; lazy-imported with clear "install the X extra" errors |
| API style | Async-first; sync wrapper via `client.sync.*` |
| Scaffolded project runnability | Runs end-to-end out of the box via `LocalRuntimeAdapter` |
| Policy engine | Minimal JSON-based evaluator (default); `cedarpy` available via `[policy-cedar]` extra |

## 3. Repo layout

```
ai-eap-sdk/
├── pyproject.toml              # workspace root, declares members
├── uv.lock
├── packages/
│   ├── eap-core/
│   │   ├── pyproject.toml      # name=eap-core; extras: pii,otel,aws,gcp,eval,mcp,a2a,policy-cedar
│   │   ├── src/eap_core/...
│   │   └── tests/
│   └── eap-cli/
│       ├── pyproject.toml      # depends on eap-core; entry point: eap = eap_cli.main:cli
│       ├── src/eap_cli/...
│       └── tests/
├── docs/
│   ├── superpowers/specs/      # this design doc lives here
│   ├── runtimes/               # bedrock.md, vertex.md — wiring guides
│   └── architecture.md
├── examples/
│   └── research-agent/         # committed reference of a scaffolded project
└── .github/workflows/ci.yml
```

Two packages in one uv workspace. `uv sync` at the root installs everything editable.

## 4. `eap_core` package structure

```
eap_core/
├── client.py              # EnterpriseLLM — public entry point
├── config.py              # RuntimeConfig, EvalConfig, IdentityConfig
├── exceptions.py          # PromptInjectionError, PolicyDeniedError, OutputValidationError, ...
├── middleware/
│   ├── base.py            # Middleware Protocol + Request/Response/Context types
│   ├── pipeline.py        # MiddlewarePipeline (chain-of-responsibility executor)
│   ├── sanitize.py        # PromptInjection middleware (regex + classifier hook)
│   ├── pii.py             # PiiMasking middleware: regex (default) + Presidio (extra)
│   ├── observability.py   # ObservabilityMiddleware: OTel GenAI semconv spans
│   ├── policy.py          # PolicyMiddleware: minimal JSON evaluator + cedarpy adapter
│   └── validate.py        # OutputValidation middleware: Pydantic v2 schema enforcement
├── runtimes/
│   ├── base.py            # BaseRuntimeAdapter ABC + AdapterRegistry
│   ├── local.py           # LocalRuntimeAdapter (deterministic, in-memory)
│   ├── bedrock.py         # AWS Bedrock AgentCore adapter (lazy boto3, env-gated)
│   └── vertex.py          # GCP Vertex AI adapter (lazy google-cloud-aiplatform, env-gated)
├── mcp/
│   ├── decorator.py       # @mcp_tool — JSON Schema generation from type hints
│   ├── registry.py        # McpToolRegistry — discovery + dispatch
│   └── server.py          # Stdio MCP server runner (uses official mcp SDK; extra)
├── a2a/
│   ├── card.py            # AgentCard pydantic model + build_card()
│   └── server.py          # FastAPI router exposing /.well-known/agent-card.json
├── identity/
│   ├── nhi.py             # NonHumanIdentity abstraction
│   ├── token_exchange.py  # OAuth 2.1 / RFC 8693 token exchange
│   └── local_idp.py       # LocalIdPStub for the walking-skeleton default
├── eval/
│   ├── trajectory.py      # Trajectory dataclass + TrajectoryRecorder middleware
│   ├── faithfulness.py    # Built-in claim extraction + entailment scoring
│   ├── ragas_adapter.py   # Optional Ragas integration ([eval] extra)
│   └── runner.py          # Eval driver used by `eap eval`
├── testing/
│   ├── fixtures.py        # make_test_client(), capture_traces(), assert_pii_round_trip()
│   └── responses.py       # CannedResponse helpers for LocalRuntimeAdapter
└── _version.py
```

## 5. Public client API

```python
class EnterpriseLLM:
    def __init__(
        self,
        runtime_config: RuntimeConfig,
        middlewares: list[Middleware] | None = None,
        identity: NonHumanIdentity | None = None,
    ) -> None: ...

    async def generate_text(
        self,
        prompt: str | Messages,
        *,
        schema: type[BaseModel] | None = None,
        **kwargs: Any,
    ) -> Response[T]: ...

    async def stream_text(
        self,
        prompt: str | Messages,
        *,
        schema: type[BaseModel] | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[Chunk]: ...

    async def invoke_tool(self, tool_name: str, args: dict) -> ToolResult: ...

    @property
    def sync(self) -> SyncProxy: ...   # client.sync.generate_text(...) wraps with asyncio.run

    async def aclose(self) -> None: ...
```

Default middleware chain when `middlewares=None`: `[PromptInjection, PiiMasking, Observability, Policy, OutputValidation]`. Users pass an explicit list to reorder, drop, or insert custom middlewares.

`Messages` is a list of `{"role": "system|user|assistant|tool", "content": str | ContentParts}`.

## 6. Middleware contract

```python
class Middleware(Protocol):
    name: str
    async def on_request(self, req: Request, ctx: Context) -> Request: ...
    async def on_response(self, resp: Response, ctx: Context) -> Response: ...
    async def on_stream_chunk(self, chunk: Chunk, ctx: Context) -> Chunk: ...
    async def on_error(self, exc: Exception, ctx: Context) -> None: ...
```

`Context` is a per-call mutable container shared across middlewares:

- `ctx.identity: NonHumanIdentity` — set by the client at request start.
- `ctx.vault: dict[str, str]` — PII re-identification table; scoped to one request, discarded after.
- `ctx.span: opentelemetry.trace.Span | None` — active OTel span if observability middleware ran.
- `ctx.metadata: dict[str, Any]` — free-form scratch space.

### Pipeline execution (onion model)

`on_request` runs left-to-right. The runtime adapter's `generate`/`stream` is invoked at the innermost layer. `on_response` runs right-to-left, so the same middleware that masked PII outbound is the one that unmasks inbound. Errors during request, runtime, or response phases trigger `on_error` on every middleware that already executed `on_request`, in reverse order. The `try/finally` semantics are enforced by the executor.

### Streaming

`stream_text` runs `on_request` once, then each chunk passes through `on_stream_chunk` left-to-right. `OutputValidation` and PII unmasking buffer the stream and emit on completion (or on a per-line boundary if the schema is `str`).

### Failure modes

Explicit, no silent passes:

- `PromptInjectionError` — sanitize detected injection; request never reaches the LLM.
- `PolicyDeniedError` — policy refused; carries the matching rule id.
- `OutputValidationError` — schema validation failed; carries the Pydantic error trace.

Configurable retry-with-correction is **off by default** in the walking skeleton. No hidden retries.

## 7. Runtime adapters

### `BaseRuntimeAdapter` ABC

```python
class BaseRuntimeAdapter(ABC):
    name: ClassVar[str]              # "local" | "bedrock" | "vertex"

    @abstractmethod
    async def generate(self, req: Request) -> RawResponse: ...

    @abstractmethod
    async def stream(self, req: Request) -> AsyncIterator[RawChunk]: ...

    @abstractmethod
    async def list_models(self) -> list[ModelInfo]: ...

    async def aclose(self) -> None: ...
```

### `AdapterRegistry`

Entry-point discoverable (`eap_core.runtimes` group in `pyproject.toml`). Third parties can ship `eap-runtime-azure` later without forking. The client resolves `RuntimeConfig.provider` against the registry.

### `LocalRuntimeAdapter` — the runnable default

Deterministic, dependency-free, used by all unit tests and by every fresh `eap init` project.

Behavior:

1. If a `responses.yaml` is present (project-level `./responses.yaml` or `~/.eap/local_responses.yaml`), match prompts by substring → return canned text.
2. Otherwise return a templated echo: `"[local-runtime] received {n} tokens, model={model}"` plus, when `schema` is provided, a stable JSON instance synthesized via Pydantic field defaults / type-default values.
3. Streaming yields the response in word-sized chunks with a small `asyncio.sleep(0)` so streaming code paths exercise correctly.
4. Token counts reported via `len(prompt.split())` so OTel attributes look real.

### `BedrockRuntimeAdapter` and `VertexRuntimeAdapter`

Both implement the full ABC. Constructors accept the right config (region/profile/model id for Bedrock; project/location/publisher_model for Vertex). They lazy-import `boto3` / `google-cloud-aiplatform` inside `generate()`.

`generate()` and `stream()` raise `NotImplementedError("Wire credentials and replace this stub. See docs/runtimes/bedrock.md")` **unless** `EAP_ENABLE_REAL_RUNTIMES=1` is set, in which case they perform a real call. This means:

- Tests pass without cloud credentials.
- Anyone with credentials gets a one-flag path to real calls without us shipping half-tested cloud code.
- The API surface is fully real — only the network call is gated.

### `RuntimeConfig`

```python
class RuntimeConfig(BaseModel):
    provider: Literal["local", "bedrock", "vertex"] | str
    model: str
    options: dict[str, Any] = Field(default_factory=dict)   # provider-specific (region, project, etc.)
```

## 8. MCP integration

### `@mcp_tool` decorator

Wraps a function or coroutine and:

1. Inspects the signature with `inspect.signature` + type hints.
2. Generates JSON Schema for inputs via Pydantic's `TypeAdapter` (handles `BaseModel`, primitives, `Annotated[..., Field(...)]`, dataclasses).
3. Generates JSON Schema for outputs from the return annotation.
4. Captures the docstring as the tool description.
5. Registers with the global `McpToolRegistry`.

```python
@mcp_tool(name="get_account_balance", description="Look up balance for a customer.")
async def get_account_balance(customer_id: str, currency: Currency = "USD") -> Balance: ...
```

### `McpToolRegistry`

Holds `(name → ToolSpec)`. Two consumers share the same registry:

- `EnterpriseLLM.invoke_tool(name, args)` — in-process dispatch (validate args against schema → run middleware on args → execute → validate return).
- `eap_core.mcp.server.run_stdio()` — wraps the registry as a real MCP stdio server using the official `mcp` SDK (lazy-imported, `[mcp]` extra).

A tool decorated once is callable both in-process by the agent itself and externally by other agents over MCP.

## 9. A2A agent card

Every scaffolded agent ships `configs/agent_card.json` matching the [A2A AgentCard spec](https://github.com/google/A2A): name, description, skills (one entry per `@mcp_tool`), authentication requirements, endpoints.

```python
from eap_core.a2a import build_card, mount_card_route

card = build_card(name="research-agent", skills_from=registry, auth="oauth2.1")
mount_card_route(app, card)   # serves GET /.well-known/agent-card.json on a FastAPI app
```

`build_card` reads from the live `McpToolRegistry` at startup so the card always matches the actual tools — no drift between docs and reality.

## 10. NHI / OAuth 2.1 token exchange

Treat the agent as a workload identity. Every tool call needing auth gets a fresh access token bound to the agent's identity, scoped to the resource being called.

### `NonHumanIdentity`

Holds:

- A long-lived **client credential** — `client_id` + private key for `private_key_jwt` per RFC 7523.
- A **token cache** keyed by `(audience, scope)` with TTL respect.
- A **token-exchange client** implementing RFC 8693.

### Token exchange flow

When a tool call needs a token:

1. Check cache → return if valid.
2. Build the agent-identity assertion (JWT signed with the agent's private key).
3. POST to the configured IdP's token endpoint with `grant_type=urn:ietf:params:oauth:grant-type:token-exchange`, `subject_token=<assertion>`, `audience=<tool's resource>`, `scope=<requested scopes>`.
4. Cache and return.

### `LocalIdPStub` — walking-skeleton default

Issues self-signed JWTs in-memory and validates them locally. The `OIDCTokenExchange` class implements the real RFC 8693 wire format and points at `LocalIdPStub` by default. Pointing it at Okta / Auth0 / Cognito is config-only.

The `Policy` middleware reads `ctx.identity` and includes it in the principal for Cedar evaluation. The MCP dispatcher pulls a token before invoking any tool whose `ToolSpec` declares `requires_auth=True`.

## 11. Policy enforcement

### Default: minimal JSON-based evaluator

Mirrors Cedar's principal/action/resource/condition shape. Rules in `configs/policy.json`:

```json
{
  "version": "1",
  "rules": [
    {"id": "allow-reads", "effect": "permit", "principal": "*", "action": ["read"], "resource": "*"},
    {"id": "deny-writes-without-role", "effect": "forbid", "principal": "*", "action": ["write", "transfer"], "resource": "*", "unless": {"principal_has_role": "operator"}}
  ]
}
```

Evaluator: ~100 LOC. First matching `forbid` wins; otherwise `permit` if any matches; default deny. Returns `(decision, matched_rule_id)`.

### Optional: `cedarpy`

Behind the `[policy-cedar]` extra. Same `PolicyMiddleware` surface; swaps the evaluator. Cedar `.pol` files supported when this path is wired.

## 12. Eval framework

### Trace capture

`TrajectoryRecorder` middleware (default-on in eval mode, opt-in otherwise) writes one JSONL record per request to `.eap/traces/<run_id>.jsonl`.

```python
@dataclass
class Trajectory:
    request_id: str
    steps: list[Step]                # one per LLM call or tool invocation, in order
    final_answer: str
    retrieved_contexts: list[str]    # docs/passages middlewares stashed in ctx
```

The recorder reuses OTel span data — no parallel observability stack.

### Faithfulness scorer

Built-in (no Ragas required for the default path):

1. **Claim extraction:** prompt the configured judge LLM (a separate `EnterpriseLLM` with eval middlewares stripped to avoid recursion) to break the answer into atomic factual claims.
2. **Entailment check:** for each claim, ask the judge whether the retrieved context supports it (`SUPPORTED | CONTRADICTED | NOT_FOUND`).
3. **Score:** `supported_count / total_claims`. Returns `FaithfulnessResult` with the per-claim breakdown so failures are debuggable.

```
Faithfulness = supported_claims / total_claims
```

The judge LLM is configurable (`EvalConfig.judge_runtime`):

- Defaults to `LocalRuntimeAdapter`, which returns deterministic stub verdicts based on substring overlap → eval tests are reproducible.
- Real providers used in real runs.

### Ragas adapter

`eap_core.eval.ragas_adapter` converts a `Trajectory` to Ragas's `EvaluationDataset` format. Behind the `[eval]` extra. Same shape applies for DeepEval if added later.

### `eap eval` command

```
eap eval --dataset PATH [--scorers faithfulness,answer_relevance] \
                       [--judge local|bedrock|vertex] \
                       [--threshold 0.7] \
                       [--report html|json|junit]
```

`golden_set.json`:
```json
[
  {"id": "case-001", "input": "...", "expected_contexts": ["..."], "expected_answer_substrings": ["..."]}
]
```

Runs each case through the agent (via the user's `agent.py` entry point), scores, emits a report (HTML for review, JSON for CI artifacts, JUnit XML for test runners), exits non-zero if any score drops below `--threshold`.

## 13. CLI commands

`eap` is a Click app. Each command is a thin handler delegating to a pure-Python function in `eap_cli.scaffolders` (testable without the CLI). Templates are Jinja2 files under `eap_cli/templates/{init,research,transactional,tool}/`. Each template directory has a `template.toml` describing variables and post-render hooks (e.g. `uv lock`).

### `eap init [DIR] [--name NAME] [--runtime local|bedrock|vertex] [--force]`

Lays down the project skeleton:

```
<project>/
├── .claude.md            # context for AI coding agents
├── pyproject.toml        # uv-managed, pulls eap-core + the right runtime extra
├── agent.py              # minimal but RUNNABLE on local runtime
├── tools/
│   └── example_tool.py   # one @mcp_tool stub
├── configs/
│   ├── policy.json       # default policy
│   └── agent_card.json   # auto-generated by build_card
├── tests/
│   └── golden_set.json   # one example eval case
├── responses.yaml        # canned responses for LocalRuntimeAdapter
├── .gitignore
└── README.md
```

If `DIR` is omitted, scaffolds in CWD. Refuses to overwrite unless `--force`.

### `eap create-agent --name NAME --template research|transactional [--state memory|sqlite]`

- **research**: retrieval-style agent with a `search_docs` MCP tool stub, multi-step reasoning loop, returns a structured answer with citations.
- **transactional**: action-style agent with `get_account` + `transfer_funds` tools, explicit policy gates on the write tool, idempotency-key handling.
- `--state memory` (default) uses an in-process dict; `--state sqlite` uses a SQLite-backed session store. Both implement the same `SessionStore` Protocol.
- Generates the agent file and updates `agent_card.json`.

### `eap create-tool --name NAME --mcp [--auth-required]`

Generates `tools/<name>.py` with:

- Typed function signature stub.
- `@mcp_tool` applied (auto schema generation).
- PII masking middleware hook pre-wired (input goes through the same chain as LLM calls).
- If `--auth-required`: sets `requires_auth=True` and adds a policy rule template for the action.

### `eap eval --dataset PATH [...]`

As described in §12.

### `eap deploy --runtime aws|gcp [--bucket BUCKET | --service NAME] [--dry-run]`

Walking-skeleton scope: **packaging only** by default.

- **aws**: produces `dist/agent.zip` matching Lambda / Bedrock AgentCore layout (handler-shaped wrapper, requirements frozen via `uv pip compile`). Prints the `aws s3 cp` command. With `--bucket` and `EAP_ENABLE_REAL_DEPLOY=1`, executes the upload via `boto3`.
- **gcp**: produces `dist/agent/` with a `Dockerfile` and `cloudbuild.yaml` for Cloud Run. Prints the `gcloud run deploy` command. With `--service` and the env flag, executes the deploy.
- `--dry-run` prints the plan without writing anything.

Same gating logic as runtime adapters: packaging is safe in CI, deploy is opt-in.

### Scaffolded `.claude.md`

The on-ramp for the next AI coding agent that opens the project. Tells them:

- Where business logic lives (`agent.py` only).
- That cross-cutting concerns are owned by `eap_core` middleware — don't reimplement.
- How to add a tool (`eap create-tool`, not handwritten).
- That `EAP_ENABLE_REAL_RUNTIMES=1` exists but is opt-in.

## 14. Testing strategy

### Unit tests (fast, default)

- One test file per module. Heavy use of in-memory fakes — `LocalRuntimeAdapter`, `LocalIdPStub`, `InMemorySessionStore`.
- Middleware chain has property-style tests: any composition order produces a valid pipeline; PII tokens that go in come back out; OTel spans always close even on error.
- Coverage target: **90%+** on `eap_core`, enforced via `pytest --cov` failing under threshold.

### Integration tests (still no network)

- E2E flow: `EnterpriseLLM` with full middleware chain → `LocalRuntimeAdapter` → assertions on the trace JSONL.
- A2A: spin up the FastAPI router via `httpx.ASGITransport`, GET `/.well-known/agent-card.json`, validate against the A2A schema.
- MCP: launch the stdio server in a subprocess, send `tools/list` + `tools/call` over stdio, assert responses.
- CLI: run `eap init` into a tmpdir, then run the resulting `agent.py` as a subprocess and assert its stdout. Catches scaffolding-vs-library drift.

### Optional-extra tests

- `tests/extras/test_presidio.py` — skipped without `[pii]`. Runs Presidio on a known PII string, asserts tokenization round-trips through the vault.
- Same pattern for OTel-with-real-exporter, cedarpy, ragas, mcp.
- CI matrix has one job per extra; local `pytest` without extras stays green.

### Cloud-adapter tests

- Default: assert `NotImplementedError` with the helpful message.
- With `EAP_ENABLE_REAL_RUNTIMES=1` + creds: a `@pytest.mark.cloud` smoke test makes one real call. Skipped in normal CI; runs in a separate scheduled workflow with secrets.

### `eap_core.testing` module

Ships with the package because the user's `agent.py` needs to be testable too. The `tests/` template uses these helpers:

- `make_test_client()` — pre-wired `EnterpriseLLM` with `LocalRuntimeAdapter` and a no-op identity.
- `assert_pii_round_trip(text, processed)` — fixture for PII tests.
- `capture_traces()` — context manager collecting OTel spans for assertions.

## 15. CI and tooling

GitHub Actions workflow with three jobs:

1. **lint** — `ruff check` + `ruff format --check` + `mypy --strict` on `src/`.
2. **test-core** — `pytest` with coverage gate, no extras.
3. **test-extras** — matrix over `[pii, otel, mcp, a2a, eval, policy-cedar]`, runs matching `tests/extras/`.

Cloud-call workflow is separate, manual-trigger or nightly.

Tooling:

- **uv** for env, lock, workspace.
- **ruff** for lint + format.
- **mypy** strict on `src/`, lenient on `tests/`.
- **pytest** + `pytest-asyncio` (auto mode).
- **Pydantic v2** for all data shapes.

## 16. Packages and extras

`eap-core` extras:

| Extra | Pulls | What it enables |
|---|---|---|
| `pii` | `presidio-analyzer`, `presidio-anonymizer`, `spacy` | Real Presidio PII masking |
| `otel` | `opentelemetry-sdk`, `opentelemetry-exporter-otlp` | Real OTel exporters (default uses console) |
| `aws` | `boto3` | Bedrock adapter real calls + `eap deploy --runtime aws` upload |
| `gcp` | `google-cloud-aiplatform` | Vertex adapter real calls |
| `mcp` | `mcp` (official SDK) | `eap_core.mcp.server.run_stdio()` |
| `a2a` | `fastapi`, `uvicorn` | A2A card HTTP serving |
| `eval` | `ragas`, `datasets` | Ragas adapter |
| `policy-cedar` | `cedarpy` | Cedar `.pol` evaluation |

Default install (no extras):

- `eap-core` pulls: `pydantic>=2`, `httpx`, `pyjwt[crypto]`, `pyyaml`. Nothing else.
- `eap-cli` pulls: `eap-core` + `click` + `jinja2`.

That's the entire base footprint. Heavyweight deps (Presidio, OTel exporters, boto3, google-cloud-aiplatform, official `mcp` SDK, `fastapi`, `ragas`, `cedarpy`) are gated behind the extras above.

## 17. Out-of-scope (explicit)

- Live cloud calls without `EAP_ENABLE_REAL_RUNTIMES=1`.
- Web UI / management console.
- Multi-tenant SaaS deployment.
- Vector store / retrieval pipeline (users wire their own; RAG context is just data flowing through the chain).
- Marketplace, plugin store, billing integrations.
- Production-grade Kubernetes operators / IaC.

## 18. Success criteria

The walking skeleton is "done" when:

1. `pip install eap-core eap-cli` works and brings nothing heavy.
2. `eap init my-agent && cd my-agent && python agent.py` produces visible output via the local runtime, no further setup.
3. `pytest` from the workspace root passes green with `>= 90%` coverage on `eap_core`.
4. `eap eval --dataset tests/golden_set.json` runs and emits a faithfulness report.
5. `EAP_ENABLE_REAL_RUNTIMES=1` + AWS credentials exercises a real Bedrock call via the smoke test (manual workflow).
6. Adding a new tool is one `eap create-tool` invocation and produces a working MCP-registered tool with PII + policy + auth wiring.
7. The scaffolded `.claude.md` is informative enough that the next AI coding agent that opens the project doesn't reimplement cross-cutting concerns.
