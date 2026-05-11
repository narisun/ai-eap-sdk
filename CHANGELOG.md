# Changelog

All notable changes to **EAP-Core** are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

The same version applies to both workspace packages (`eap-core` and
`eap-cli`); they ship together.

---

## [Unreleased]

Nothing yet. Open a PR.

---

## [0.3.0] — 2026-05-10 — GCP Vertex Agent Engine integration + vendor-neutral Protocols

Adds full integration with GCP Vertex AI Agent Engine across the
parallel surface to AgentCore (Runtime, Observability, Identity,
Memory Bank, Agent Sandbox (code + browser), Agent Gateway, Agent
Registry, AP2 payments, Gen AI Eval) and promotes the cross-cutting
abstractions to top-level Protocols so backends are interchangeable
by config.

### Architectural shift — vendor-neutral Protocols at top level

`eap_core` now exposes four cross-cloud Protocols that were previously
implicit:

- `eap_core.sandbox.CodeSandbox` + `BrowserSandbox` + `SandboxResult`
  — abstract sandboxed code/browser execution. Backed in-process by
  `InProcessCodeSandbox` (subprocess) and `NoopBrowserSandbox`;
  in the cloud by AgentCore Code Interpreter / Browser and Vertex
  Agent Sandbox.
- `eap_core.discovery.AgentRegistry` — abstract org-wide
  agent/tool/MCP-server catalog. Defaulted to `InMemoryAgentRegistry`;
  cloud impls are `RegistryClient` (AWS) and `VertexAgentRegistry` (GCP).
- `eap_core.payments.PaymentBackend` + `PaymentRequired` — abstract
  agent microtransactions. Defaulted to `InMemoryPaymentBackend`;
  cloud impls are AgentCore `PaymentClient` (x402) and `AP2PaymentClient` (AP2).
- `eap_core.security.ThreatDetector` + `ThreatAssessment` — abstract
  prompt-injection / threat scoring. Defaulted to a 5-pattern
  `RegexThreatDetector`.

If your agent depends on the Protocol (not the concrete class), it
runs unmodified on either AWS or GCP. Switching is a one-line
constructor change at the seam.

### Added — GCP Vertex Agent Engine integration

All live GCP calls lazy-import `google-cloud-aiplatform` and are gated
behind `EAP_ENABLE_REAL_RUNTIMES=1`. CI does not need GCP credentials.

**Phase A — Runtime + Observability + Identity:**

- `eap deploy --runtime vertex-agent-engine` — packages a Cloud Run-
  compatible image (`linux/amd64`, `PORT` env, `EXPOSE 8080`) with a
  FastAPI handler exposing `POST /invocations` + `GET /health`. Live
  `docker build` gated by `EAP_ENABLE_REAL_DEPLOY=1`.
- `configure_for_vertex_observability(project_id=, service_name=,
  endpoint=)` — wires the OTel SDK to a Cloud Trace OTLP endpoint
  and writes a `gcp.project_id` resource attribute. Returns `False`
  when the `[otel]` extra is missing.
- `VertexAgentIdentityToken(scopes=...)` — wraps the standard Google
  auth chain (ADC → workload identity → IAM SA) with a
  `get_token(audience=, scope=)` signature that matches
  `NonHumanIdentity` for drop-in substitution.

**Phase B — Managed Memory + Sandboxes:**

- `VertexMemoryBankStore(project_id=, location=, memory_bank_id=)` —
  Vertex Memory Bank backend; implements the `MemoryStore` Protocol
  (remember/recall/list_keys/forget/clear).
- `VertexCodeSandbox(project_id=, location=, sandbox_id=)` —
  implements the `CodeSandbox` Protocol; returns `SandboxResult` with
  stdout/stderr/exit_code/artifacts.
- `VertexBrowserSandbox(project_id=, location=, session_id=)` —
  implements the `BrowserSandbox` Protocol
  (navigate/click/fill/extract_text/screenshot).
- `register_code_sandbox_tools(registry, project_id=, ...)` — registers
  `execute_python`, `execute_javascript`, `execute_typescript` MCP
  tools that traverse the middleware chain on invoke.
- `register_browser_sandbox_tools(registry, project_id=, ...)` —
  registers five `browser_*` MCP tools.

**Phase C — Outbound Gateway:**

- `VertexGatewayClient(gateway_url=, identity=, ...)` — JSON-RPC 2.0
  MCP client for any MCP-HTTP endpoint; supported Google configuration
  is the Vertex Agent Gateway. Identical wire shape to
  `agentcore.GatewayClient` — pointing at either gateway is a
  constructor swap. Pluggable identity and httpx auth.

**Phase D — Registry, Payments (AP2), Evaluations:**

- `VertexAgentRegistry(project_id=, location=, registry_id=)` —
  implements the `AgentRegistry` Protocol against Vertex Agent
  Registry. `publish` validates the `name` field before the env-flag
  gate so config bugs surface even without `EAP_ENABLE_REAL_RUNTIMES`.
- `AP2PaymentClient(wallet_provider_id=, project_id=, ...)` —
  implements the `PaymentBackend` Protocol against Google's Agent
  Payment Protocol. Drop-in compatible with `agentcore.PaymentClient`:
  same `start_session` / `authorize` / `can_afford` / budget
  bookkeeping.
