# EAP-Core Developer Guide

This guide is for engineers who **extend** EAP-Core, not just use it.
It explains the design intent so future contributors can keep adding
value without diluting what makes the SDK useful.

If you only want to *consume* the SDK, the top-level `README.md` and
the design spec at `docs/superpowers/specs/2026-05-10-eap-core-design.md`
are enough. Read this if you're about to add a new middleware, a new
runtime adapter, a new template, a new extras dependency, or anything
that crosses a public-API boundary.

---

## Part 1 — Why "thin, yet essential"

EAP-Core sits between two failure modes the industry keeps producing:

**The fat platform.** A walled-garden runtime that owns the agent
loop, the prompt format, the memory model, and your deploy pipeline.
You get a quick start; you pay forever in vendor lock-in, hard
upgrades, and rigid abstractions that fight your problem.

**The bare transport.** A thin HTTP wrapper around a model provider.
Every team rebuilds prompt-injection sanitization, PII masking,
observability, policy enforcement, identity, and eval — usually
inconsistently, often badly, almost never auditably.

EAP-Core makes a different bet: **own the cross-cutting concerns,
nothing else.**

- We own the middleware chain. Every LLM call and every tool call
  passes through the same chain. There is one place to enforce policy,
  one place to mask PII, one place to record OTel spans. Audit becomes
  possible because the chain is the only path.
- We do **not** own the agent loop, the prompt format, the state
  machine, the retrieval pipeline, the memory model, or the deploy
  target. That's user code. Use LangGraph, write a state machine by
  hand, run a single LLM call — the SDK doesn't care.

The "thin" part is what lets enterprise teams adopt us. The
"essential" part is what makes it worth adopting at all.

### The bargain

Teams building on EAP-Core trade a small amount of conformance for a
large amount of safety:

- **They write business logic in plain Python.** No DSL, no graph
  builder, no proprietary state object.
- **They get cross-cutting concerns for free.** Sanitize, PII, OTel,
  policy, validation are wired into the default chain on every call.
  They are not optional add-ons that someone has to remember to wire.
- **They commit to open protocols.** MCP for tools, A2A for agent
  cards, OTel GenAI for tracing, OAuth 2.1 for identity. If a vendor
  ever swaps one of these for something proprietary, the SDK swaps the
  *implementation* underneath, not the user's code.
- **They keep the option to leave.** No EAP-Core type appears in the
  hot path of business logic except `EnterpriseLLM.generate_text` (or
  `invoke_tool`). Removing the SDK is a bounded refactor, not a
  rewrite.

Maintainers must hold up the other end of the bargain. **Every
change** to this SDK is judged against whether it preserves that
deal. The rest of this guide is how to do that.

### Why this works in enterprise

Enterprise teams have non-negotiable obligations:

- Auditability of every model and tool call.
- Demonstrable enforcement of data handling policy (PII, regulated
  content, access control).
- Identity-bound calls — every action is traceable to a workload
  identity, not a shared API key.
- Observability that doesn't depend on a vendor's UI.
- Eval that runs in CI and blocks regressions.

A platform that gets you 80% of these and lets you opt out of the
rest doesn't actually solve the problem; the moment one team opts
out, the audit story is gone for the whole org. The SDK has to make
**the easy path the safe path**, on every call, by default.

That's the load-bearing claim. Everything in the design follows from
it.

---

## Part 2 — Design principles (load-bearing)

These are the principles the codebase was built on. Treat them as
constraints when you extend it. Each is here because we expect to
revisit it under pressure; this is the resistance it has to push
back.

### 2.1 Strategy pattern over inheritance

`BaseRuntimeAdapter` is an ABC with a tiny interface (`generate`,
`stream`, `list_models`, `aclose`). The Local, Bedrock, and Vertex
adapters share nothing except this interface. No common base class
with helper methods, no shared state, no template-method tricks.

**Why this matters.** Each adapter wraps a vendor's SDK; vendor SDKs
disagree about everything from streaming chunk shape to error
hierarchy. Inheritance forces a least-common-denominator that no
adapter actually wants. The Strategy pattern keeps the contract
narrow and the bodies independent.

**Apply when extending.** A new adapter should look like Local,
Bedrock, or Vertex — a single file, no inheritance from anything
except `BaseRuntimeAdapter`, lazy import of the vendor SDK inside the
methods that need it.

### 2.2 Chain of Responsibility for cross-cutting concerns

The middleware pipeline is an onion. Every middleware sees every
request on the way in (left-to-right) and every response on the way
out (right-to-left). On error, the same middlewares that ran
`on_request` get `on_error` called in reverse.

**Why this matters.** Cross-cutting concerns compose. PII masking
needs to run before the LLM sees the prompt; PII unmasking needs to
run after the response comes back. Observability needs to wrap
*both*. The onion model is the only structure that gives you
symmetric setup/teardown semantics for free.

**Apply when extending.** A new middleware almost always pairs
`on_request` with `on_response` (or pairs `on_request` with
`on_error`). If your middleware only ever needs `on_request`, you're
probably writing a guard, not a true cross-cutting concern.

### 2.3 Open protocols over proprietary glue

Every public boundary uses a recognized open standard:

- Tools: MCP.
- Agent discovery: A2A AgentCard at `/.well-known/agent-card.json`.
- Tracing: OTel GenAI semantic conventions.
- Identity: OAuth 2.1 + RFC 8693 token exchange.
- Schemas: JSON Schema (via Pydantic v2).

**Why this matters.** When a vendor wants to "embrace and extend" one
of these, the SDK is the buffer. We update the *implementation*
behind the standard interface; the user's code keeps importing from
`eap_core.mcp` or `eap_core.a2a`. The user is shielded from the
churn.

**Apply when extending.** If you're tempted to expose a
vendor-specific shape on the public API, stop and find the
corresponding standard. If no standard exists, ship the
vendor-specific bit behind an extras-gated module so users can opt
in, and document it as such until a standard catches up.

### 2.4 Optional extras, not hard dependencies

The base install pulls four runtime libraries (`pydantic`, `httpx`,
`pyjwt[crypto]`, `pyyaml`) plus `jsonschema` for tool input validation.
Everything else — Presidio, OpenTelemetry SDK, boto3, Vertex,
official MCP SDK, FastAPI, Ragas, cedarpy — lives behind an
**optional extra**. The relevant module lazy-imports its dep at
call-time and raises a clear "install the X extra" message if
missing.

**Why this matters.** A junior engineer running `pip install
eap-core` should get a working SDK that boots in under a second. The
moment we add a 500MB transitive dep to defaults, we lose every
integration test, every quick demo, and every "let me try this in a
notebook" use case.

