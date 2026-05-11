# research-agent — retrieval-style reference project

Minimal reference for a **retrieval-style** EAP-Core agent: calls a
`search_docs` tool, then asks the LLM to summarize with the retrieved
docs as context. Good starting point for QA agents, research agents,
and RAG-backed assistants.

This template is what `eap create-agent --template research` generates.
Read it side-by-side with the user guides — every wiring decision here
maps to a step in the cloud-specific tutorials.

**What it demonstrates:**

| EAP-Core feature | Where in this project |
|---|---|
| `@mcp_tool` definition (typed Python → JSON Schema) | `tools/search_docs.py` |
| Per-process `McpToolRegistry` (no global singleton) | `agent.py` (top-level `REGISTRY`) |
| Default middleware chain — sanitize → PII → OTel → policy → validate | `agent.build_client` |
| JSON policy gate (Cedar-shaped) | `configs/policy.json` |
| A2A AgentCard (capability advertisement) | `configs/agent_card.json` |
| `EnterpriseLLM.invoke_tool` + `generate_text` (retrieve → reason → summarize) | `agent.answer` |
| `LocalRuntimeAdapter` (`provider="local"`) — no cloud creds needed | `RuntimeConfig` in `build_client` |
| Eval golden-set (drives `eap eval` from the project root) | `tests/golden_set.json` |

For tool authoring conventions, see
[user-guide-aws-agentcore.md §1.5](../../docs/user-guide-aws-agentcore.md)
(symmetric on the Vertex side). For identity wiring on auth-required
tools — see the transactional-agent template — see §1.6. For deploy
+ registry publishing, see §1.14 and §1.17.

## Run locally (no cloud credentials needed)

```bash
cd examples/research-agent
python agent.py
```

Expected output:

```
[local-runtime] received 35 tokens, model=echo-1
```

The `LocalRuntimeAdapter` echoes a deterministic token count for the
combined prompt (retrieval context + question) — enough to exercise
the full middleware chain end-to-end without contacting any LLM
provider. Swap `RuntimeConfig(provider="local", ...)` for `bedrock`
or `vertex` (and set `EAP_ENABLE_REAL_RUNTIMES=1`) to graduate to a
real model; the rest of the code is unchanged.

## Run the eval

```bash
uv run eap eval --dataset tests/golden_set.json --threshold 0.5
```

Drives each case through `agent.answer`, scores the trajectory with
`FaithfulnessScorer` + `DeterministicJudge`, and exits non-zero if any
case scores below the threshold. Drop into CI to turn faithfulness
regressions into failed builds. See user-guide §1.16 for live
`AgentCoreEvalScorer` / Ragas composition.

## Files

```
examples/research-agent/
├── agent.py              # business logic + middleware chain + entry point
├── tools/
│   └── search_docs.py    # stubbed retrieval tool; replace with your retriever
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

- For an **action-style** template that performs writes (with policy
  gates + idempotency keys), see
  [`examples/transactional-agent`](../transactional-agent/README.md).
- For a **full cloud reference** wiring AgentCore identity,
  observability, memory, registry, payments, and eval, see
  [`examples/agentcore-bank-agent`](../agentcore-bank-agent/README.md).
- For the end-to-end tutorial, see
  [`docs/user-guide-aws-agentcore.md`](../../docs/user-guide-aws-agentcore.md)
  or
  [`docs/user-guide-gcp-vertex.md`](../../docs/user-guide-gcp-vertex.md).