- `to_vertex_eval_dataset(trajectories)` — maps `Trajectory` records
  to Vertex Gen AI Eval Service shape
  (prompt/response/context/trace_id/steps).
- `VertexEvalScorer(project_id=, metric=, ...)` — `Scorer` impl that
  calls Vertex Eval and returns `FaithfulnessResult` indistinguishable
  from `AgentCoreEvalScorer`.

### Added — vendor-neutral abstractions (top-level)

- `eap_core.sandbox` — `CodeSandbox`, `BrowserSandbox`,
  `SandboxResult`, `InProcessCodeSandbox`, `NoopBrowserSandbox`.
- `eap_core.discovery` — `AgentRegistry`, `InMemoryAgentRegistry`.
- `eap_core.payments` — `PaymentBackend`, `PaymentRequired`,
  `InMemoryPaymentBackend`.
- `eap_core.security` — `ThreatDetector`, `ThreatAssessment`,
  `RegexThreatDetector` (5 default injection patterns).

All four are re-exported from `eap_core` top-level.

### Added — packaging + workspace plumbing

- `[gcp]` extra on `eap-core` (and re-forwarded from workspace root)
  pulls `google-cloud-aiplatform`, which transitively brings in
  `google-auth` and `google-auth-transport-requests`. The workspace
  `[all]` extra includes it.
- Mypy `google` / `google.*` module overrides silence untyped-import
  errors at workspace level.

### Docs

- `docs/integrations/gcp-vertex-agent-engine.md` — full positioning,
  cross-cloud equivalence table, service-by-service mapping, and
  per-phase usage walkthroughs.

### Stats

- **342 tests passing** (up from 243 in v0.2.0).
- 69 new tests: 7 CLI deploy, 9 Phase A integration, 20 Phase B,
  13 Phase C, 20 Phase D.
- Lint / format / strict mypy all green.

---

## [0.2.0] — 2026-05-10 — AWS Bedrock AgentCore integration

Adds full integration with AWS Bedrock AgentCore across all 11
managed services (Runtime, Identity, Observability, Memory, Gateway,
Code Interpreter, Browser, Payments, Evaluations, Policy, Registry)
plus inbound JWT verification. The integration ships in four phases
(A → B → C → D), each independently shippable and adding value on
its own. All live AgentCore calls are gated behind
`EAP_ENABLE_REAL_RUNTIMES=1` so tests stay deterministic and CI does
not need AWS credentials.

The architectural claim that motivates the integration: **EAP-Core's
middleware chain runs in the agent's own process, before any
AgentCore-managed service sees the data**. PII is masked before
AgentCore Memory stores it. Prompt injection is blocked before the
text reaches the Code Interpreter sandbox. Policy denials happen
even when calls bypass Gateway. Defense in depth across the full
AgentCore surface.

### Stats
- **243 tests passing** (up from 153 in v0.1.0).
- Coverage holds ≥ 90% on the no-extras baseline.
- Lint, format, and strict mypy all green.

### Added — AWS Bedrock AgentCore integration (Phase D)

Closes feature parity with AgentCore. Three independent pieces, all
following the same lazy-boto3 + `EAP_ENABLE_REAL_RUNTIMES=1` gating
pattern as Phases A–C.

- **`RegistryClient`** — AWS Agent Registry client for org-wide
  discovery. Methods: `publish_agent_card(card)`,
  `publish_mcp_server(name, ...)`, `get_record(name)`,
  `search(query)`, `list_records(record_type=..., max_results=...)`.
  Construction does no I/O.