**Apply when extending.** Default to extras. The bar to add a hard
dep at the base level is "needed by 100% of users on every call."
Almost nothing meets that bar.

### 2.5 Lazy imports + fail loudly

Optional extras are imported inside the function or method that needs
them, wrapped in `try/except ImportError`, and the catch raises an
`ImportError` with the **exact `pip install` command** the user
should run.

```python
def _init_presidio(self) -> None:
    try:
        from presidio_analyzer import AnalyzerEngine
        from presidio_anonymizer import AnonymizerEngine
    except ImportError as e:
        raise ImportError(
            "engine='presidio' requires the [pii] extra: pip install eap-core[pii]"
        ) from e
    ...
```

**Why this matters.** A silent fall-back to a no-op when an extras
dep is missing is a security failure waiting to happen. If a user
thinks they have Presidio masking on and they actually don't, they
ship PII to the LLM. We make the failure immediate and obvious.

**Apply when extending.** Never silently degrade. If the user asked
for "presidio" engine and presidio is missing, raise. If they asked
for OTel and SDK is missing, the middleware can no-op (because the
default install doesn't have it either) — but **always** keep
`ctx.metadata` populated so eval and audit don't depend on the SDK
being installed.

### 2.6 Identity is a first-class concept

`Context.identity` is set by the client at request start and
available to every middleware. The policy middleware uses it as the
principal in the rule evaluation. The MCP dispatcher uses it to
acquire OAuth tokens before tool calls. The trajectory recorder logs
it.

**Why this matters.** Identity is the only thing that makes audit
trails actionable. "Workload X did Y on resource Z at time T" is the
unit of accountability. If identity is opt-in, audit is opt-in, which
is to say there is no audit.

**Apply when extending.** Any new middleware that gates an action
must read `ctx.identity`. Any new tool dispatcher must check whether
the tool requires auth and acquire a token via the identity's
`get_token()`. Don't introduce alternate auth paths.

### 2.7 Per-request `Context` over module state

`Context` is a per-call mutable container shared across middlewares.
It carries the vault, the OTel span, the active identity, the request
id, and a free-form `metadata` dict. It is created in the client at
request start and discarded when the request ends.

**Why this matters.** The middleware chain composes only because
context flows along with the request, not via globals. If a
middleware stashes the active span in a module-level variable, the
next concurrent request clobbers it. We have seen this bug ship in
other agentic platforms; it is corrosive.

**Apply when extending.** If you find yourself reaching for a global
or a class-level mutable, walk back. Stash on `ctx.metadata` with a
namespaced key (`gen_ai.usage.input_tokens`, `policy.matched_rule`,
`tenant.id`, etc.).

### 2.8 Trust-but-verify at boundaries

We trust internal code; we verify everything that crosses a system
boundary.

- LLM responses → `OutputValidationMiddleware` enforces a Pydantic
  schema before we accept the payload.
- Tool inputs → the registry validates `args` against the JSON Schema
  generated from type hints before invoking the function.
- Identity tokens → `LocalIdPStub.verify` checks the JWT signature.
  In production, `OIDCTokenExchange` round-trips against a real IdP.
- Policy → every action runs through `PolicyEvaluator.evaluate`
  before the runtime adapter is called.

**Why this matters.** Boundary defenses are cheap and the failure
modes they prevent are catastrophic (LLM injection, data
exfiltration, privilege escalation). Inside the SDK, defensive checks
add noise without blocking real attacks.

**Apply when extending.** New extension points should validate at the
boundary they cross. The boundary is "user input enters the SDK" or
"SDK output leaves to a third party." Internal middleware-to-runtime
calls don't need defensive coding.

### 2.9 Standard-first, framework-agnostic

We don't ship a "preferred framework" for agent state, retrieval, or
memory. Each team brings their own. The SDK is built so a LangGraph
agent, a CrewAI agent, a hand-rolled state machine, or a single
prompt all work the same way through the chain.

**Why this matters.** Frameworks come and go. The cross-cutting
concerns are durable. Coupling the SDK to a particular framework
would force every team into one choice and would mean re-writing the
SDK every two years.

**Apply when extending.** Resist any feature that says "to use this
you must use framework X." If the feature is genuinely useful, find
the framework-agnostic shape — usually it's a Protocol the framework
implements.

### 2.10 Versioned schemas, evolvable

Every public data structure is a Pydantic model with explicit fields.
Adding a new optional field is forward-compatible. Removing or
renaming a field is a breaking change and follows the deprecation
playbook (Part 7).

**Why this matters.** AgentCard, ToolSpec, Trajectory, EvalReport are
*serialized*. They cross processes, get logged, get stored. Schema
churn breaks downstream consumers in ways that are very hard to
debug. We are stricter about schema stability than about Python API
stability.

**Apply when extending.** New fields default to optional with a
sensible default. Renames go through a `Field(alias=...)` shim before
the old name disappears. Removing a field is a major-version bump.

---

## Part 3 — The middleware contract

If you only learn one thing about EAP-Core internals, learn the
middleware contract. Everything else hangs off it.

### 3.1 The Protocol

```python
class Middleware(Protocol):
    name: str
    async def on_request(self, req: Request, ctx: Context) -> Request: ...
    async def on_response(self, resp: Response, ctx: Context) -> Response: ...
    async def on_stream_chunk(self, chunk: Chunk, ctx: Context) -> Chunk: ...
    async def on_error(self, exc: Exception, ctx: Context) -> None: ...
```

A middleware is any object satisfying this protocol. Subclassing
`PassthroughMiddleware` is a convenience for partial implementations.
You don't have to.

### 3.2 The onion executor

The pipeline does the following on `client.generate_text(...)`:

1. Iterate middlewares in order. For each, append to the `ran` list
   (so `on_error` knows who needs cleanup), then call
   `on_request(req, ctx)`. The returned `req` replaces the prior one.
2. Call the runtime adapter's `generate(req)`.
3. Iterate `ran` in reverse and call `on_response(resp, ctx)` on each.
   The returned `resp` replaces the prior one.

On any exception during step 1 or 2 or 3, the executor calls
`on_error(exc, ctx)` on every middleware in `ran` in reverse, then
re-raises.

**Concrete consequences:**

- A middleware that raises in `on_request` *will* receive `on_error`.
  Append-before-await is intentional; see `pipeline.py`.
- `on_response` middlewares only run if every prior `on_request`
  succeeded *and* the runtime adapter returned. If the runtime raised,
  no `on_response` runs; only `on_error`.
- Middlewares listed *later* in the chain are *closer* to the runtime
  on the way down (last to see the request) and *farther* from the
  runtime on the way back (first to see the response).

### 3.3 Streaming

`stream_text` runs `on_request` once, calls the runtime adapter's
`stream(req)`, then for every chunk yielded, runs the chunk through
each middleware's `on_stream_chunk` in order.

**Important:** `on_response` is **not** called for streaming. If your
middleware needs to act on a complete response (e.g. schema
validation, PII unmasking of a fully-formed sentence), you must
buffer in `on_stream_chunk` and emit on completion. This is the
trade-off for streaming — you can't both stream early *and* validate
the full response.

### 3.4 The Context object

```python
@dataclass
class Context:
    vault: dict[str, str] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    span: Any = None
    identity: Any = None
    request_id: str = ""
```

- `vault` — PII re-identification table. Keys are `<TYPE_xxxxxxxx>`
  tokens; values are the original PII.
- `metadata` — free-form key-value bag for cross-middleware
  coordination. **Always namespace your keys** (`gen_ai.*`,
  `policy.*`, `tenant.*`).
- `span` — the active OTel span if observability is wired.
- `identity` — the `NonHumanIdentity` for this request.
- `request_id` — UUID, set by the client.

The `Context` is created at request start and discarded at request
end. It is **not** thread-safe across requests; never reach into
another request's context.

### 3.5 Performance contract

Middlewares run on every request. Their fixed cost matters.

- **No I/O in `on_request`/`on_response` unless absolutely necessary.**
  PII regex masking is OK. A network call to a remote policy decision
  point is not — keep evaluation in-process or cache aggressively.
- **No global state mutation.** Use `ctx.metadata`.
- **Bail early** when there's no work to do. The PII unmasker checks
  `if not ctx.vault: return resp` before doing anything else.

### 3.6 Error contract

Errors that propagate out of middlewares should carry rich metadata.

- `PromptInjectionError` carries the matched pattern.
- `PolicyDeniedError` carries the matched rule id.
- `OutputValidationError` carries the Pydantic error trace.
- `MCPError` carries the tool name.
- `IdentityError` carries the underlying IdP failure reason.

A custom middleware that rejects a request should raise a custom
exception that subclasses `EapError` and includes whatever the audit
log will need to reconstruct the decision. Don't raise
`ValueError("denied")`.

### 3.7 Observability is mandatory

Every middleware should leave `ctx.metadata` traces of its decisions
even when the OTel SDK isn't installed. The convention is:

- `gen_ai.*` — model and operation metadata (semconv).
- `policy.matched_rule` — which rule allowed the request.
- `pii.masked_count` — how many tokens went into the vault.
- `<your-feature>.<key>` — your namespaced fields.

Downstream consumers (eval, audit log, replay) read these fields
without depending on OTel being installed.

---

## Part 4 — Extension cookbook

Each subsection is a complete, working example. Copy and adapt.

### 4.1 Add a custom middleware

**When.** You need a cross-cutting concern that isn't covered by the
default chain. Common cases: tenant tagging, rate limiting, cost
budgeting, approval workflows, custom audit logging, response
caching.

```python
# my_pkg/middleware/tenant_stamp.py
from __future__ import annotations

from eap_core.middleware import PassthroughMiddleware
from eap_core.types import Context, Request


class TenantStamper(PassthroughMiddleware):
    """Stamps the active tenant id on the request so it shows up in
    OTel spans and trajectory records."""

    name = "tenant_stamp"

    def __init__(self, tenant_id: str) -> None:
        self._tenant_id = tenant_id

    async def on_request(self, req: Request, ctx: Context) -> Request:
        ctx.metadata["tenant.id"] = self._tenant_id
        return req
```

Wire it into your client:

```python
client = EnterpriseLLM(
    runtime_config,
    middlewares=[
        TenantStamper(tenant_id="acme"),
        PromptInjectionMiddleware(),
        PiiMaskingMiddleware(),
        ObservabilityMiddleware(),
        PolicyMiddleware(...),
        OutputValidationMiddleware(),
    ],
)
```

**What to keep in mind:**

- Pick a clear name (it shows up in error messages and trace tags).
- Namespace your `ctx.metadata` keys with a prefix that clearly
  identifies your concern.
- If you raise, raise an `EapError` subclass with rich data.

### 4.2 Add a custom runtime adapter

**When.** You're integrating a new LLM provider, an internal model
gateway, or a record/replay shim.

```python
# my_pkg/runtimes/azure_openai.py
from __future__ import annotations

import os
from collections.abc import AsyncIterator

from eap_core.config import RuntimeConfig
from eap_core.runtimes.base import (
    BaseRuntimeAdapter,
    ModelInfo,
    RawChunk,
    RawResponse,
)
from eap_core.types import Request


class AzureOpenAIAdapter(BaseRuntimeAdapter):
    name = "azure-openai"

    def __init__(self, config: RuntimeConfig) -> None:
        self._config = config

    async def generate(self, req: Request) -> RawResponse:
        try:
            from openai import AsyncAzureOpenAI
        except ImportError as e:
            raise ImportError(
                "azure-openai adapter requires `pip install openai`"
            ) from e
        client = AsyncAzureOpenAI(
            api_key=os.environ["AZURE_OPENAI_API_KEY"],
            api_version=self._config.options.get("api_version", "2024-08-01-preview"),
            azure_endpoint=self._config.options["endpoint"],
        )
        resp = await client.chat.completions.create(
            model=self._config.model,
            messages=[
                {"role": m.role, "content": m.content if isinstance(m.content, str) else ""}
                for m in req.messages
            ],
        )
        return RawResponse(
            text=resp.choices[0].message.content or "",
            usage={
                "input_tokens": resp.usage.prompt_tokens,
                "output_tokens": resp.usage.completion_tokens,
            },
            finish_reason=resp.choices[0].finish_reason,
            raw={"id": resp.id},
        )

    async def stream(self, req: Request) -> AsyncIterator[RawChunk]:
        # similar pattern; left as exercise
        raise NotImplementedError

    async def list_models(self) -> list[ModelInfo]:
        return [ModelInfo(name=self._config.model, provider="azure-openai")]
```

Register it via your package's `pyproject.toml`:

```toml
[project.entry-points."eap_core.runtimes"]
azure-openai = "my_pkg.runtimes.azure_openai:AzureOpenAIAdapter"
```

`RuntimeConfig(provider="azure-openai", model="gpt-4o-mini",
options={"endpoint": "https://..."})` then resolves through the
entry-point group. **No fork required.**

**What to keep in mind:**

- Lazy-import the vendor SDK inside `generate`/`stream`. Constructing
  the adapter must not pull the SDK.
- Raise a clear `ImportError` if the vendor SDK is missing. Tell the
  user the exact install command.
- If your adapter requires an env flag like
  `EAP_ENABLE_REAL_RUNTIMES=1` (because the call costs money or hits
  prod), gate the network call behind it. See
  `runtimes/bedrock.py` for the canonical pattern.
- Map the vendor's usage fields to our `input_tokens` /
  `output_tokens` so OTel attributes stay consistent across
  providers.

### 4.3 Add a custom Judge / scorer

**When.** Your domain has a quality dimension that
`FaithfulnessScorer` doesn't measure (answer relevance, citation
correctness, hallucinated entity rate, etc.) — or you want to swap
the LLM-as-judge for a rule-based or human-in-the-loop scorer.

