# eap-core

Enterprise Agentic AI Platform SDK — the **thin bridge** that
centralizes cross-cutting concerns (prompt-injection sanitization, PII
masking, OTel observability, policy enforcement, output validation) in
a swappable middleware chain, while staying out of the way of business
logic. EAP-Core is built on open protocols (MCP, A2A, OTel GenAI,
OAuth 2.1 / RFC 8693) and every heavyweight integration (Presidio,
OpenTelemetry SDK, AWS, GCP, Cedar, Ragas, official MCP SDK, FastAPI)
lives behind an optional extra — lazy-imported, trivially replaceable.

**Not on public PyPI.** Install from this repository.

## Install

The canonical install snippets — pin convention, workspace sync, and
downstream `uv add` flow — live in the top-level
[`README.md` §Install](../../README.md#install). The version pin is
`@v0.6.3`; the matrix below covers the optional extras.

### Optional extras

| Extra | What it enables |
|---|---|
| `pii` | Microsoft Presidio for PII detection/masking (default uses a regex fallback) |
| `otel` | OpenTelemetry SDK + OTLP exporter (default uses a no-op tracer) |
| `aws` | `boto3` — AWS Bedrock Runtime adapter + AgentCore integration (identity, memory, registry, code-interpreter, browser, payments, eval) |
| `gcp` | `google-cloud-aiplatform` — Vertex AI runtime adapter + Vertex Agent Engine integration |
| `mcp` | Official `mcp` SDK — stdio MCP server transport (default uses the in-tree pure-Python registry) |
| `a2a` | `fastapi` — serve the A2A AgentCard over HTTP |
| `eval` | `ragas` adapter for `EvalRunner` (default uses the in-tree `FaithfulnessScorer`) |
| `policy-cedar` | `cedarpy` — real Cedar engine via `CedarPolicyEvaluator` (default uses the in-tree `JsonPolicyEvaluator`). See [developer-guide §4.4](../../docs/developer-guide.md#44-add-a-custom-policy-evaluator) for the swap. |

Install one at a time or all at once:

```bash
uv sync --all-packages --group dev --extra otel
uv sync --all-packages --all-extras --group dev
```

## Quick links

- [Root README](../../README.md) — value statement, install matrix,
  CLI reference, quick start, production checklist.
- [Developer guide](../../docs/developer-guide.md) — architecture,
  extension points, public-surface stability table, cookbooks.
- [AWS user guide](../../docs/user-guide-aws-agentcore.md) — end-to-end
  tutorial against AWS Bedrock AgentCore (11 services).
- [GCP user guide](../../docs/user-guide-gcp-vertex.md) — end-to-end
  tutorial against GCP Vertex Agent Engine.
- [AWS integration reference](../../docs/integrations/aws-bedrock-agentcore.md)
- [GCP integration reference](../../docs/integrations/gcp-vertex-agent-engine.md)
- [CHANGELOG](../../CHANGELOG.md) — per-release notes including
  migration recipes.

## Extending the SDK

The full design lives at
`docs/superpowers/specs/2026-05-10-eap-core-design.md`. The
[developer guide](../../docs/developer-guide.md) walks through every
extension point — custom middleware, runtime adapters, identity
providers, policy evaluators, scorers, MCP transports — with public-
surface stability annotations so you can tell at a glance what is safe
to depend on.