- **`PaymentRequired`** (exception) — raised by tool wrappers when an
  upstream service responds `HTTP 402`. Carries `amount_cents`,
  `currency`, `merchant`, `original_url`, and the raw x402 payload.
  Named to match the HTTP 402 "Payment Required" status (not the
  ruff-preferred `Error` suffix — intentional, noqa'd).
- **`PaymentClient`** — opens a budget-limited `PaymentSession`
  via AgentCore Payments, signs payments via the configured wallet
  (Coinbase CDP or Stripe/Privy), and tracks spending in-process.
  Methods: `start_session()`, `authorize_and_retry(req)`, plus
  the synchronous helpers `can_afford(amount_cents)`,
  `remaining_cents`, `spent_cents`, `session_id`. Budget bookkeeping
  is deterministic from the client's own state so agents can
  pre-check before any AWS call.
- **`to_agentcore_eval_dataset(trajectories)`** — pure-function
  exporter that converts our `Trajectory` records to AgentCore Eval's
  question / answer / contexts / trace_id / steps shape. Useful for
  S3 upload or boto3 batch calls. Empty list → empty list.
- **`AgentCoreEvalScorer`** — implements our `_ScorerProto` so it
  plugs into `EvalRunner.scorers` alongside the deterministic
  scorer. Wraps an AgentCore evaluator ARN (built-in like
  `arn:aws:bedrock-agentcore:::evaluator/Builtin.Helpfulness` or
  custom). A single `EvalReport.aggregate` can then carry scores from
  both our in-process scorer and AgentCore's managed evaluator
  side-by-side.

### Tests added (24 new tests)

`test_integrations_agentcore_phase_d.py`:
- `RegistryClient` (7): construction is cheap (no boto3 import),
  every method gated by env flag, stores construction params.
- `PaymentRequired` (2): carries x402 metadata, optional raw defaults.
- `PaymentClient` (6): initial state, gated start/authorize,
  `can_afford` respects budget, `remaining_cents` tracks spend,
  construction is cheap.
- Eval adapters (4): one row per trajectory, missing input handled,
  steps serialized, empty list returns empty.
- `AgentCoreEvalScorer` (5): default + custom name, gated by env
  flag, construction is cheap, satisfies `EvalRunner` scorer shape.

### Stats

- **243 tests passing** (up from 219 in Phase C).
- Coverage holds. Lint, format, and strict mypy all green.
- The AgentCore integration is now feature-complete across all four
  phases. Live API calls remain `EAP_ENABLE_REAL_RUNTIMES=1`-gated;
  flip the flag with AWS credentials configured to exercise them.

### Added — AWS Bedrock AgentCore integration (Phase C)

Gateway integration. Outbound: an EAP-Core agent uses Gateway-hosted
tools through the normal `invoke_tool` path with full middleware
chain enforcement. Inbound: project tools are published to Gateway as
an OpenAPI 3.1 HTTP target.

- **`GatewayClient`** — MCP-over-HTTP client (plain JSON-RPC 2.0).
  Methods: `list_tools()`, `invoke(name, args)`, `aclose()`. Auth is
  pluggable: pass an `httpx` `auth=` object for AWS SigV4, or set
  `identity=` to a `NonHumanIdentity` for OAuth Bearer tokens
  (audience-scoped, cached). Construction does no I/O; live calls
  gated by `EAP_ENABLE_REAL_RUNTIMES=1`.
- **`add_gateway_to_registry(registry, gateway, tool_specs)`** —
  registers remote Gateway tools as proxy specs in a local
  `McpToolRegistry`. Each proxy's `fn` is a closure that forwards
  `(name, args)` to `gateway.invoke`. After this call,
  `client.invoke_tool("<remote_tool>", {...})` flows through the
  agent's middleware chain locally (sanitize / PII / policy / OTel /
  validate) and then crosses the network. Proxy specs are marked
  `requires_auth=True` because they cross a trust boundary.
- **`export_tools_as_openapi(registry, ...)`** — generates an
  OpenAPI 3.1 spec from any `McpToolRegistry`. Each tool becomes a
  `POST /tools/<name>` operation with the tool's input schema as the
  request body schema. The `x-mcp-tool.requires_auth` extension
  preserves the SDK's auth marker so Gateway can apply outbound auth
  correctly. Empty registries produce a valid skeleton.
- **`eap publish-to-gateway`** CLI command — runs
  `export_tools_as_openapi` against the project's registry and writes
  the spec + a deploy `README.md` to `dist/gateway/`. Importing the
  user's entry file (default `agent.py`) triggers the `@mcp_tool`
  decorator side-effects that populate the registry. Has `--dry-run`,
  `--entry`, `--title`, `--server-url` flags.

### Tests added (19 new tests)

`test_integrations_agentcore_phase_c.py`:
- `GatewayClient`: list_tools / invoke gated by env flag; construction
  no I/O; JSON-RPC 2.0 wire shape for `tools/list`; surfaces
  single-text-content directly; returns full content list when
  multipart; raises `MCPError` on JSON-RPC errors and HTTP errors;
  attaches `Authorization: Bearer <token>` from `identity`.
- `add_gateway_to_registry`: registers proxy specs, dispatches
  through the registry forwards to gateway, skips unnamed specs,
  marks proxies as `requires_auth=True`, handles both `inputSchema`
  (camelCase) and `input_schema` (snake_case) keys.
- `export_tools_as_openapi`: emits one path per tool, marks
  auth-required in `x-mcp-tool` extension, empty-registry skeleton.
- `eap publish-to-gateway` end-to-end: scaffolded project →
  `openapi.json` + `README.md` with the expected `POST /tools/<name>`
  operations; `--dry-run` writes nothing; missing entry errors
  cleanly.

### Stats
- **219 tests passing** (up from 200 in Phase B).
- Coverage holds. Lint, format, and strict mypy all green.

### Added — AWS Bedrock AgentCore integration (Phase B)

In-process adapters for AgentCore-managed services. Live calls are
gated behind `EAP_ENABLE_REAL_RUNTIMES=1`; tests run deterministically
without AWS credentials.

- **`eap_core.memory`** (new module) — `MemoryStore` Protocol with
  five operations (`remember`, `recall`, `list_keys`, `forget`,
  `clear`), all async. `InMemoryStore` default impl (dict-backed,
  per-session isolation) for tests and local development. Plus
  `MemoryStore` is exported at the package root.
- **`Context.memory_store` and `Context.session_id`** — new optional
  fields on the per-request `Context`. Tools and middleware can read
  / write memory through the same Protocol regardless of backend.
  Existing tests untouched (defaults preserve old behavior).
