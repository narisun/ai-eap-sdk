# EAP-Core Standards Layer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wire the open standards layer onto the EAP-Core foundation: MCP tool registry + `@mcp_tool` decorator, in-process `invoke_tool` dispatch, A2A AgentCard, and the optional MCP stdio server / A2A FastAPI router. Also fixes the Presidio integration deferred from Plan 1.

**Architecture:** Tools register once via the `@mcp_tool` decorator and are reachable two ways from the same `McpToolRegistry`: in-process via `EnterpriseLLM.invoke_tool()` (which runs the dispatch through the existing middleware chain) and over MCP stdio when the optional `[mcp]` extra is installed. AgentCards are auto-built from the registry so an agent's advertised skills always match its actual tools.

**Tech Stack:** Python 3.11+, Pydantic v2, official `mcp` SDK (extra), FastAPI (extra), Presidio's `AnonymizerEngine` for the PII fix.

**Spec reference:** `docs/superpowers/specs/2026-05-10-eap-core-design.md` §8, §9.
**Predecessor:** Plan 1 — `docs/superpowers/plans/2026-05-10-eap-core-foundation.md` (the eap-core foundation must be in place before this plan starts).

---

## File Structure

```
packages/eap-core/src/eap_core/
├── mcp/
│   ├── __init__.py
│   ├── types.py            # ToolSpec, MCPError
│   ├── decorator.py        # @mcp_tool
│   ├── registry.py         # McpToolRegistry (singleton + factory)
│   └── server.py           # Stdio MCP server runner (uses official mcp SDK; [mcp] extra)
├── a2a/
│   ├── __init__.py
│   ├── card.py             # AgentCard model + build_card
│   └── server.py           # FastAPI router exposing /.well-known/agent-card.json ([a2a] extra)
└── client.py               # MODIFY: wire invoke_tool to McpToolRegistry

packages/eap-core/tests/
├── test_mcp_decorator.py
├── test_mcp_registry.py
├── test_invoke_tool.py
├── test_a2a_card.py
└── extras/
    ├── test_mcp_server.py     # [mcp] extra
    └── test_a2a_server.py     # [a2a] extra

packages/eap-core/src/eap_core/middleware/
└── pii.py                  # MODIFY: AnonymizerEngine integration for engine="presidio"
```

---

## Task 1: MCP types and ToolSpec

**Files:**
- Create: `packages/eap-core/src/eap_core/mcp/__init__.py`
- Create: `packages/eap-core/src/eap_core/mcp/types.py`
- Create: `packages/eap-core/tests/test_mcp_types.py`

- [ ] **Step 1: Write the failing test**

`packages/eap-core/tests/test_mcp_types.py`:
```python
import pytest
from pydantic import ValidationError

from eap_core.mcp.types import MCPError, ToolSpec


def test_tool_spec_minimal():
    spec = ToolSpec(name="get_balance", description="...", input_schema={}, output_schema=None, fn=lambda: None)
    assert spec.name == "get_balance"
    assert spec.requires_auth is False


def test_tool_spec_rejects_empty_name():
    with pytest.raises(ValidationError):
        ToolSpec(name="", description="x", input_schema={}, output_schema=None, fn=lambda: None)


def test_mcp_error_carries_tool_name():
    e = MCPError(tool_name="x", message="boom")
    assert e.tool_name == "x"
    assert "boom" in str(e)
```

- [ ] **Step 2: Run, expect ModuleNotFoundError**

Run: `.venv/bin/python -m pytest packages/eap-core/tests/test_mcp_types.py -v`

- [ ] **Step 3: Implement `mcp/types.py`**

```python
"""MCP-side data types."""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ToolSpec(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    name: str
    description: str
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] | None = None
    fn: Callable[..., Any]
    requires_auth: bool = False
    is_async: bool = True

    @field_validator("name")
    @classmethod
    def _name_nonempty(cls, v: str) -> str:
        if not v:
            raise ValueError("ToolSpec.name must be non-empty")
        return v


class MCPError(Exception):
    def __init__(self, tool_name: str, message: str) -> None:
        super().__init__(f"{tool_name}: {message}")
        self.tool_name = tool_name
        self.message = message
```

- [ ] **Step 4: Implement `mcp/__init__.py`**

```python
from eap_core.mcp.types import MCPError, ToolSpec

__all__ = ["MCPError", "ToolSpec"]
```

- [ ] **Step 5: Run, expect 3 PASS.**