```python
# my_pkg/eval/answer_relevance.py
from __future__ import annotations

from eap_core.eval import FaithfulnessResult, Trajectory


class AnswerRelevanceScorer:
    """Score how well the answer addresses the question.

    For demo simplicity, ratio of question-content-words present in
    the answer. In production, replace with an LLM judge.
    """

    name = "answer_relevance"

    async def score(self, traj: Trajectory) -> FaithfulnessResult:
        question = (traj.extra or {}).get("input_text", "")
        if not question or not traj.final_answer:
            return FaithfulnessResult(request_id=traj.request_id, score=0.0)
        q_words = {w.lower() for w in question.split() if len(w) > 3}
        a_words = {w.lower() for w in traj.final_answer.split() if len(w) > 3}
        if not q_words:
            return FaithfulnessResult(request_id=traj.request_id, score=0.0)
        score = len(q_words & a_words) / len(q_words)
        return FaithfulnessResult(request_id=traj.request_id, score=score)
```

Wire it into a runner:

```python
runner = EvalRunner(
    agent=my_agent,
    scorers=[
        FaithfulnessScorer(judge=DeterministicJudge()),
        AnswerRelevanceScorer(),
    ],
    threshold=0.5,
)
```

**What to keep in mind:**

- Match the `_ScorerProto` shape (string `name` + async
  `score(traj) -> FaithfulnessResult`). It's a `Protocol` — no
  inheritance needed.