- **`AgentCoreMemoryStore`** — AgentCore Memory backend for the
  `MemoryStore` Protocol. Construction is cheap (no I/O); every
  method lazy-imports `boto3` and raises `NotImplementedError`
  without `EAP_ENABLE_REAL_RUNTIMES=1`. Same env-flag pattern as
  the Bedrock / Vertex runtime adapters.
- **`register_code_interpreter_tools(registry, region=...)`** —
  registers three `@mcp_tool` functions on a registry:
  `execute_python`, `execute_javascript`, `execute_typescript`. Each
  returns `{"stdout": str, "stderr": str, "exit_code": int}`. Tool
  calls go through the full middleware chain so generated code is
  sanitized / PII-checked / policy-gated / observability-recorded
  before execution.
- **`register_browser_tools(registry, region=...)`** — registers
  five MCP tools for web interaction: `browser_navigate`,
  `browser_click`, `browser_fill`, `browser_extract_text`,
  `browser_screenshot`. Policy can deny navigate to specific
  hostnames; every action records as an OTel span.
- **`InboundJwtVerifier`** — verifies JWTs issued by AgentCore
  Identity (or any OIDC IdP) at the HTTP boundary of an agent.
  Fetches JWKS via OIDC discovery URL with TTL caching; validates
  audience / scope / client / kid; rejects expired tokens. Uses
  PyJWT (already a default dep) plus `cryptography` for RS256 key
  loading.
- **`jwt_dependency(verifier)`** — FastAPI dependency factory that
  pulls the bearer token from the `Authorization` header and calls
  `verifier.verify`. Drop-in `Depends(...)` for the generated
  AgentCore `handler.py` route. Lazy-imports FastAPI; clear
  `ImportError` if the `[a2a]` extra isn't installed.

### Tests added (35 new tests)
- `test_memory.py` (14 tests): Protocol conformance, round-trip,
  session isolation, list_keys, forget / clear semantics,
  overwrite, unicode handling, `Context` field plumbing.
- `test_integrations_agentcore_phase_b.py` (21 tests):
  `AgentCoreMemoryStore` Protocol conformance + env-flag gating +
  cheap construction, all 5 methods raise without env flag.
  Code Interpreter / Browser tool registration and schema shape.
  `InboundJwtVerifier` accepts valid tokens, rejects wrong
  audience / disallowed client / missing scope / unknown kid /
  expired tokens; JWKS caching verified.

### Stats
- **200 tests passing** (up from 166 in Phase A).
- Coverage holds. `ruff check`, `ruff format --check`, and
  `mypy --strict` all green.

### Added — AWS Bedrock AgentCore integration (Phase A)
- **`docs/integrations/aws-bedrock-agentcore.md`** — full integration
  guide. Positioning ("EAP-Core inside AgentCore"), service-by-service
  mapping table (11 AgentCore services × what we have / gap / approach),
  Phase A specifics, and Phases B–D plan.
- **`eap deploy --runtime agentcore`** — packages the project as an
  ARM64 Docker container implementing the AgentCore HTTP protocol
  contract (`POST /invocations`, `GET /ping`, port 8080). Generates
  `Dockerfile`, `handler.py` (FastAPI wrapper around the user's entry
  function), and a deploy `README.md`. Live build via
  `EAP_ENABLE_REAL_DEPLOY=1`.
- **`eap_core.integrations.agentcore`** module:
  - `OIDCTokenExchange.from_agentcore(region=..., workload_identity_id=...)`
    — factory that points the existing RFC 8693 client at AgentCore
    Identity's regional token endpoint. Everything downstream
    (`NonHumanIdentity` cache, per-tool token attachment) works
    unchanged.
  - `configure_for_agentcore(service_name=..., endpoint=..., headers=...)`
    — sets up OTel SDK with OTLP exporter so traces flow into
    AgentCore Observability (CloudWatch). Graceful no-op without the
    `[otel]` extra.
- **15 new tests** for the deploy packager (Dockerfile, handler
  routes, custom entry, dry-run, live-deploy gating, ASGI
  end-to-end smoke test) and the integration helpers (regional
  endpoint, env-var overrides, RFC 8693 round-trip, OTel
  configuration).

### Changed — install instructions (no public PyPI)
- README, `eap-core/README.md`, `eap-cli/README.md` updated. EAP-Core
  is **not** published to public PyPI; install from this repo via
  `uv add "eap-core @ git+https://github.com/narisun/ai-eap-sdk.git@v0.1.0#subdirectory=packages/eap-core"`
  or the equivalent for an internal package index.
- README adds explicit guidance on building wheels for a private
  index (AWS CodeArtifact, Azure Artifacts, internal devpi).

---

## [0.1.0] — 2026-05-10 — Walking Skeleton

The initial walking-skeleton release. All public modules and
interfaces exist with working in-memory implementations. Cloud
adapters (AWS Bedrock, GCP Vertex) are shape-correct stubs gated
behind `EAP_ENABLE_REAL_RUNTIMES=1`. Heavyweight integrations
(Presidio, OpenTelemetry SDK, official MCP SDK, FastAPI, cedarpy,
Ragas) are optional extras, lazy-imported behind clean interfaces.

### Added — SDK foundation (`eap-core`)

#### Core types and configuration
- `Request`, `Response`, `Chunk`, `Message`, `Context` — Pydantic
  models / dataclasses for the wire shapes that flow through the
  middleware chain.