- [ ] **Step 6: Commit**

```bash
git add packages/eap-core/src/eap_core/mcp/__init__.py \
        packages/eap-core/src/eap_core/mcp/types.py \
        packages/eap-core/tests/test_mcp_types.py
git commit -m "feat(mcp): add ToolSpec and MCPError types"
```

---

## Task 2: `@mcp_tool` decorator

**Files:**
- Create: `packages/eap-core/src/eap_core/mcp/decorator.py`
- Create: `packages/eap-core/tests/test_mcp_decorator.py`

- [ ] **Step 1: Write the failing test**

`packages/eap-core/tests/test_mcp_decorator.py`:
```python
from typing import Annotated

from pydantic import BaseModel, Field

from eap_core.mcp.decorator import mcp_tool


def test_decorator_extracts_name_from_function_default():
    @mcp_tool()
    async def lookup_user(user_id: str) -> str:
        """Look up a user by id."""
        return user_id

    assert lookup_user.spec.name == "lookup_user"
    assert "Look up a user" in lookup_user.spec.description


def test_decorator_overrides_name_and_description():
    @mcp_tool(name="get_user", description="custom desc")
    async def lookup_user(user_id: str) -> str:
        return user_id

    assert lookup_user.spec.name == "get_user"
    assert lookup_user.spec.description == "custom desc"


def test_decorator_generates_input_schema_from_primitives():
    @mcp_tool()
    async def add(a: int, b: int = 0) -> int:
        """Sum two ints."""
        return a + b

    schema = add.spec.input_schema
    assert schema["type"] == "object"
    assert "a" in schema["properties"]
    assert "b" in schema["properties"]
    assert schema["properties"]["a"]["type"] == "integer"
    assert "a" in schema["required"]
    assert "b" not in schema.get("required", [])


def test_decorator_generates_input_schema_from_pydantic_model():
    class Query(BaseModel):
        text: str
        limit: int = 10

    @mcp_tool()
    async def search(q: Query) -> list[str]:
        return []

    schema = search.spec.input_schema
    # The arg `q` is a complex type, so the schema includes a $ref or inlines the model
    assert schema["type"] == "object"
    assert "q" in schema["properties"]


def test_decorator_marks_requires_auth():
    @mcp_tool(requires_auth=True)
    async def protected_op() -> None:
        pass

    assert protected_op.spec.requires_auth is True


def test_decorator_preserves_callable():
    @mcp_tool()
    async def echo(x: str) -> str:
        return x

    # the decorated symbol must still be awaitable
    import asyncio
    assert asyncio.run(echo("hi")) == "hi"
```

- [ ] **Step 2: Run, expect ModuleNotFoundError.**

- [ ] **Step 3: Implement `mcp/decorator.py`**

```python
"""@mcp_tool decorator — generates JSON Schema from type hints."""
from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Any, get_type_hints

from pydantic import BaseModel, TypeAdapter

from eap_core.mcp.types import ToolSpec


def _schema_for_param(annotation: Any) -> dict[str, Any]:
    try:
        return TypeAdapter(annotation).json_schema()
    except Exception:  # noqa: BLE001
        return {"type": "object"}


def _build_input_schema(fn: Callable[..., Any]) -> dict[str, Any]:
    sig = inspect.signature(fn)
    hints = get_type_hints(fn)
    properties: dict[str, Any] = {}
    required: list[str] = []
    for name, param in sig.parameters.items():
        if name == "self":
            continue
        ann = hints.get(name, str)
        properties[name] = _schema_for_param(ann)
        if param.default is inspect.Parameter.empty:
            required.append(name)
    schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return schema


def _build_output_schema(fn: Callable[..., Any]) -> dict[str, Any] | None:
    hints = get_type_hints(fn)
    ret = hints.get("return")
    if ret is None or ret is type(None):
        return None
    return _schema_for_param(ret)


def mcp_tool(
    *,
    name: str | None = None,
    description: str | None = None,
    requires_auth: bool = False,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def wrap(fn: Callable[..., Any]) -> Callable[..., Any]:
        spec = ToolSpec(
            name=name or fn.__name__,
            description=description or (inspect.getdoc(fn) or "").strip(),
            input_schema=_build_input_schema(fn),
            output_schema=_build_output_schema(fn),
            fn=fn,
            requires_auth=requires_auth,
            is_async=inspect.iscoroutinefunction(fn),
        )
        fn.spec = spec  # type: ignore[attr-defined]
        return fn
    return wrap
```