- Return a `FaithfulnessResult` even if your scorer doesn't compute
  faithfulness. The result type is the universal score record.
- Aggregate scores in `EvalReport.aggregate` are keyed by `name` —
  pick something unique per scorer.

### 4.4 Add a custom policy evaluator

**When.** You're integrating with an existing policy decision point
(OPA, Cedar service, internal entitlement system) or you need policy
rules that the JSON evaluator can't express.

```python
# my_pkg/policy/opa.py
from __future__ import annotations

from typing import Any

import httpx

from eap_core.middleware.policy import PolicyDecision


class OPAPolicyEvaluator:
    """Calls an OPA HTTP endpoint for each evaluation."""

    def __init__(self, opa_url: str, package: str) -> None:
        self._url = f"{opa_url.rstrip('/')}/v1/data/{package.replace('.', '/')}"
        self._http = httpx.Client(timeout=2.0)

    def evaluate(self, principal: Any, action: str, resource: str) -> PolicyDecision:
        body = {
            "input": {
                "principal": getattr(principal, "client_id", "*"),
                "roles": getattr(principal, "roles", []),
                "action": action,
                "resource": resource,
            }
        }
        resp = self._http.post(self._url, json=body)
        resp.raise_for_status()
        result = resp.json().get("result", {})
        if result.get("allow"):
            return PolicyDecision(True, result.get("rule", "opa"), "OPA permitted")
        return PolicyDecision(False, result.get("rule", "opa"), result.get("reason", "OPA denied"))
```

Wire it in:

```python
mw = PolicyMiddleware(OPAPolicyEvaluator(opa_url="http://opa:8181", package="eap.authz"))
```

**What to keep in mind:**

- The `PolicyEvaluator` Protocol expects synchronous `evaluate`. If
  you need async I/O, wrap with `asyncio.to_thread` or change your
  evaluator to use a connection pool with low timeouts. Slow policy
  decisions block every LLM call.
- Cache aggressively. Most policy decisions are stable per
  `(principal, action, resource)` for at least a few seconds.
- Return a `PolicyDecision` even for errors. A failed evaluator
  should fail closed (deny) with a `rule_id` like `"opa-error"` so
  audit can distinguish from real denials.

### 4.5 Add a CLI template

**When.** You have a project type the existing templates don't
cover. Examples: a streaming-only agent, a multi-modal agent, a
memory-backed conversational agent, an agent with a built-in vector
store.

The CLI's templates live under
`packages/eap-cli/src/eap_cli/templates/<name>/`. Each is a directory
of Jinja2 files plus a `template.toml` describing required variables.

To add a `streaming` template:

1. Create `packages/eap-cli/src/eap_cli/templates/streaming/` with the
   files (look at `templates/research/` for an overlay-style template
   or `templates/init/` for a base-project template).
2. Add a `template.toml`:

   ```toml
   [template]
   name = "streaming"
   description = "Streaming-style agent"
   required_vars = ["agent_name"]
   ```
3. If it's an overlay (like `research`), add the literal to the Click
   `--template` choice in `main.py` and to the `_VALID_TEMPLATES`
   set in `scaffolders/create_agent.py`.
4. Add a test in `packages/eap-cli/tests/test_create_agent.py`.

**What to keep in mind:**

- Templates render with `StrictUndefined`. If you reference a
  variable in `.j2` that the scaffolder doesn't pass, render fails
  loudly. This is intentional.
- Keep templates small. They are documentation as much as code; the
  shorter they are, the more readable.
- The scaffolded `.claude.md` is real load-bearing. Update it
  whenever you change conventions so the next AI coding agent reading
  the project picks them up.

### 4.6 Add an optional extra

**When.** You're integrating a heavyweight library that not every
user will want.

1. Add the dep to `packages/eap-core/pyproject.toml` under
   `[project.optional-dependencies]`:

   ```toml
   [project.optional-dependencies]
   my-extra = ["the-heavy-lib>=2.0"]
   ```
2. Add a forwarder at the workspace root (`pyproject.toml`):

   ```toml
   [project.optional-dependencies]
   my-extra = ["eap-core[my-extra]"]
   ```
3. Lazy-import the dep inside the consuming module, with a clear
   `ImportError` message if missing.
4. Add an extras test under
   `packages/eap-core/tests/extras/test_my_extra.py` with
   `pytest.importorskip("the_heavy_lib")` at the top and
   `pytestmark = pytest.mark.extras`.