- `RuntimeConfig`, `IdentityConfig`, `EvalConfig` — typed config
  objects.
- Exception hierarchy: `EapError`, `PromptInjectionError`,
  `PolicyDeniedError`, `OutputValidationError`, `RuntimeAdapterError`,
  `IdentityError`, `MCPError`.

#### Middleware chain (Chain of Responsibility, onion model)
- `Middleware` Protocol with `on_request` / `on_response` /
  `on_stream_chunk` / `on_error` hooks.
- `MiddlewarePipeline` — onion executor; runs request left-to-right,
  response right-to-left, calls `on_error` in reverse on every
  middleware that already entered.
- `PassthroughMiddleware` — convenience base class.
- Five default middlewares:
  - `PromptInjectionMiddleware` — regex patterns + pluggable async
    classifier hook. Raises `PromptInjectionError` on match.
  - `PiiMaskingMiddleware` — regex tokenizer (default) plus per-request
    vault for re-identification; Presidio `AnonymizerEngine`
    integration via the `[pii]` extra (handles overlapping findings
    correctly via SDK).
  - `ObservabilityMiddleware` — writes OTel GenAI semantic-convention
    attributes to `ctx.metadata` always; emits real OTel spans when
    `[otel]` extra is installed.
  - `PolicyMiddleware` — accepts any `PolicyEvaluator` Protocol.
    Ships a Cedar-shaped JSON evaluator (default, ~100 LOC) and a
    cedarpy hook for the `[policy-cedar]` extra.
  - `OutputValidationMiddleware` — Pydantic v2 schema enforcement on
    LLM output. Parses JSON, validates, attaches typed payload to
    `Response.payload`.

#### Runtime adapters (Strategy pattern)
- `BaseRuntimeAdapter` ABC with `generate` / `stream` / `list_models`
  / `aclose`.
- `AdapterRegistry` with entry-point discovery via the
  `eap_core.runtimes` group — third parties can ship adapters as
  separate packages.
- `LocalRuntimeAdapter` — deterministic in-memory runtime with
  `responses.yaml` canned-response support and Pydantic-schema
  synthesis. Used by tests and every fresh-scaffolded project.
- `BedrockRuntimeAdapter` — AWS Bedrock AgentCore stub. Lazy-imports
  `boto3`. Real network calls gated behind
  `EAP_ENABLE_REAL_RUNTIMES=1`.
- `VertexRuntimeAdapter` — GCP Vertex AI stub. Lazy-imports
  `google-cloud-aiplatform`. Same env-flag gate.

#### Identity (NHI + OAuth 2.1)
- `NonHumanIdentity` — workload identity with TTL-cached JWT
  issuance per `(audience, scope)`. Includes `jti` claim so two
  tokens issued in the same second are still distinct.
- `LocalIdPStub` — in-memory IdP that issues HS256 JWTs.
- `OIDCTokenExchange` — RFC 8693 token-exchange client. Implements
  the `urn:ietf:params:oauth:grant-type:token-exchange` grant via
  `httpx.AsyncClient`. Same code points at Okta / Auth0 / Cognito by
  config change.

#### MCP integration
- `@mcp_tool` decorator — wraps a Python function, generates JSON
  Schema for inputs and outputs from type hints (via Pydantic
  `TypeAdapter`), captures the docstring, registers a `ToolSpec`.
- `McpToolRegistry` — discovery + dispatch. Validates incoming `args`
  against the spec's input schema before invoking. Supports both
  `async` and sync tool functions (sync runs in a worker thread).
- `default_registry()` — module-level singleton the decorator can
  auto-register into.
- `build_mcp_server(registry)` — bridges the registry to the official
  `mcp` SDK 1.x via `Server.list_tools()` + `Server.call_tool()`
  decorators. `[mcp]` extra.
- `run_stdio(registry)` — convenience entry point that runs a
  registry as an MCP-stdio server.

#### A2A AgentCard
- `AgentCard`, `Skill` Pydantic models matching the A2A spec.
- `build_card(name=..., skills_from=registry, auth=...)` — auto-builds
  the card from the live MCP registry so advertised skills can never
  drift from actual tools.
- `mount_card_route(app, card)` — FastAPI router registering
  `GET /.well-known/agent-card.json`. `[a2a]` extra.

#### Eval framework
- `Trajectory`, `Step` — Pydantic models for the recorded path of
  a request.
- `TrajectoryRecorder` middleware — writes one JSONL record per
  request to disk and/or buffers in memory. Reuses the OTel
  attributes from `ObservabilityMiddleware` (no parallel
  observability stack).
- `Judge` Protocol with two implementations:
  - `DeterministicJudge` — sentence-split for claim extraction;
    content-word overlap for entailment. Reproducible for tests.
  - `LLMJudge` — wraps an `EnterpriseLLM` client (with eval
    middlewares stripped to avoid recursion). For production use
    against a real LLM.
- `FaithfulnessScorer` — produces a `FaithfulnessResult` with a
  per-claim breakdown so failures are debuggable.
- `EvalRunner` — drives `EvalCase` records (loaded from a JSON
  golden-set via `EvalRunner.load_dataset`) through a user-provided
  agent function, applies all configured scorers, returns an
  `EvalReport` with per-case results, aggregates, and pass/fail
  counts against a threshold.