- [ ] **Step 4: Run, expect 6 PASS.**

- [ ] **Step 5: Commit**

```bash
git add packages/eap-core/src/eap_core/mcp/decorator.py \
        packages/eap-core/tests/test_mcp_decorator.py
git commit -m "feat(mcp): add @mcp_tool decorator with JSON Schema generation"
```

---

## Task 3: McpToolRegistry

**Files:**
- Create: `packages/eap-core/src/eap_core/mcp/registry.py`
- Create: `packages/eap-core/tests/test_mcp_registry.py`
- Modify: `packages/eap-core/src/eap_core/mcp/__init__.py`

- [ ] **Step 1: Write the failing test**

`packages/eap-core/tests/test_mcp_registry.py`:
```python
import pytest

from eap_core.mcp.decorator import mcp_tool
from eap_core.mcp.registry import McpToolRegistry, default_registry
from eap_core.mcp.types import MCPError


@pytest.fixture
def reg():
    return McpToolRegistry()


async def test_register_and_dispatch(reg: McpToolRegistry):
    @mcp_tool()
    async def add(a: int, b: int) -> int:
        return a + b
    reg.register(add.spec)
    result = await reg.invoke("add", {"a": 2, "b": 3})
    assert result == 5


async def test_invoke_unknown_tool_raises(reg: McpToolRegistry):
    with pytest.raises(MCPError, match="not found"):
        await reg.invoke("missing", {})


async def test_invoke_validates_args_against_schema(reg: McpToolRegistry):
    @mcp_tool()
    async def add(a: int, b: int) -> int:
        return a + b
    reg.register(add.spec)
    with pytest.raises(MCPError, match="validation"):
        await reg.invoke("add", {"a": "not-an-int", "b": 3})


def test_list_tools_returns_specs(reg: McpToolRegistry):
    @mcp_tool()
    async def echo(x: str) -> str:
        return x
    reg.register(echo.spec)
    specs = reg.list_tools()
    assert len(specs) == 1
    assert specs[0].name == "echo"


async def test_invoke_supports_sync_function(reg: McpToolRegistry):
    @mcp_tool()
    def doubler(x: int) -> int:
        return x * 2
    reg.register(doubler.spec)
    result = await reg.invoke("doubler", {"x": 5})
    assert result == 10


def test_default_registry_is_singleton():
    a = default_registry()
    b = default_registry()
    assert a is b
```

- [ ] **Step 2: Run, expect ModuleNotFoundError.**

- [ ] **Step 3: Implement `mcp/registry.py`**

```python
"""McpToolRegistry — discovery and dispatch for MCP-decorated tools."""
from __future__ import annotations

import asyncio
import inspect
from typing import Any

from jsonschema import ValidationError as JsonSchemaError
from jsonschema import validate as jsonschema_validate

from eap_core.mcp.types import MCPError, ToolSpec


class McpToolRegistry:
    def __init__(self) -> None:
        self._specs: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        self._specs[spec.name] = spec

    def get(self, name: str) -> ToolSpec | None:
        return self._specs.get(name)

    def list_tools(self) -> list[ToolSpec]:
        return list(self._specs.values())

    async def invoke(self, name: str, args: dict[str, Any]) -> Any:
        spec = self._specs.get(name)
        if spec is None:
            raise MCPError(tool_name=name, message="tool not found in registry")
        if spec.input_schema:
            try:
                jsonschema_validate(args, spec.input_schema)
            except JsonSchemaError as e:
                raise MCPError(tool_name=name, message=f"input validation failed: {e.message}") from e
        try:
            if spec.is_async:
                return await spec.fn(**args)
            return await asyncio.to_thread(spec.fn, **args)
        except MCPError:
            raise
        except Exception as e:  # noqa: BLE001
            raise MCPError(tool_name=name, message=f"tool raised: {e}") from e


_DEFAULT: McpToolRegistry | None = None


def default_registry() -> McpToolRegistry:
    """Module-level singleton the @mcp_tool decorator can auto-register into."""
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = McpToolRegistry()
    return _DEFAULT
```

- [ ] **Step 4: Add `jsonschema` to default deps**

Edit `packages/eap-core/pyproject.toml` — append to `dependencies`:
```toml
"jsonschema>=4.21",
```

Sync the new dep:
```bash
/Users/admin-h26/EAAP/ai-eap-sdk/.venv/bin/pip install "jsonschema>=4.21"
```

