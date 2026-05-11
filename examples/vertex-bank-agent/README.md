# bank-agent — GCP Vertex Agent Engine reference

Full reference implementation that mirrors
[`docs/user-guide-gcp-vertex.md`](../../docs/user-guide-gcp-vertex.md)
end-to-end. Use it as a starting point or read it side-by-side with
the user guide.

The structure (and the `agent.py` business logic) is identical to
the AWS version at
[`examples/agentcore-bank-agent/`](../agentcore-bank-agent/) —
only the integration constructors in `cloud_wiring.py` differ. The
Protocol seams (`MemoryStore`, `CodeSandbox`, `BrowserSandbox`,
`AgentRegistry`, `PaymentBackend`) make the swap a constructor
change.

**What it demonstrates:**

| User-guide step | Where in this project |
|---|---|
| 1.6 Identity (`VertexAgentIdentityToken`) | `cloud_wiring.build_identity` |
| 1.7 Observability (`configure_for_vertex_observability`) | `cloud_wiring.wire_observability` |
| 1.8 Memory (`VertexMemoryBankStore`) | `cloud_wiring.build_memory` |
| 1.9 Code Sandbox tools | `cloud_wiring.register_cloud_tools` |
| 1.10 Browser Sandbox tools | `cloud_wiring.register_cloud_tools` |
| 1.14 Registry (`VertexAgentRegistry`) | `cloud_wiring.build_registry` |
| 1.15 Payments (`AP2PaymentClient` + `PaymentRequired`) | `cloud_wiring.build_payments` + `agent.execute_transfer` |
| 1.16 Evaluations (`VertexEvalScorer`) | `cloud_wiring.build_eval_scorer` |
| 1.17 Deploy | `eap deploy --runtime vertex-agent-engine` (run from this dir) |

The middleware chain (prompt-injection sanitization, PII masking,
OTel attributes, policy enforcement, output validation) is in
`agent.build_client`.

## Run locally (no GCP credentials needed)

```bash
cd examples/vertex-bank-agent
python agent.py
```

Expected output:

```
=== bank-agent (mode: STUB) ===
balance: {'id': 'acct-1', 'balance_cents': 50000, 'owner': 'alice', 'source': 'fresh'}
transfer: {'status': 'ok', 'from_id': 'acct-1', 'to_id': 'acct-2', ...}
balance (re-read): {'id': 'acct-1', 'balance_cents': 50000, 'source': 'cache'}
published to registry: rec-1
cloud tools: none (set EAP_ENABLE_REAL_RUNTIMES=1 to wire them)
```

In **stub mode** (the default):

- `MemoryStore` is `InMemoryStore` — values survive within the process.
- `AgentRegistry` is `InMemoryAgentRegistry` — `publish` returns a
  fake record id.
- `PaymentBackend` is `InMemoryPaymentBackend` — budget bookkeeping
  works, but no real signing.
- Code + Browser Sandbox tools are not registered.

Every cloud-backed seam is exercised by the agent code through the
Protocol — flipping to live mode is a constructor change, nothing
above.

## Graduate to live Vertex calls

```bash
export EAP_ENABLE_REAL_RUNTIMES=1
export GOOGLE_CLOUD_PROJECT=my-gcp-project
export GOOGLE_CLOUD_LOCATION=us-central1
gcloud auth application-default login
python agent.py
```

In **live mode**:

- `MemoryStore` is `VertexMemoryBankStore` against the
  `bank-agent-memory` memory bank.
- `AgentRegistry` is `VertexAgentRegistry` against the
  `bank-platform` registry.
- `PaymentBackend` is `AP2PaymentClient` against the
  `bank-agent-wallet` wallet provider, with a $5.00 session budget.
- Code + Browser Sandbox MCP tools are registered (eight total).
- `VertexEvalScorer` is available for `EvalRunner`.

You'll need to provision those Vertex resources (memory bank,
registry, wallet) in the GCP console first. The service account
attached to the workload (or your local ADC) needs
`roles/aiplatform.user` at minimum.

## Smoke-test the wiring without running the full agent

```bash
python cloud_wiring.py
```

Prints which subsystems are live vs. stub. Useful for verifying your
env flags / credentials are picked up before you exercise real
traffic.

## Deploy to Vertex Agent Runtime

```bash
EAP_ENABLE_REAL_DEPLOY=1 GOOGLE_CLOUD_PROJECT=my-gcp-project \
  uv run eap deploy --runtime vertex-agent-engine --service bank-agent --region us-central1 \
  --allow-unauthenticated
```

We pass `--allow-unauthenticated` here because the example targets
local smoke testing. For real deployment, pass
`--auth-discovery-url + --auth-issuer + --auth-audience` instead —
see user-guide §1.17.

Produces `dist/vertex-agent-engine/` with the linux/amd64 Dockerfile,
FastAPI handler, and a README walking through the Artifact Registry
push + Vertex Agent Engine registration. See user-guide §1.17 for
the full deploy flow.

## Run the eval

```bash
uv run eap eval --dataset tests/golden_set.json --threshold 0.5
```

In stub mode the local `FaithfulnessScorer` runs. In live mode,
compose with `VertexEvalScorer` by editing `agent.py` — see
user-guide §1.16.

## Files

```
examples/vertex-bank-agent/
├── agent.py              # business logic + middleware chain + entry point
├── cloud_wiring.py       # every Vertex integration constructor in one place
├── tools/
│   ├── lookup_account.py # read-only @mcp_tool
│   └── transfer_funds.py # auth-required @mcp_tool
├── configs/
│   ├── policy.json       # Cedar-shaped JSON policy
│   └── agent_card.json   # A2A AgentCard
├── tests/
│   └── golden_set.json   # eval cases
├── responses.yaml        # canned local-runtime responses
├── pyproject.toml
└── README.md             # this file
```

## Cross-cloud comparison

The companion AWS Bedrock AgentCore example at
[`examples/agentcore-bank-agent/`](../agentcore-bank-agent/) has
the same `agent.py`, `tools/`, `configs/`, and `tests/`. The only
file that differs is `cloud_wiring.py`. Diff them side-by-side to
see exactly which lines change when you swap clouds:

```bash
diff examples/agentcore-bank-agent/cloud_wiring.py \
     examples/vertex-bank-agent/cloud_wiring.py
```