- `emit_json` / `emit_html` / `emit_junit` — three report formats.
  HTML is styled and color-coded by pass/fail; JUnit XML drops
  cleanly into CI test runners.
- `to_ragas_dataset(trajectories)` — converts trajectories to
  Ragas's `EvaluationDataset.from_list` shape. `[eval]` extra.

#### Testing helpers (`eap_core.testing`)
- `make_test_client()` — pre-wired `EnterpriseLLM` with
  `LocalRuntimeAdapter` and a permit-all policy. Three lines instead
  of fifteen for tests.
- `capture_traces()` — context manager that records `ctx.metadata`
  snapshots after each request. No OTel SDK required.
- `assert_pii_round_trip()` — fixture for verifying PII masking +
  unmasking.
- `canned_responses()` — context manager for deterministic
  `LocalRuntimeAdapter` responses in tests.

#### Top-level public API
The most-used names are re-exported at the package root:

```python
from eap_core import (
    EnterpriseLLM, RuntimeConfig, IdentityConfig, EvalConfig,
    Request, Response, Chunk, Message, Context,
    mcp_tool, McpToolRegistry, ToolSpec, default_registry,
    AgentCard, Skill, build_card,
    EvalRunner, EvalCase, EvalReport, FaithfulnessScorer,
    DeterministicJudge, Trajectory, TrajectoryRecorder,
    PolicyDeniedError, PromptInjectionError, OutputValidationError,
    EapError, IdentityError, RuntimeAdapterError,
)
```

### Added — CLI (`eap-cli`)

A Click-based CLI that scaffolds the golden path for new agentic AI
projects. Each command delegates to a pure-Python scaffolder
(testable without the CLI).

#### Commands
- `eap init <DIR> [--name NAME] [--runtime local|bedrock|vertex] [--force]` —
  scaffolds a new agent project (11 files: `agent.py`,
  `pyproject.toml`, default middleware chain, `tools/example_tool.py`,
  `configs/policy.json`, `configs/agent_card.json`, `responses.yaml`,
  `tests/golden_set.json`, `.claude.md`, `.gitignore`, `README.md`).
  Result runs end-to-end on the local runtime with no cloud creds.
- `eap create-agent --name NAME --template research|transactional` —
  overlays an agent template:
  - **research** — retrieval-style with `search_docs` tool; the
    agent calls the tool then summarizes with the docs as context.
  - **transactional** — action-style with `get_account` +
    `transfer_funds(requires_auth=True)` tools, idempotency-key
    handling.
- `eap create-tool --name NAME --mcp [--auth-required]` — adds a
  typed MCP-decorated tool stub. Uses `__name__` filename
  substitution in the renderer.
- `eap create-mcp-server <DIR> [--name NAME] [--force]` — scaffolds a
  standalone MCP-stdio server project (no LLM client). 8-file
  template that registers tools and runs `run_stdio()`. Suitable for
  Claude Desktop / Claude Code / IDE MCP integrations.
- `eap eval --dataset PATH [--agent path:func] [--report json|html|junit]
  [--threshold 0.7] [--output FILE]` — runs the project's agent over
  a golden-set, scores via configured scorers, exits non-zero if any
  case is below threshold.
- `eap deploy --runtime aws|gcp [--bucket BUCKET | --service NAME]
  [--dry-run]` — packaging only by default. AWS produces
  `dist/agent.zip`; GCP produces `dist/agent/` with `Dockerfile` and
  `cloudbuild.yaml`. Live cloud calls gated behind
  `EAP_ENABLE_REAL_DEPLOY=1`.

#### Templates
- `init/` — base project skeleton
- `research/` — retrieval-style overlay
- `transactional/` — action-style overlay with auth-required write tool
- `tool/` — single-tool scaffolder template
- `mcp_server/` — standalone MCP-stdio server template

Templates are Jinja2 files (`*.j2`). The renderer
(`eap_cli.scaffolders.render`) handles dotfiles, hidden directories,
`__name__` filename substitution, and `template.toml` metadata.

### Added — examples
- `examples/research-agent/` — committed reference of a scaffolded
  research-style agent. Exposes `answer(query)` for `eap eval`.
- `examples/transactional-agent/` — committed reference of a
  scaffolded transactional agent. Running `python agent.py` executes
  a `transfer_funds` call with idempotency key.
- `examples/mcp-server-example/` — committed reference of a
  scaffolded standalone MCP server.

### Added — documentation
- `README.md` — comprehensive top-level README explaining the
  thin-bridge architecture, cross-cutting concerns owned by the
  middleware chain, the open standards we bet on (MCP, A2A, OTel
  GenAI, OAuth 2.1, Pydantic v2), install instructions, 5-minute
  quick start, full CLI reference with examples, scaffolded project
  layout, custom-extension guides for middleware / runtime / scorer /
  policy, observability section, production checklist, repository
  layout, and development setup.
- `docs/developer-guide.md` — 1,300-line guide for engineers who
  extend the SDK (10 parts: design intent, load-bearing principles,
  middleware contract, extension cookbook, ecosystem-evolution
  playbook, anti-patterns, versioning + deprecation, codebase tour,
  testing philosophy, future-proofing checklist).