- [ ] **Step 5: Update `mcp/__init__.py`**

```python
from eap_core.mcp.decorator import mcp_tool
from eap_core.mcp.registry import McpToolRegistry, default_registry
from eap_core.mcp.types import MCPError, ToolSpec

__all__ = [
    "MCPError",
    "McpToolRegistry",
    "ToolSpec",
    "default_registry",
    "mcp_tool",
]
```

- [ ] **Step 6: Run, expect 6 PASS.**

- [ ] **Step 7: Commit**

```bash
git add packages/eap-core/src/eap_core/mcp/registry.py \
        packages/eap-core/src/eap_core/mcp/__init__.py \
        packages/eap-core/pyproject.toml \
        packages/eap-core/tests/test_mcp_registry.py
git commit -m "feat(mcp): add McpToolRegistry with input-schema validation and sync/async dispatch"
```

---

## Task 4: Wire `EnterpriseLLM.invoke_tool` to the registry

**Files:**
- Modify: `packages/eap-core/src/eap_core/client.py`
- Create: `packages/eap-core/tests/test_invoke_tool.py`

- [ ] **Step 1: Write the failing test**

`packages/eap-core/tests/test_invoke_tool.py`:
```python
import pytest

from eap_core.client import EnterpriseLLM
from eap_core.config import RuntimeConfig
from eap_core.exceptions import PolicyDeniedError
from eap_core.mcp.decorator import mcp_tool
from eap_core.mcp.registry import McpToolRegistry
from eap_core.mcp.types import MCPError
from eap_core.middleware.policy import JsonPolicyEvaluator, PolicyMiddleware


PERMIT_ALL = {
    "version": "1",
    "rules": [{"id": "permit-all", "effect": "permit", "principal": "*", "action": "*", "resource": "*"}],
}


async def test_invoke_tool_dispatches_via_registry():
    reg = McpToolRegistry()

    @mcp_tool()
    async def double(n: int) -> int:
        return n * 2

    reg.register(double.spec)

    client = EnterpriseLLM(
        RuntimeConfig(provider="local", model="echo-1"),
        middlewares=[PolicyMiddleware(JsonPolicyEvaluator(PERMIT_ALL))],
        tool_registry=reg,
    )
    result = await client.invoke_tool("double", {"n": 21})
    assert result == 42


async def test_invoke_tool_unknown_raises_mcp_error():
    client = EnterpriseLLM(
        RuntimeConfig(provider="local", model="echo-1"),
        middlewares=[PolicyMiddleware(JsonPolicyEvaluator(PERMIT_ALL))],
        tool_registry=McpToolRegistry(),
    )
    with pytest.raises(MCPError):
        await client.invoke_tool("nonexistent", {})


async def test_invoke_tool_runs_through_policy_middleware():
    """Policy denies tool actions when no rule permits the tool name."""
    deny_writes = {
        "version": "1",
        "rules": [
            {"id": "permit-reads", "effect": "permit", "principal": "*", "action": ["tool:read_account"], "resource": "*"},
            {"id": "deny-default", "effect": "forbid", "principal": "*", "action": ["tool:transfer"], "resource": "*"},
        ],
    }
    reg = McpToolRegistry()

    @mcp_tool()
    async def transfer(amount: int) -> str:
        return "ok"

    reg.register(transfer.spec)

    client = EnterpriseLLM(
        RuntimeConfig(provider="local", model="echo-1"),
        middlewares=[PolicyMiddleware(JsonPolicyEvaluator(deny_writes))],
        tool_registry=reg,
    )
    with pytest.raises(PolicyDeniedError):
        await client.invoke_tool("transfer", {"amount": 100})
```