5. Add the extra to the CI matrix in `.github/workflows/ci.yml` so
   the test runs.
6. Add the heavy-lib's module(s) to `[[tool.mypy.overrides]]` if
   they don't ship `py.typed`.
7. Document the extra in the README's install section.

**What to keep in mind:**

- The extras name is the user-visible string; pick something short
  and stable. `pii`, `otel`, `mcp`, `a2a` are good. `presidio` is
  worse because we might swap implementations later.
- If the extra adds a new code path with significant logic, add
  `# pragma: no cover` to that path and let the extras matrix cover
  it. Default `test-core` should not measure it.

### 4.7 Add a custom identity provider

**When.** You're integrating with a real OIDC IdP (Okta, Auth0,
Cognito, Keycloak, custom) or with a non-OIDC system.

For real OIDC, the bundled `OIDCTokenExchange` is the right tool
out of the box; just point it at your token endpoint:

```python
exchange = OIDCTokenExchange(
    token_endpoint="https://my-idp.example.com/oauth/token",
)
nhi = NonHumanIdentity(
    client_id="my-agent",
    idp=...,  # a custom IDP that signs assertions; see local_idp.py
    default_audience="https://api.bank.example",
)
```

For a non-OIDC system, implement the `IdentityProvider` Protocol:

```python
class IdentityProvider(Protocol):
    def issue(
        self,
        *,
        client_id: str,
        audience: str,
        scope: str,
        roles: list[str] | None = None,
    ) -> str: ...
```

Anything that returns a string token is acceptable. The downstream
consumer (the tool dispatcher) just attaches it as a Bearer header.

**What to keep in mind:**

- The cache in `NonHumanIdentity` is keyed on `(audience, scope)` and
  uses a 5-second buffer before expiry to avoid race conditions.
  Don't subclass to disable caching; instead pass `token_ttl=0` on
  your IdP for tests.
- Roles are read from `principal.roles` by the policy middleware;
  populate them in your IdP's `issue` if your policy needs them.

---

## Part 5 — Evolving with the open source ecosystem

The SDK's value depends on staying current with open standards. This
section is the playbook for handling change in the libraries we
depend on.

### 5.1 New LLM providers

The Strategy pattern (§2.1) was designed for this. A new provider is
a single new file under `runtimes/`, a single entry in
`pyproject.toml`'s `[project.entry-points."eap_core.runtimes"]`, and
optionally a new optional extra.

**Decision tree:**

- Is the provider's SDK already widely used? → ship our adapter as
  part of `eap-core` under a new extra (e.g. `[anthropic]`).
- Is it niche or proprietary? → ship it as a separate package
  (`eap-runtime-{provider}`) that depends on `eap-core` and
  registers via the same entry-point group. No fork required.

**Watch-outs.** Different providers report token usage with different
field names. Always normalize to `input_tokens` / `output_tokens` in
the `RawResponse.usage` dict so OTel attributes stay consistent.

### 5.2 MCP SDK churn

The MCP SDK has changed shape between 0.x and 1.x. We isolate the
churn in `eap_core/mcp/server.py`:

- The decorator and registry are pure Python and don't depend on the
  SDK.
- `build_mcp_server` and `run_stdio` are the only places that import
  `mcp.*`.

When the SDK changes:

1. Update the imports and decorator usage in
   `eap_core/mcp/server.py`.
2. The registry, the decorator, and the dispatch path don't change.
3. Add a version pin or floor in
   `[project.optional-dependencies] mcp` if needed.

The extras test `tests/extras/test_mcp_server.py` will catch a real
break against the installed version. Run the matrix locally
(`uv sync --all-packages --group dev --extra mcp && pytest -m extras
packages/eap-core/tests/extras/test_mcp_server.py`).

### 5.3 OTel GenAI semconv updates

OTel's GenAI semantic conventions are still being formalized. We
follow the current published attribute names
(`gen_ai.request.model`, `gen_ai.usage.input_tokens`, etc.).

When semconv publishes a new version:

1. Update the attribute names in
   `eap_core/middleware/observability.py`.
2. Update the keys we write to `ctx.metadata`. Downstream consumers
   that read `gen_ai.*` get the new names automatically.
3. Bump our SDK's version with a deprecation notice if you preserved
   the old keys for compatibility (see Part 7).

### 5.4 Pydantic major version bumps

We pin Pydantic to v2.x. A v3 migration would touch:

- Every `BaseModel` subclass (mostly mechanical).
- `TypeAdapter.json_schema()` calls in `mcp/decorator.py`.
- `model_validate`/`model_dump`/`model_copy` calls everywhere.

Hold the line on Pydantic v2 until v3 is mature and the migration
tools are reliable. When you do migrate, keep the public types
(`Request`, `Response`, `AgentCard`, `Trajectory`) bytewise-compatible
on the wire — that's a hard constraint per §2.10.

### 5.5 New evaluation frameworks

Ragas, DeepEval, Inspect AI, and the next entry are interchangeable
as far as the SDK's eval framework cares. Each gets:

- A new optional extra.
- An adapter at `eap_core/eval/{framework}_adapter.py` that converts
  `Trajectory` to the framework's input shape.
- A skip-on-import test under `tests/extras/`.

Don't try to abstract over multiple frameworks. The conversion code
is small; the abstraction over it would be bigger and would obscure
the per-framework idioms.

### 5.6 Replacing Click

Click is fine for now. If the day comes that we want Typer or rich
for the CLI:

1. The scaffolders in `packages/eap-cli/src/eap_cli/scaffolders/` are
   pure Python — no Click types in their signatures. They keep
   working.
2. Only `main.py` changes — and it's <200 lines.

This is by design (§2.9). Keep it that way: scaffolders stay pure
Python, Click bindings stay in `main.py`.

### 5.7 New cloud-platform integrations

A "cloud-platform integration" wires EAP-Core's vendor-neutral
Protocols to a managed agent platform (AWS Bedrock AgentCore, GCP
Vertex Agent Engine, Azure AI Foundry Agent Service, etc.). It is
**not** the same thing as a new LLM provider (§5.1) — LLM adapters
go under `runtimes/`, platform integrations go under `integrations/`.

The shape is established by `integrations/agentcore.py` and
`integrations/vertex.py`. A new integration follows the same
template:

1. **Pick the Protocols you'll implement.** Look at
   `eap_core.memory.MemoryStore`,
   `eap_core.sandbox.CodeSandbox` / `BrowserSandbox`,
   `eap_core.discovery.AgentRegistry`,
   `eap_core.payments.PaymentBackend`. If the cloud has the
   equivalent service, implement the Protocol. If it has something
   genuinely new, propose a new Protocol first and only ship the
   integration after the Protocol lands.
