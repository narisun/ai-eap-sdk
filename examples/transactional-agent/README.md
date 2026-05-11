# transactional-agent — action-style reference project

Minimal reference for an **action-style** EAP-Core agent: performs
writes via tools, with explicit policy gates, an auth-required tool
(`transfer_funds`), and idempotency-key handling so retries are safe.
Good starting point for any agent that performs writes — fund
movements, ticket creation, infra mutations, etc.

This template is what `eap create-agent --template transactional`
generates. Read it side-by-side with the user guides — every wiring
decision here maps to a step in the cloud-specific tutorials.

**What it demonstrates:**

| EAP-Core feature | Where in this project |
|---|---|
| Read-only `@mcp_tool` | `tools/get_account.py` |
| Auth-required `@mcp_tool` (`requires_auth=True`) | `tools/transfer_funds.py` |
| Idempotency-key dedup on writes | `tools/transfer_funds.py` (`_LEDGER`) |
| Per-process `McpToolRegistry` (no global singleton) | `agent.py` (top-level `REGISTRY`) |
| Workload identity — `NonHumanIdentity` + `LocalIdPStub` | `agent.py` (top-level `IDENTITY`) |
| Dispatcher refuses auth-required tools without identity (v0.5.0 C5) | `EnterpriseLLM(identity=IDENTITY, ...)` |
| Default middleware chain — sanitize → PII → OTel → policy → validate | `agent.build_client` |
| JSON policy gate (Cedar-shaped) | `configs/policy.json` |
| A2A AgentCard (capability advertisement) | `configs/agent_card.json` |
| Pre-flight balance check before transfer | `agent.execute_transfer` |
| Eval golden-set (drives `eap eval` from the project root) | `tests/golden_set.json` |

For tool authoring conventions, see
[user-guide-aws-agentcore.md §1.5](../../docs/user-guide-aws-agentcore.md).
For identity wiring (swapping `LocalIdPStub` for AgentCore's
`OIDCTokenExchange.from_agentcore(...)`), see §1.6. For deploy +
registry publishing, see §1.14 and §1.17.

## Run locally (no cloud credentials needed)

```bash
cd examples/transactional-agent
python agent.py
```

Expected output:

```
{'status': 'ok', 'from_id': 'acct-1', 'to_id': 'acct-2', 'amount_cents': 1000, 'idempotency_key': '...'}
```

The agent looks up `acct-1`, verifies it has at least 1,000 cents, then
calls the auth-required `transfer_funds` tool with a fresh UUID
idempotency key. `LocalIdPStub` signs the workload identity locally
so the dispatcher's auth-required check passes — fine for tests and
the in-tree example. In production, swap for a real IdP
(`OIDCTokenExchange.from_agentcore(...)` on AWS, the equivalent on
Vertex).

## Run the eval

```bash
uv run eap eval --dataset tests/golden_set.json --threshold 0.5
```

Drives each case through `agent.execute_transfer`, scores the
trajectory, and exits non-zero on regression. Drop into CI to turn
correctness regressions into failed builds. See user-guide §1.16 for
live `AgentCoreEvalScorer` / Ragas composition.

## Files

```
examples/transactional-agent/
├── agent.py              # business logic + middleware chain + identity + entry point
├── tools/
│   ├── get_account.py    # read-only @mcp_tool
│   └── transfer_funds.py # auth-required @mcp_tool with idempotency-key dedup
├── configs/
│   ├── policy.json       # Cedar-shaped JSON policy
│   └── agent_card.json   # A2A AgentCard
├── tests/
│   └── golden_set.json   # eval cases
├── responses.yaml        # canned LocalRuntimeAdapter responses
├── pyproject.toml
└── README.md             # this file
```

## What's next

- For a **retrieval-style** template (RAG-backed reasoning), see
  [`examples/research-agent`](../research-agent/README.md).
- For a **full cloud reference** wiring AgentCore identity,
  observability, memory, registry, payments, and eval — including a
  real `OIDCTokenExchange.from_agentcore(...)` identity — see
  [`examples/agentcore-bank-agent`](../agentcore-bank-agent/README.md).
- For the end-to-end tutorial, see
  [`docs/user-guide-aws-agentcore.md`](../../docs/user-guide-aws-agentcore.md)
  or
  [`docs/user-guide-gcp-vertex.md`](../../docs/user-guide-gcp-vertex.md).