- [ ] **Step 2: Run, expect 3 FAILS (the client doesn't have `invoke_tool` yet).**

- [ ] **Step 3: Modify `client.py` to accept `tool_registry` and implement `invoke_tool`**

Add the import at the top:
```python
from eap_core.mcp.registry import McpToolRegistry
from eap_core.mcp.types import MCPError, ToolSpec
```

Update `EnterpriseLLM.__init__` to accept `tool_registry: McpToolRegistry | None = None` (alongside existing args), and store it:
```python
def __init__(
    self,
    runtime_config: RuntimeConfig,
    middlewares: list[Middleware] | None = None,
    identity: NonHumanIdentity | None = None,
    registry: AdapterRegistry | None = None,
    tool_registry: McpToolRegistry | None = None,
) -> None:
    self._config = runtime_config
    self._registry = registry or AdapterRegistry.from_entry_points()
    self._adapter: BaseRuntimeAdapter = self._registry.create(runtime_config)
    self._pipeline = MiddlewarePipeline(middlewares or [])
    self._identity = identity
    self._tool_registry = tool_registry
```

Add the `invoke_tool` method:
```python
async def invoke_tool(self, tool_name: str, args: dict[str, Any]) -> Any:
    if self._tool_registry is None:
        raise MCPError(tool_name=tool_name, message="no tool registry configured on EnterpriseLLM")
    spec = self._tool_registry.get(tool_name)
    if spec is None:
        raise MCPError(tool_name=tool_name, message="tool not found in registry")

    ctx = Context(request_id=uuid.uuid4().hex, identity=self._identity)
    # Build a synthetic Request so the middleware chain still runs (sanitize/PII/policy).
    req = Request(
        model=self._config.model,
        messages=[],
        metadata={
            "operation_name": "invoke_tool",
            "action": f"tool:{tool_name}",
            "resource": tool_name,
            "tool_args": args,
        },
    )

    async def terminal(r: Request, c: Context) -> Response:
        # Read potentially-mutated args back from metadata so PII masking applies.
        invoked_args = r.metadata.get("tool_args", args)
        result = await self._tool_registry.invoke(tool_name, invoked_args)
        return Response(text=str(result), payload=result)

    resp = await self._pipeline.run(req, ctx, terminal)
    return resp.payload
```

- [ ] **Step 4: Run, expect 3 PASS. Confirm full suite still green.**

- [ ] **Step 5: Commit**

```bash
git add packages/eap-core/src/eap_core/client.py \
        packages/eap-core/tests/test_invoke_tool.py
git commit -m "feat(client): wire invoke_tool through middleware chain to McpToolRegistry"
```

---

## Task 5: A2A AgentCard model + `build_card`

**Files:**
- Create: `packages/eap-core/src/eap_core/a2a/__init__.py`
- Create: `packages/eap-core/src/eap_core/a2a/card.py`
- Create: `packages/eap-core/tests/test_a2a_card.py`

- [ ] **Step 1: Write the failing test**

`packages/eap-core/tests/test_a2a_card.py`:
```python
import pytest

from eap_core.a2a.card import AgentCard, Skill, build_card
from eap_core.mcp.decorator import mcp_tool
from eap_core.mcp.registry import McpToolRegistry


def test_agent_card_serializes_to_dict():
    card = AgentCard(
        name="research-agent",
        description="answers research questions",
        skills=[Skill(name="search", description="search docs", input_schema={}, output_schema=None)],
        endpoints={"http": "https://agent.example/v1"},
        authentication={"type": "oauth2.1"},
    )
    d = card.model_dump()
    assert d["name"] == "research-agent"
    assert d["skills"][0]["name"] == "search"
    assert d["authentication"]["type"] == "oauth2.1"


def test_build_card_reads_skills_from_registry():
    reg = McpToolRegistry()

    @mcp_tool(description="Look up an account.")
    async def lookup_account(id: str) -> dict:
        return {}

    @mcp_tool(description="Transfer funds.", requires_auth=True)
    async def transfer(amount: int) -> str:
        return "ok"

    reg.register(lookup_account.spec)
    reg.register(transfer.spec)

    card = build_card(
        name="bank-agent",
        description="helps with banking ops",
        skills_from=reg,
        auth="oauth2.1",
        endpoints={"http": "https://bank.example/v1"},
    )
    skill_names = {s.name for s in card.skills}
    assert {"lookup_account", "transfer"}.issubset(skill_names)
    assert card.authentication == {"type": "oauth2.1"}


def test_build_card_with_no_auth():
    reg = McpToolRegistry()
    card = build_card(name="x", description="y", skills_from=reg)
    assert card.authentication is None
```

- [ ] **Step 2: Run, expect ModuleNotFoundError.**

- [ ] **Step 3: Implement `a2a/card.py`**

```python
"""A2A AgentCard model and builder.

Reference: https://github.com/google/A2A — `/.well-known/agent-card.json` schema.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from eap_core.mcp.registry import McpToolRegistry


class Skill(BaseModel):
    name: str
    description: str
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] | None = None
    requires_auth: bool = False


class AgentCard(BaseModel):
    name: str
    description: str
    version: str = "0.1.0"
    skills: list[Skill] = Field(default_factory=list)
    endpoints: dict[str, str] = Field(default_factory=dict)
    authentication: dict[str, Any] | None = None


def build_card(
    *,
    name: str,
    description: str,
    skills_from: McpToolRegistry,
    auth: str | None = None,
    endpoints: dict[str, str] | None = None,
    version: str = "0.1.0",
) -> AgentCard:
    skills = [
        Skill(
            name=spec.name,
            description=spec.description,
            input_schema=spec.input_schema,
            output_schema=spec.output_schema,
            requires_auth=spec.requires_auth,
        )
        for spec in skills_from.list_tools()
    ]
    return AgentCard(
        name=name,
        description=description,
        version=version,
        skills=skills,
        endpoints=endpoints or {},
        authentication={"type": auth} if auth else None,
    )
```

- [ ] **Step 4: Implement `a2a/__init__.py`**

```python
from eap_core.a2a.card import AgentCard, Skill, build_card

__all__ = ["AgentCard", "Skill", "build_card"]
```

- [ ] **Step 5: Run, expect 3 PASS.**

- [ ] **Step 6: Commit**

```bash
git add packages/eap-core/src/eap_core/a2a/__init__.py \
        packages/eap-core/src/eap_core/a2a/card.py \
        packages/eap-core/tests/test_a2a_card.py
git commit -m "feat(a2a): add AgentCard model and build_card from McpToolRegistry"
```

---

## Task 6: A2A FastAPI router (`[a2a]` extra)

**Files:**
- Create: `packages/eap-core/src/eap_core/a2a/server.py`
- Create: `packages/eap-core/tests/extras/test_a2a_server.py`
- Modify: `packages/eap-core/src/eap_core/a2a/__init__.py`

- [ ] **Step 1: Write the extras test**

`packages/eap-core/tests/extras/test_a2a_server.py`:
```python
import pytest

pytest.importorskip("fastapi")
pytestmark = pytest.mark.extras

import httpx
from fastapi import FastAPI

from eap_core.a2a.card import AgentCard, Skill
from eap_core.a2a.server import mount_card_route


async def test_well_known_endpoint_returns_card():
    card = AgentCard(
        name="test-agent",
        description="t",
        skills=[Skill(name="echo", description="echo", input_schema={}, output_schema=None)],
    )
    app = FastAPI()
    mount_card_route(app, card)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as ac:
        resp = await ac.get("/.well-known/agent-card.json")
    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "test-agent"
    assert body["skills"][0]["name"] == "echo"
```

- [ ] **Step 2: Run, expect ModuleNotFoundError.**

- [ ] **Step 3: Implement `a2a/server.py`**

```python
"""A2A FastAPI server helpers — exposes `/.well-known/agent-card.json`."""
from __future__ import annotations

from typing import TYPE_CHECKING

from eap_core.a2a.card import AgentCard

if TYPE_CHECKING:
    from fastapi import FastAPI


def mount_card_route(app: "FastAPI", card: AgentCard) -> None:
    """Register GET /.well-known/agent-card.json on the given FastAPI app.

    Importing fastapi is deferred to call time so the [a2a] extra is only
    required for projects that actually serve the card.
    """
    try:
        from fastapi import APIRouter
    except ImportError as e:
        raise ImportError(
            "mount_card_route requires the [a2a] extra: pip install eap-core[a2a]"
        ) from e

    router = APIRouter()

    @router.get("/.well-known/agent-card.json")
    async def _agent_card() -> dict:
        return card.model_dump()

    app.include_router(router)
```

- [ ] **Step 4: Update `a2a/__init__.py`**

```python
from eap_core.a2a.card import AgentCard, Skill, build_card
from eap_core.a2a.server import mount_card_route

__all__ = ["AgentCard", "Skill", "build_card", "mount_card_route"]
```

- [ ] **Step 5: Run, expect 1 PASS (or SKIP if FastAPI missing).**

- [ ] **Step 6: Commit**

```bash
git add packages/eap-core/src/eap_core/a2a/server.py \
        packages/eap-core/src/eap_core/a2a/__init__.py \
        packages/eap-core/tests/extras/test_a2a_server.py
git commit -m "feat(a2a): add FastAPI route for /.well-known/agent-card.json"
```

---

## Task 7: MCP stdio server (`[mcp]` extra)

**Files:**
- Create: `packages/eap-core/src/eap_core/mcp/server.py`
- Create: `packages/eap-core/tests/extras/test_mcp_server.py`
- Modify: `packages/eap-core/src/eap_core/mcp/__init__.py`

- [ ] **Step 1: Write the extras test (in-process, not a real subprocess)**

`packages/eap-core/tests/extras/test_mcp_server.py`:
```python
import pytest

pytest.importorskip("mcp")
pytestmark = pytest.mark.extras

from eap_core.mcp.decorator import mcp_tool
from eap_core.mcp.registry import McpToolRegistry
from eap_core.mcp.server import build_mcp_server


async def test_build_mcp_server_registers_tools():
    """Smoke test: build_mcp_server returns an mcp.Server with our tools listed."""
    reg = McpToolRegistry()

    @mcp_tool()
    async def hello(who: str) -> str:
        """Say hello."""
        return f"hello {who}"

    reg.register(hello.spec)

    server = build_mcp_server(reg, server_name="test-eap")
    # The official mcp SDK exposes registered tools through the Server instance.
    # Exact API varies by SDK version; we only verify the server is built without error.
    assert server is not None
```

- [ ] **Step 2: Run, expect ModuleNotFoundError on `eap_core.mcp.server`.**

- [ ] **Step 3: Implement `mcp/server.py`**

```python
"""MCP stdio server — wraps McpToolRegistry as an MCP server.

The official `mcp` SDK is required ([mcp] extra). This module is
lazy-imported by callers that want to expose tools over stdio.
"""
from __future__ import annotations

from typing import Any

from eap_core.mcp.registry import McpToolRegistry


def build_mcp_server(registry: McpToolRegistry, *, server_name: str = "eap-core") -> Any:
    """Build an MCP server that exposes all tools in `registry` over stdio.

    Returns the underlying `mcp.server.Server` instance. The caller is
    responsible for running the stdio transport (typically via
    `await server.run(...)` or the SDK's `run_stdio` helper).
    """
    try:
        from mcp.server import Server  # type: ignore[import-not-found]
        from mcp.types import TextContent, Tool  # type: ignore[import-not-found]
    except ImportError as e:
        raise ImportError(
            "MCP stdio server requires the [mcp] extra: pip install eap-core[mcp]"
        ) from e

    server: Any = Server(server_name)

    @server.list_tools()
    async def _list_tools() -> list[Tool]:
        return [
            Tool(
                name=spec.name,
                description=spec.description,
                inputSchema=spec.input_schema,
            )
            for spec in registry.list_tools()
        ]

    @server.call_tool()
    async def _call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
        result = await registry.invoke(name, arguments)
        return [TextContent(type="text", text=str(result))]

    return server


async def run_stdio(registry: McpToolRegistry, *, server_name: str = "eap-core") -> None:
    """Convenience entry point: build the server and run it over stdio."""
    try:
        from mcp.server.stdio import stdio_server  # type: ignore[import-not-found]
    except ImportError as e:
        raise ImportError("MCP stdio runner requires the [mcp] extra") from e
    server = build_mcp_server(registry, server_name=server_name)
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())
```

- [ ] **Step 4: Update `mcp/__init__.py`**

```python
from eap_core.mcp.decorator import mcp_tool
from eap_core.mcp.registry import McpToolRegistry, default_registry
from eap_core.mcp.types import MCPError, ToolSpec

__all__ = [
    "MCPError",
    "McpToolRegistry",
    "ToolSpec",
    "default_registry",
    "mcp_tool",
]
```

(Don't import `server` from `__init__` — it's lazy on purpose to avoid pulling `mcp` SDK at import time.)

- [ ] **Step 5: Run extras test, expect PASS or SKIP.**

The `mcp` SDK API changed between 0.x and 1.x. If the API surface used in `build_mcp_server` doesn't match the installed version, get the test green by:
- Looking at the installed `mcp` version: `.venv/bin/python -c "import mcp; print(mcp.__version__)"`
- Reading the SDK's `mcp.server` exports.
- Adjusting decorators / type names to match.

If the SDK API is significantly different and the fix is non-trivial, mark the test `xfail` with a precise reason (analogous to the Presidio test) and report `DONE_WITH_CONCERNS`.

- [ ] **Step 6: Commit**

```bash
git add packages/eap-core/src/eap_core/mcp/server.py \
        packages/eap-core/src/eap_core/mcp/__init__.py \
        packages/eap-core/tests/extras/test_mcp_server.py
git commit -m "feat(mcp): add stdio server bridge from McpToolRegistry to official MCP SDK"
```

---

## Task 8: Fix Presidio integration via AnonymizerEngine

This task closes the xfail from Plan 1 by switching the Presidio path to use the official `AnonymizerEngine`, which handles overlapping spans correctly.

**Files:**
- Modify: `packages/eap-core/src/eap_core/middleware/pii.py`
- Modify: `packages/eap-core/tests/extras/test_pii_presidio.py` (remove xfail decorator)

- [ ] **Step 1: Replace the manual Presidio span-replacement with `AnonymizerEngine`**

In `pii.py`, replace `_mask_text`'s presidio branch with:

```python
def _mask_text(self, text: str, vault: dict[str, str]) -> str:
    if self._engine == "regex":
        return _replace_in_text(text, vault, self._patterns)
    # Presidio path — analyze + anonymize via the SDK to handle overlapping spans.
    analyzer, anonymizer = self._presidio  # type: ignore[misc]
    results = analyzer.analyze(text=text, language="en")
    if not results:
        return text
    # Build per-finding operators that emit a unique token AND stash the original.
    from presidio_anonymizer.entities import OperatorConfig  # type: ignore[import-not-found]

    operators: dict[str, OperatorConfig] = {}
    # Two-pass: anonymize once with placeholder operator, then post-process to
    # replace placeholders with vault tokens. Simpler: iterate findings sorted
    # right-to-left after anonymizer resolves overlaps.
    resolved = anonymizer.anonymize(
        text=text,
        analyzer_results=results,
        operators={"DEFAULT": OperatorConfig("replace", {"new_value": "<<PII>>"})},
    )
    # `resolved.items` lists each replaced span with its entity_type and the original text segment.
    out = resolved.text
    # Replace each <<PII>> placeholder, in order, with a unique tokenized name.
    for item in resolved.items:
        token = f"<{item.entity_type}_{uuid.uuid4().hex[:8]}>"
        # Pull original text from the source `text` using item.start/item.end.
        original = text[item.start : item.end]
        vault[token] = original
        out = out.replace("<<PII>>", token, 1)
    return out
```

- [ ] **Step 2: Remove xfail from the Presidio test**

In `packages/eap-core/tests/extras/test_pii_presidio.py`, remove the `@pytest.mark.xfail(...)` decorator.

- [ ] **Step 3: Run the Presidio test, expect PASS.**

Run: `.venv/bin/python -m pytest packages/eap-core/tests/extras/test_pii_presidio.py -v`

If anonymizer's `items` ordering or attribute names differ from this code, debug:
- `print(resolved.items)` to see the actual structure
- Adjust `item.start`/`item.end`/`item.entity_type` field names to match the installed `presidio-anonymizer` version

If the fix can't be made to pass within ~30 minutes, restore the xfail and report `DONE_WITH_CONCERNS` with a note on what the SDK API looks like.

- [ ] **Step 4: Run the full suite to confirm regex path still passes.**

Run: `.venv/bin/python -m pytest packages/eap-core/tests/test_pii.py packages/eap-core/tests/extras/test_pii_presidio.py -v`

Expected: 4 regex tests still PASS, Presidio test now PASS.

- [ ] **Step 5: Commit**

```bash
git add packages/eap-core/src/eap_core/middleware/pii.py \
        packages/eap-core/tests/extras/test_pii_presidio.py
git commit -m "fix(pii): use AnonymizerEngine for Presidio path to handle overlapping spans"
```

---

## Done conditions for this plan

When all tasks are complete:

1. `EnterpriseLLM(...).invoke_tool("name", args)` dispatches a registered tool through the full middleware chain.
2. `@mcp_tool` generates valid JSON Schema for primitives and Pydantic models.
3. A scaffolded FastAPI app can `mount_card_route(app, card)` and respond to `GET /.well-known/agent-card.json`.
4. `eap_core.mcp.server.build_mcp_server(registry)` returns an MCP-SDK server exposing the registry's tools.
5. The Presidio extras test passes (or is xfailed with a precise reason if the SDK API needed more research time).
6. Full suite green: `not extras and not cloud` ≥ 90% coverage; `extras` matrix runs locally for installed extras.
7. Plans 3 (eval) and 4 (CLI) build on this — `EnterpriseLLM.invoke_tool` and the registry are the foundation for trajectory recording in Plan 3 and the CLI's tool-scaffolding in Plan 4.