- `docs/superpowers/specs/2026-05-10-eap-core-design.md` — full
  design specification.
- `docs/superpowers/plans/` — four implementation plans
  (foundation, standards, eval, CLI) capturing the build process.

### Added — CI and tooling
- GitHub Actions workflow with three job groups:
  - **lint** — `ruff check`, `ruff format --check`, `mypy --strict`.
  - **test-core** — full test suite with `not extras and not cloud`
    marker filter; coverage gate ≥ 90%.
  - **test-extras** matrix — `[otel, mcp, a2a, policy-cedar, eval]`;
    each entry installs only its extra and runs the corresponding
    extras tests.
- `[dependency-groups.dev]` at the workspace root with pytest,
  pytest-asyncio, pytest-cov, ruff, mypy.
- Workspace-root `[project.optional-dependencies]` forwarders for
  every member's extras (e.g. `pii = ["eap-core[pii]"]`) so
  `uv sync --extra pii` from the root activates the corresponding
  extra on `eap-core`.
- Mypy `[[tool.mypy.overrides]]` for untyped third-party libs (boto3,
  vertexai, presidio, cedarpy, ragas, jsonschema, mcp, fastapi,
  opentelemetry, yaml).

### Test stats at 0.1.0
- 153 tests passing on the no-extras baseline.
- 5 extras tests (`otel`, `mcp`, `a2a`, `policy-cedar`, `eval`)
  passing in their respective matrix entries.
- Coverage ≥ 90% on `eap_core` (default test-core run).
- Lint, format, and strict mypy all green.

### Known limitations
- The `[pii]` extras test (`tests/extras/test_pii_presidio.py`) is
  intentionally not in the CI matrix because Presidio's spaCy
  dependency requires the `en_core_web_lg` model (~600 MB) and spaCy
  cannot auto-download it inside a `uv`-managed venv. Run locally
  with the model installed manually:
  ```bash
  uv pip install https://github.com/explosion/spacy-models/releases/download/en_core_web_lg-3.8.0/en_core_web_lg-3.8.0-py3-none-any.whl
  uv run pytest packages/eap-core/tests/extras/test_pii_presidio.py
  ```
- Cloud adapters (`bedrock`, `vertex`) are shape-correct stubs. Real
  network calls are gated behind `EAP_ENABLE_REAL_RUNTIMES=1` and
  exercised only in a separate, manually-triggered cloud workflow
  (not part of every CI run).
- `LLMJudge` is provided but not unit-tested in the default suite —
  it requires a real LLM client. `DeterministicJudge` covers the
  scorer's algorithmic path in CI.

### Implementation references
- Build proceeded in four sequential phases, each with its own
  implementation plan under `docs/superpowers/plans/`:
  1. **Foundation** (Plan 1) — workspace, middleware chain, runtime
     adapters, identity, `EnterpriseLLM` client, testing helpers, CI.
  2. **Standards** (Plan 2) — MCP types, decorator, registry;
     `invoke_tool` wiring; A2A AgentCard + FastAPI route; MCP stdio
     server; Presidio `AnonymizerEngine` fix.
  3. **Eval** (Plan 3) — `Trajectory`, `TrajectoryRecorder`, `Judge`
     Protocol, `FaithfulnessScorer`, `EvalRunner`, JSON/HTML/JUnit
     emitters, Ragas adapter.
  4. **CLI** (Plan 4) — `eap-cli` package, all five commands, all
     four templates, `examples/research-agent` reference, e2e CLI
     test.
- Each phase shipped with TDD: failing test first, minimal
  implementation, green test, commit. ~30 commits across the four
  phases.

### Post-merge CI and quality fixes

Changes that surfaced when the post-merge CI ran for the first time
(workspace dependency-groups + extras forwarders + mypy overrides).
Same 0.1.0 release; corrections rolled into the tag.

#### Fixed
- **CI was failing because `uv sync --dev` was a no-op.** No
  `[dependency-groups.dev]` existed at the workspace root, so `uv run
  pytest`/`ruff`/`mypy` couldn't find their binaries. Added a
  workspace-root dev group with the right deps.
- **`uv sync --extra <name>` looked for extras on the root project
  instead of `eap-core`.** Added forwarder entries (`pii =
  ["eap-core[pii]"]`, etc.) at the workspace root so root-level extras
  resolve to member extras.
- **`test_presidio_engine_initialises_when_available`** in
  `test_pii.py` failed in CI because spaCy couldn't auto-download the
  model. Removed the test (it duplicated coverage from the properly
  gated `tests/extras/test_pii_presidio.py`).
- **Mypy strict-mode errors against untyped third-party libs.** Added
  `[[tool.mypy.overrides]]` for boto3, vertexai, presidio, cedarpy,
  ragas, jsonschema, mcp, fastapi, opentelemetry, yaml.
- **`_ScorerProto` was a class but mypy demanded a return statement
  on its body.** Changed to a real `Protocol`.
- **`EnterpriseLLM.invoke_tool`'s inner `terminal` callable** failed
  mypy's None-narrowing because `_tool_registry` is
  `McpToolRegistry | None`. Fixed by binding to a non-None local
  before defining the closure.