2. **One file per cloud.** `integrations/<cloud>.py` carries every
   class. Group by phase with section comments (A: deploy + identity
   + observability; B: managed memory + sandboxes; C: gateway;
   D: registry + payments + eval).
3. **Lazy-import the cloud SDK** inside each class's `_client()`
   helper or method. Construction must not touch the network or
   import the SDK.
4. **Gate live calls** behind `EAP_ENABLE_REAL_RUNTIMES=1` so CI runs
   without credentials. Methods raise `NotImplementedError` with a
   setup hint when the flag is unset.
5. **Add a new extra** under `[project.optional-dependencies]` that
   pulls the cloud's SDK. Forward it at the workspace root.
6. **Add a deploy target.** Extend
   `packages/eap-cli/src/eap_cli/scaffolders/deploy.py` with a
   `package_<cloud>()` function and a `deploy_<cloud>()` function
   (the latter gated by `EAP_ENABLE_REAL_DEPLOY=1`). Wire the new
   `--runtime` choice in `main.py`. Generate a Dockerfile +
   handler that matches the cloud's HTTP contract.
7. **Match the CodeSandbox / BrowserSandbox surface** so existing
   `client.invoke_tool(...)` calls keep working when the user swaps
   backends. The middleware chain runs **before** the cloud sandbox
   sees the data — preserve that contract.
8. **Tests follow the AgentCore / Vertex phase split:** four files
   under `tests/test_integrations_<cloud>_phase_<a|b|c|d>.py`.
   Tests assert env-flag gating, Protocol conformance via
   `isinstance(..., MemoryStore)`, and stub-mode behavior. Live
   tests are marked `@pytest.mark.cloud` and run only in the
   separate cloud-credential CI lane.
9. **Document the integration** at
   `docs/integrations/<cloud>-<platform>.md`. Include the
   cross-cloud equivalence table (Protocol ↔ AWS impl ↔ GCP impl ↔
   your new impl) and per-phase usage walkthroughs. Add a row to the
   README's Protocol table for each Protocol you implemented.

**The point is: a new cloud is plug-in, not a fork.** If the design
forces you to add a hook on the middleware Protocol or a new
top-level Protocol just to integrate one cloud, stop and reconsider —
the abstraction probably needs to land in core first.

### 5.8 New policy engines

If a new policy DSL emerges (Rego succession, Cedar v3, OpenFGA,
etc.):

- Implement the `PolicyEvaluator` Protocol — that's the universal
  shape (§4.4).
- Ship it behind a new extra.
- Don't touch the JSON evaluator default.

The default JSON evaluator is **load-bearing**. It's what lets the
core install run end-to-end without any extras. Treat it as
permanent.

---

## Part 6 — Anti-patterns (the refuse list)

Things that will be tempting to do and that you should refuse.

### 6.1 Don't grow the core install

Every dep added to the base `[project] dependencies` of `eap-core`
makes the SDK heavier for every user. The current four (Pydantic,
httpx, PyJWT, PyYAML) plus jsonschema are the **maximum**. New deps go
behind extras (§2.4).

If you find yourself wanting to add a base dep, ask: would it be a
catastrophe if the user couldn't install this? If not, it's an extra.

### 6.2 Don't hardcode vendor names in the chain

```python
# WRONG
async def on_response(self, resp, ctx):
    if "anthropic" in resp.raw.get("model", ""):
        # special handling
```

The chain runs above the runtime adapter. It must be vendor-agnostic.
Vendor specifics belong in the adapter (`runtimes/{vendor}.py`).

If a vendor returns data that doesn't fit our normalized shape, fix
the adapter to normalize it.

### 6.3 Don't bypass the middleware

Direct `await self._adapter.generate(req)` in user code defeats the
entire SDK. The default `EnterpriseLLM.generate_text` is the only
sanctioned path.

If a user has a legitimate reason to bypass the chain (e.g. a low-level
debugging utility), that's their choice — but the SDK's own modules
(client, dispatcher, recorders) must always go through the pipeline.

### 6.4 Don't add an abstraction for one implementation

`BaseRuntimeAdapter` made sense the moment we had two adapters
(Local + Bedrock). It would have been wrong with one. The same logic
applies to any future abstraction:

- One implementation? → ship it concrete.
- Two implementations? → maybe an abstraction. Look at the
  similarities; if they're shallow, leave them concrete.
- Three implementations? → almost certainly an abstraction.

The pattern works because two implementations let you see what's
truly common vs. accidental.

### 6.5 Don't break the middleware Protocol shape

`Middleware` has four methods: `on_request`, `on_response`,
`on_stream_chunk`, `on_error`. Adding a fifth would force every
existing middleware to update. It's almost never worth it.

If you find yourself wanting a new hook, ask whether you can stash
the data in `ctx.metadata` from one of the existing hooks and process
it in a downstream consumer.

### 6.6 Don't silently degrade

A middleware that says "policy denied" must raise. A PII masker that
can't load Presidio must raise. An identity provider that can't
acquire a token must raise. Falling back to "no protection" without
telling anyone is the worst possible failure mode.

The exception messages should tell the user exactly what to do
(`pip install eap-core[pii]`, set env var, etc.).

### 6.7 Don't introduce thread-local or module-level state

Concurrent agentic systems are real. State on `Context` survives;
state on a class attribute or a module global doesn't.

If you must cache (e.g. token cache in `NonHumanIdentity`), make it
instance-level and document the lifecycle.

### 6.8 Don't ship breaking schema changes without a migration

Public types (`Request`, `Response`, `AgentCard`, `Trajectory`,
`EvalReport`, `EvalCase`) are serialized. They cross processes, get
written to JSONL traces, get stored as eval datasets. A breaking
change to any of them invalidates every consumer.

If you must rename a field, follow the deprecation playbook (§7) and
keep the old name working for at least one minor version.

---

## Part 7 — Versioning, deprecation, compatibility

### 7.1 SemVer commitments

- **Major** (X.y.z) — breaking changes to the Python API or
  serialized schemas. Includes removing a deprecated field, renaming
  a public function, changing a Protocol signature, dropping a
  Python version.
- **Minor** (x.Y.z) — new features, new optional extras, new CLI
  commands, new fields on existing types (always optional with
  defaults). Deprecation warnings introduced.
- **Patch** (x.y.Z) — bug fixes, documentation, internal refactors
  with no public surface change.

### 7.2 The deprecation playbook

When you need to rename or remove a public API:

1. **Mark deprecated** in the current minor release. Add a
   `DeprecationWarning` on use. Add a "Deprecated" note to the
   docstring.
2. **Keep it working** for at least one minor version. The new path
   is added; the old path is preserved.
3. **Update README and the CHANGELOG** with the migration recipe.
4. **Remove** in the next major release.

For Pydantic schema fields, use `Field(alias=...)` to map the new
name to the old name during the deprecation window.

For Python functions, keep the old name as a thin shim that emits
the warning and forwards to the new name.

### 7.3 What's stable, what's experimental

| Surface | Stability |
|---|---|
| `EnterpriseLLM` public methods | Stable. SemVer applies. |
| `Middleware` Protocol | Stable. Adding a method is breaking. |
| `BaseRuntimeAdapter` ABC | Stable. |
| `MemoryStore` Protocol | Stable. Adding a method is breaking. |
| `CodeSandbox` / `BrowserSandbox` Protocols | Stable. Adding a method is breaking. |
| `AgentRegistry` Protocol | Stable. Adding a method is breaking. |
| `PaymentBackend` Protocol + `PaymentRequired` | Stable. |
| `ThreatDetector` Protocol + `ThreatAssessment` | Stable. |
| `Request` / `Response` / `Chunk` / `Message` | Stable on the wire. |
| `AgentCard` / `Skill` | Stable on the wire (A2A spec). |
| `Trajectory` | Stable on the wire (eval/audit consumers). |
| `SandboxResult` | Stable. |
| Default middleware classes | Stable behavior; impl details may change. |
| In-process Protocol defaults (`InMemoryStore`, `InProcessCodeSandbox`, etc.) | Stable behavior; impl details may change. |
| Templates | May change between minors; not part of the API. |
| `_*` private symbols | No stability guarantee. |
| Cloud adapter network calls (`runtimes/`) | Behind `EAP_ENABLE_REAL_RUNTIMES=1`; may evolve as vendor SDKs change. |
| Cloud integration classes (`integrations/agentcore`, `integrations/vertex`) | Constructors + Protocol methods stable. Wire calls behind `EAP_ENABLE_REAL_RUNTIMES=1`; may evolve as cloud APIs change. |

### 7.4 Python version policy

We target the current stable Python and the previous one (currently
3.11 and 3.12; 3.13 will be added when ecosystem deps support it).
Dropping a Python version is a major release.

---

## Part 8 — A walking tour of the codebase

### 8.1 Top level

```
ai-eap-sdk/
├── packages/
│   ├── eap-core/        # the SDK
│   └── eap-cli/         # the `eap` CLI
├── examples/            # committed reference projects
├── docs/                # specs, plans, this guide
├── pyproject.toml       # uv workspace root
├── uv.lock              # committed; deterministic resolution
└── .github/workflows/   # CI
```

The two-package split is intentional. `eap-core` is the
import-heavy dependency users put in their `pyproject.toml`. `eap-cli`
is the entry-point users install for scaffolding. They share a venv
in development; users can pick one or both.

### 8.2 `eap-core` layout

```
src/eap_core/
├── __init__.py          # curated public API re-exports (see §4 of README)
├── _version.py          # single source of truth for the version
├── client.py            # EnterpriseLLM — public entry point
├── config.py            # RuntimeConfig, IdentityConfig, EvalConfig
├── exceptions.py        # exception hierarchy
├── types.py             # Request/Response/Chunk/Message/Context
├── memory.py            # MemoryStore Protocol + InMemoryStore default
├── sandbox.py           # CodeSandbox / BrowserSandbox Protocols + in-process defaults
├── discovery.py         # AgentRegistry Protocol + InMemoryAgentRegistry default
├── payments.py          # PaymentBackend Protocol + PaymentRequired + InMemoryPaymentBackend
├── security.py          # ThreatDetector Protocol + RegexThreatDetector default
├── middleware/          # the chain of responsibility
│   ├── base.py
│   ├── pipeline.py      # the onion executor
│   ├── sanitize.py
│   ├── pii.py
│   ├── observability.py
│   ├── policy.py
│   └── validate.py
├── runtimes/            # cloud adapters (Strategy)
│   ├── base.py
│   ├── registry.py      # entry-point discovery
│   ├── local.py
│   ├── bedrock.py       # env-gated
│   └── vertex.py        # env-gated
├── integrations/        # cloud-platform integrations (lazy + env-gated)
│   ├── agentcore.py     # AWS Bedrock AgentCore — 11 services (Phases A–D)
│   └── vertex.py        # GCP Vertex Agent Engine — parallel surface
├── identity/            # NHI + OAuth 2.1 + RFC 8693
│   ├── nhi.py
│   ├── token_exchange.py
│   └── local_idp.py
├── mcp/                 # MCP tools — decorator + registry + server
│   ├── decorator.py
│   ├── registry.py
│   ├── server.py        # [mcp] extra
│   └── types.py
├── a2a/                 # A2A AgentCard + FastAPI route
│   ├── card.py
│   └── server.py        # [a2a] extra
├── eval/                # trajectory recording + scoring + reports
│   ├── trajectory.py
│   ├── faithfulness.py
│   ├── runner.py
│   ├── reports.py
│   └── ragas_adapter.py # [eval] extra
└── testing/             # fixtures shipped to users
    ├── fixtures.py
    └── responses.py
```

**Two-layer separation between `runtimes/` and `integrations/`:**

- `runtimes/` holds **LLM adapters** — they call `messages/completions`
  on a model provider (Bedrock LLM, Vertex LLM, or local mock). They
  satisfy `BaseRuntimeAdapter` and are picked by `RuntimeConfig(provider=...)`.
- `integrations/` holds **agent-platform adapters** — the
  cross-cutting agent infrastructure each cloud offers around LLMs
  (managed memory, sandboxes, gateways, registries, payments, eval).
  They satisfy the vendor-neutral Protocols in
  `sandbox.py` / `discovery.py` / `payments.py` / `memory.py` and are
  wired in at the seam where your agent code instantiates them.

The split keeps the LLM swap (`provider="bedrock"` vs
`provider="vertex"`) independent of the agent-platform swap
(`AgentCoreMemoryStore` vs `VertexMemoryBankStore`).

**One file = one responsibility.** If a file grows beyond 250 lines or
starts to cover multiple concerns, split it before you keep adding.

### 8.3 `eap-cli` layout

```
src/eap_cli/
├── main.py                        # Click app — thin handlers only
├── scaffolders/                   # pure Python — testable without CLI
│   ├── render.py                  # Jinja2 renderer
│   ├── init.py
│   ├── create_agent.py
│   ├── create_tool.py
│   ├── create_mcp_server.py
│   ├── eval_cmd.py
│   └── deploy.py
└── templates/                     # Jinja2 .j2 files
    ├── init/
    ├── research/
    ├── transactional/
    ├── tool/
    └── mcp_server/
```

