# eap-core

Enterprise Agentic AI Platform SDK — core middleware, runtime adapters, identity.

**Not on public PyPI.** Install from this repository:

```bash
# Inside the workspace
uv sync --all-packages --group dev

# As a downstream dep (pin to a tag)
uv add "eap-core @ git+https://github.com/narisun/ai-eap-sdk.git@v0.6.0#subdirectory=packages/eap-core"
uv add "eap-core[pii,otel] @ git+https://github.com/narisun/ai-eap-sdk.git@v0.6.0#subdirectory=packages/eap-core"
```

See the top-level `README.md` for usage and the full install matrix
including all optional extras (`pii`, `otel`, `mcp`, `a2a`, `eval`,
`policy-cedar`, `aws`, `gcp`).

The full design lives at
`docs/superpowers/specs/2026-05-10-eap-core-design.md`. The developer
guide for extending the SDK lives at `docs/developer-guide.md`.
