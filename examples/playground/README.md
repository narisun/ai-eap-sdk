# EAP-Core Playground

Browser-based UI for testing the example agents in this repo.

The playground auto-discovers every `examples/*/agent.py` that exports
`build_client()` and exposes them in a dropdown. For each agent you
can chat with it and watch a live trace of every tool call the agent
made along the way — a debugging / learning aid for SDK users.

## Run

```bash
cd examples/playground
uv run --with eap-core --with fastapi --with uvicorn python server.py
```

Open <http://127.0.0.1:8765> in a browser.

## What it does

- **Chat** — send a message; see the response plus a trace of every
  tool call the agent made along the way.
- **Invoke a tool directly** — bypass the LLM and call a specific tool
  with custom arguments. Useful for testing your tool wiring without
  paying for LLM tokens.

All example agents in this repo use `provider="local"` (canned
responses from each agent's `responses.yaml`), so the playground works
without any LLM credentials configured. Tool invocations are real —
they exercise the actual SDK code paths including policy gates,
identity plumbing, and observability spans.

## API

The frontend is a thin layer over a JSON API:

| Method | Path | Body | Returns |
|---|---|---|---|
| `GET`  | `/api/agents`                       | — | `[{name, description, tool_names, error?}]` |
| `POST` | `/api/agents/{name}/chat`           | `{"message": "..."}` | `{"text": "...", "trace": [...]}` |
| `POST` | `/api/agents/{name}/tools/{tool}`   | `{"arguments": {...}}` | `{"result": ...}` |

Trace entries have the shape:

```json
{"kind": "tool_call", "name": "lookup_account",
 "args": {"account_id": "acct-1"},
 "result": {"id": "acct-1", "balance_cents": 12345},
 "duration_ms": 0.42, "ts_ms": 1.7}
```

## How tracing works

The SDK's `Middleware` Protocol exposes `on_request`, `on_response`,
`on_stream_chunk`, and `on_error` — there is **no** `on_tool_call`
hook. The playground therefore captures tool calls via a registry
wrapper: when an agent is first loaded, `tracing.install_trace()`
monkey-patches its `McpToolRegistry.invoke` with a traced version that
appends entries to a `ContextVar`-backed per-request buffer. A small
`PlaygroundTraceMiddleware` at the front of the pipeline resets the
buffer on every `on_request` so each `generate_text` call starts with
a fresh trace.

The wrapper is **playground-local** — it lives in `tracing.py` here,
not in `eap_core.middleware`. SDK users who want production tracing
should use `ObservabilityMiddleware` (OTel spans).

## Files

- `server.py` — FastAPI app, agent discovery, API endpoints
- `tracing.py` — `PlaygroundTraceMiddleware` + registry wrapper
- `static/` — frontend (HTML/JS/CSS); populated by T2
