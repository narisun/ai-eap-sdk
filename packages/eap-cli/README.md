# eap-cli

The `eap` CLI — the **golden path** for scaffolding and ops on
EAP-Core agentic AI projects. Every command delegates to a pure-Python
scaffolder, so you can use the library directly if you prefer; the CLI
just gives you the canonical, idiomatic shape (default middleware
chain wired, JSON policy in the right place, A2A AgentCard with the
right schema, eval golden-set ready to run) in one command.

**Not on public PyPI.** Install from this repository.

## Install

The canonical install snippets — pin convention, workspace sync, and
downstream `uv add` flow — live in the top-level
[`README.md` §Install](../../README.md#install). The version pin is
`@v0.6.0`. `eap-cli` re-exports `eap-core`'s extras (so an
`eap-cli[aws]` install pulls `boto3` for the AgentCore-aware
`eap deploy --runtime agentcore` codepath).

## Command surface

| Command | One-line purpose |
|---|---|
| `eap init <DIR>` | Scaffold a new agent project (agent.py, middleware chain, example tool, policy, AgentCard, golden-set). |
| `eap create-agent --template research\|transactional` | Overlay a retrieval-style or action-style agent template on the current project. |
| `eap create-tool --name <name> --mcp [--auth-required]` | Add a typed Python `@mcp_tool` function; JSON Schema generated from your type hints. |
| `eap create-mcp-server <DIR>` | Scaffold a standalone MCP-stdio server project (no LLM, just tools). |
| `eap eval --dataset <path>` | Drive an agent against a golden-set JSON, score the trajectories, exit non-zero on regression. |
| `eap deploy --runtime aws\|gcp\|agentcore\|vertex-agent-engine` | Package the current project for the chosen runtime (Lambda zip, Cloud Run dir, AgentCore ARM64 image, Vertex Agent Engine image). |
| `eap publish-to-gateway --gateway-url <url>` | Register the project's MCP tools with an AgentCore Gateway (or symmetric Vertex registry) so other agents can discover and invoke them. |

For tutorial-style coverage of each command in context, see the
[AWS user guide](../../docs/user-guide-aws-agentcore.md) and
[GCP user guide](../../docs/user-guide-gcp-vertex.md). For the
reference description of every flag, see the
[Root README §CLI reference](../../README.md#cli-reference).

## Quick links

- [Root README](../../README.md) — value statement, install matrix,
  CLI reference, quick start, production checklist.
- [Developer guide](../../docs/developer-guide.md) — architecture,
  extension points, public-surface stability table, cookbooks (incl.
  §5.7 "Add a new cloud-platform integration").
- [AWS user guide](../../docs/user-guide-aws-agentcore.md)
- [GCP user guide](../../docs/user-guide-gcp-vertex.md)
- [AWS integration reference](../../docs/integrations/aws-bedrock-agentcore.md)
- [GCP integration reference](../../docs/integrations/gcp-vertex-agent-engine.md)
- [CHANGELOG](../../CHANGELOG.md)

## Specification

The CLI specification lives at
`docs/superpowers/specs/2026-05-10-eap-core-design.md` §13.