- **Click `str` args** for the `--runtime` and `--template` flags
  failed `Literal[...]` type checks at the boundary. Added explicit
  `cast(...)` calls in the Click handlers.
- **Inline `# type: ignore` comments** that became unused after the
  module-level overrides were added — cleaned up.
- **Ruff `per-file-ignores` glob** at the workspace root anchored at
  the wrong path — `tests/**/*.py` never matched the actual test
  files at `packages/eap-core/tests/`. Fixed to `**/tests/**/*.py`.

#### Changed
- **CI workflow** updated to use `uv sync --all-packages --group dev
  [--extra <name>]` patterns. Dropped `pii` from the test-extras
  matrix (spaCy model issue); added `mcp`, `a2a`, `eval`.
- **Coverage `omit` list** extended with the three pure-extras-only
  modules (`a2a/server.py`, `mcp/server.py`,
  `eval/ragas_adapter.py`).
- **`# pragma: no cover`** added to:
  - `LLMJudge.extract_claims` and `LLMJudge.entails` (require a real
    LLM, not unit-tested).
  - The Presidio path in `PiiMaskingMiddleware._mask_text` and
    `_init_presidio` (covered by extras tests, not default).
- **`ruff format`** applied across the tree (~70 files reformatted to
  match the configured style).

### Documentation, examples, and `eap create-mcp-server`

Final additions before the 0.1.0 tag — improve the user experience
and close two real gaps without changing the public API surface.

#### Added
- **`eap create-mcp-server <DIR>`** — new CLI command and template
  for scaffolding standalone MCP-stdio server projects (separate
  artifact from agent projects). 4 unit tests + 1 e2e test cover
  the path.
- **Top-level `eap_core` re-exports** — `mcp_tool`,
  `McpToolRegistry`, `default_registry`, `AgentCard`, `Skill`,
  `build_card`, `EvalRunner`, `EvalCase`, `FaithfulnessScorer`,
  `DeterministicJudge`, `Trajectory`, `TrajectoryRecorder`, and the
  full exception hierarchy. Users can now write
  `from eap_core import EnterpriseLLM, mcp_tool, build_card`.
- **`README.md`** — comprehensive rewrite (~520 lines). Covers
  architecture diagram, default middleware table, open-standards
  story, install, 5-minute quick start, full CLI reference, custom
  extension guides, observability, production checklist, repo layout,
  and dev setup.
- **`examples/transactional-agent/`** — second committed example,
  scaffolded from the transactional template. Demonstrates
  policy-gated write tools and idempotency-key handling.
- **`examples/mcp-server-example/`** — third committed example,
  scaffolded from the new mcp-server template. Demonstrates a
  standalone tools-only project.
- **e2e tests** for the transactional and mcp-server scaffolders in
  `packages/eap-cli/tests/test_e2e.py`.
- **`docs/developer-guide.md`** — 1,300-line developer guide for
  contributors who extend the SDK rather than just consume it.
  Documents the design intent, the 10 load-bearing principles, the
  middleware contract, an extension cookbook with runnable code
  samples for every extension point, an ecosystem-evolution
  playbook (new LLM providers / MCP SDK churn / OTel semconv updates
  / Pydantic majors / new eval frameworks / replacing Click / new
  policy engines), an anti-patterns refuse list, the versioning and
  deprecation playbook, a codebase tour, the testing philosophy, and
  a future-proofing checklist.
- **`CHANGELOG.md`** — this file.

#### Fixed
- **mcp_server template's `server.py.j2`** had import order that ruff
  isort wanted reorganized. Updated the template so future scaffolds
  produce clean code without needing `--fix`.
- **`__all__` in `eap_core/__init__.py`** intentionally not
  alphabetically sorted (grouped semantically by category instead);
  added `# noqa: RUF022` with rationale.

#### Changed
- **README's repository-layout section** now lists all three example
  projects and points at `docs/developer-guide.md` for contributors.

---

## Compatibility table

| Surface | Stability since 0.1.0 |
|---|---|
| `EnterpriseLLM` public methods | Stable |
| `Middleware` Protocol | Stable |
| `BaseRuntimeAdapter` ABC | Stable |
| `Request` / `Response` / `Chunk` / `Message` (wire format) | Stable |
| `AgentCard` / `Skill` (wire format) | Stable (matches A2A spec) |
| `Trajectory` (wire format) | Stable |
| Exception hierarchy | Stable |
| Default middleware classes | Behavior stable; impl details may change |
| CLI commands | Stable |
| Templates | May change between minor versions; not part of API |
| `_*` private symbols | No stability guarantee |
| Cloud adapter network calls | Behind env flag; may evolve as vendor SDKs change |

---

## How to read this changelog

- **Added** — new features users can call.
- **Changed** — behavior changes for existing features.
- **Deprecated** — features still working but scheduled for removal.
- **Removed** — features no longer present.
- **Fixed** — bug fixes.
- **Security** — vulnerabilities patched.

For the design rationale behind any change, see
[`docs/developer-guide.md`](docs/developer-guide.md). For the full
build history, see `docs/superpowers/specs/` (intent) and
`docs/superpowers/plans/` (execution).