Click handlers in `main.py` are thin — they parse flags, call a
scaffolder, print a result line. **No business logic in `main.py`.**
This way, every CLI command is reusable as a library function.

### 8.4 `docs/`

```
docs/
├── developer-guide.md                       # this file
├── user-guide-aws-agentcore.md              # for engineers building on AgentCore
├── user-guide-gcp-vertex.md                 # for engineers building on Vertex
├── integrations/
│   ├── aws-bedrock-agentcore.md             # AgentCore positioning + per-service mapping
│   └── gcp-vertex-agent-engine.md           # Vertex positioning + per-service mapping
└── superpowers/
    ├── specs/2026-05-10-eap-core-design.md
    └── plans/
        ├── 2026-05-10-eap-core-foundation.md
        ├── 2026-05-10-eap-core-standards.md
        ├── 2026-05-10-eap-core-eval.md
        └── 2026-05-10-eap-cli.md
```

The spec is the source of truth for *intent*. The plans document the
*how*. This guide explains the *why*. The per-cloud integration docs
explain the *how* of each platform mapping. The user guides explain
the *how* of building an agent on each cloud (audience: engineers
*using* the SDK, not extending it). Keep them in sync — if a plan
diverges from the spec during implementation, update the spec; when
you add or change a cloud integration, update both the integration
doc and the matching user guide.

---

## Part 9 — Testing philosophy

### 9.1 Three test categories, three CI jobs

1. **Default tests** — unit + integration tests that exercise the
   slim install. No extras required. Run in CI's `test-core` job.
2. **Extras tests** — under `tests/extras/`, marked
   `pytestmark = pytest.mark.extras`, gated by
   `pytest.importorskip("the_dep")`. Each runs in a per-extra matrix
   entry in CI.
3. **Cloud tests** — marked `@pytest.mark.cloud`, run only with
   `EAP_ENABLE_REAL_RUNTIMES=1` and real credentials. Not in default
   CI; manual workflow with secrets.

Pytest config has these markers declared in `pyproject.toml` so
unknown markers fail loudly.

### 9.2 Coverage gate

`tool.coverage.report.fail_under = 90` enforces ≥90% coverage on the
default `test-core` run. Modules whose code paths only run with extras
installed are added to `tool.coverage.run.omit`. Mixed modules
(default path + extras path) use `# pragma: no cover` on the
extras-gated branches.

If your change drops coverage below 90%:

1. Add a focused unit test for the new code.
2. If the new code is genuinely extras-only, add `# pragma: no cover`
   to the relevant lines.
3. Don't lower the gate.

### 9.3 What we test

- **Behavior, not implementation.** `test_pipeline.py` checks that
  middleware order is correct on the wire; it doesn't check internal
  variable names.
- **Real failure modes.** Errors should propagate with rich data;
  tests assert on the data, not just the error type.
- **End-to-end paths.** `tests/test_e2e.py` (in `eap-cli`) scaffolds
  a fresh project, runs `python agent.py` as a subprocess, asserts
  on the output. This is the single most valuable test in the repo —
  it catches scaffolding-vs-library drift, which is the thing
  reviewers will miss.
- **Boundary defenses.** Tests for `OutputValidationMiddleware`,
  `PromptInjectionMiddleware`, and the registry's input validation
  are deliberately heavy. Failures here are security-relevant.

### 9.4 What we don't test

- We don't test third-party libraries. Our tests for
  `BedrockRuntimeAdapter` verify it calls boto3 the right way, not
  that boto3 works.
- We don't test "happy path" trivially. A test that constructs an
  object and asserts on its attributes is usually low-value.
- We don't write integration tests against staging environments in
  default CI. Cloud tests are separate and gated.

---

## Part 10 — Future-proofing checklist

Before merging a non-trivial change, walk through this checklist.

### Public API

- [ ] If I added a new public function/class, did I re-export it from
      the appropriate `__init__.py`?
- [ ] If I changed a Pydantic model field, is it a backward-compatible
      addition (new optional field with default), or did I follow the
      deprecation playbook?
- [ ] If I added a new Protocol or ABC, do I have at least two
      implementations? If only one, can I make it concrete?

### Cross-cutting

- [ ] If I added a new middleware, does it write to `ctx.metadata`
      with a namespaced key?
- [ ] Does it raise an `EapError` subclass with rich data on
      rejection?
- [ ] Does it perform any I/O? If so, can it be cached or batched?
- [ ] Does it depend on the OTel SDK being installed? If so, is it
      gated correctly?

### Dependencies

- [ ] If I added a new dep, is it behind an optional extra?
- [ ] If yes, did I add the forwarder at the workspace root?
- [ ] Did I add the module to `[[tool.mypy.overrides]]` if it lacks
      `py.typed`?
- [ ] Did I add an extras test?
- [ ] Did I add the extra to the CI matrix?

### Templates & CLI

- [ ] If I added a new template, did I add the literal to
      `--template`'s Click choice and to `_VALID_TEMPLATES`?
- [ ] If I changed a template, does the scaffolded project still run
      end-to-end? (Subprocess test in `test_e2e.py` catches this.)
- [ ] Does the scaffolded `.claude.md` reflect any new conventions?

### Tests

- [ ] Did `pytest -m "not extras and not cloud"` stay green and
      ≥90% coverage?
- [ ] Did each `--extra` matrix entry stay green?
- [ ] Did `ruff check && ruff format --check && mypy` stay clean?

### Docs

- [ ] If I changed user-visible behavior, did I update the README?
- [ ] If I changed an extension point, did I update this guide?
- [ ] If I changed a load-bearing principle (Part 2), did I write up
      the rationale in a CHANGELOG entry?

---

## Closing

The reason EAP-Core exists is that nobody else is going to write the
boring layer for you. Cross-cutting concerns are not interesting
research; they're not anyone's career-making project; and they are
the difference between an enterprise agentic system that ships and
one that gets recalled.

Hold the line on "thin." Hold the line on "essential." Add features
that strengthen the bargain we made with users (Part 1) and refuse
features that weaken it (Part 6). When you're not sure, re-read Part
2 — the load-bearing principles are the reason this works.

Future contributors: leave the SDK lighter than you found it. The
default install is a contract. The middleware chain is a contract.
The standards we picked are a contract. **Make new things plug in;
do not change the shape.** That's how the SDK keeps being useful as
the ecosystem changes around it.
