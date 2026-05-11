# bank-agent — AWS Bedrock AgentCore reference

Full reference implementation that mirrors
[`docs/user-guide-aws-agentcore.md`](../../docs/user-guide-aws-agentcore.md)
end-to-end. Use it as a starting point or read it side-by-side with
the user guide.

**What it demonstrates:**

| User-guide step | Where in this project |
|---|---|
| 1.6 Identity (`OIDCTokenExchange.from_agentcore`) | `cloud_wiring.build_identity` |
| 1.7 Observability (`configure_for_agentcore`) | `cloud_wiring.wire_observability` |
| 1.8 Memory (`AgentCoreMemoryStore`) | `cloud_wiring.build_memory` |
| 1.9 Code Interpreter tools | `cloud_wiring.register_cloud_tools` |
| 1.10 Browser tools | `cloud_wiring.register_cloud_tools` |
| 1.14 Registry (`RegistryClient`) | `cloud_wiring.build_registry` |
| 1.15 Payments (`PaymentClient` + `PaymentRequired`) | `cloud_wiring.build_payments` + `agent.execute_transfer` |
| 1.16 Evaluations (`AgentCoreEvalScorer`) | `cloud_wiring.build_eval_scorer` |
| 1.17 Deploy | `eap deploy --runtime agentcore` (run from this dir) |

The middleware chain (prompt-injection sanitization, PII masking,
OTel attributes, policy enforcement, output validation) is in
`agent.build_client`.

## Run locally (no AWS credentials needed)

```bash
cd examples/agentcore-bank-agent
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
- Code Interpreter / Browser tools are not registered.
- Identity uses `LocalIdPStub` — fine for tests.

Every cloud-backed seam is exercised by the agent code through the
Protocol — flipping to live mode is a constructor change, nothing
above.

## Graduate to live AgentCore calls

```bash
export EAP_ENABLE_REAL_RUNTIMES=1
export AWS_REGION=us-east-1
# Plus standard AWS creds: env vars / ~/.aws/credentials / IAM role.
python agent.py
```

In **live mode**:

- `MemoryStore` is `AgentCoreMemoryStore` against the
  `bank-agent-memory` memory id.
- `AgentRegistry` is `RegistryClient` against the
  `bank-platform` registry.
- `PaymentBackend` is `PaymentClient` against the
  `bank-agent-wallet` wallet provider, with a $5.00 session budget.
- Code Interpreter + Browser MCP tools are registered (eight total).
- `AgentCoreEvalScorer` is available for `EvalRunner`.

You'll need to provision those AgentCore resources (memory bank,
registry, wallet) in the AWS console first.

## Smoke-test the wiring without running the full agent

```bash
python cloud_wiring.py
```

Prints which subsystems are live vs. stub. Useful for verifying your
env flags / credentials are picked up before you exercise real
traffic.

## Deploy to AgentCore Runtime

```bash
EAP_ENABLE_REAL_DEPLOY=1 uv run eap deploy --runtime agentcore --service bank-agent
```

Produces `dist/agentcore/` with the ARM64 Dockerfile, FastAPI
handler, and a README walking through the ECR push +
AgentCore Runtime registration. See user-guide §1.17 for the full
deploy flow.

## Run the eval

```bash
uv run eap eval --dataset tests/golden_set.json --threshold 0.5
```

In stub mode the local `FaithfulnessScorer` runs. In live mode,
compose with `AgentCoreEvalScorer` by editing `agent.py` — see
user-guide §1.16.

## Files

```
examples/agentcore-bank-agent/
├── agent.py              # business logic + middleware chain + entry point
├── cloud_wiring.py       # every AgentCore integration constructor in one place
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
