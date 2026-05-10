# eap-cli

The `eap` CLI — scaffolding and ops for EAP-Core agentic AI projects.

**Not on public PyPI.** Install from this repository:

```bash
# Inside the workspace
uv sync --all-packages --group dev

# As a downstream dep (pin to a tag)
uv add "eap-cli @ git+https://github.com/narisun/ai-eap-sdk.git@v0.1.0#subdirectory=packages/eap-cli"
```

Then:

```bash
eap init my-agent
cd my-agent && python agent.py
```

See the top-level `README.md` for the full command surface and
`docs/superpowers/specs/2026-05-10-eap-core-design.md` §13 for the
specification.
