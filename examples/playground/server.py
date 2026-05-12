"""EAP-Core playground server — browser UI for the example agents.

Discovers every ``examples/*/agent.py`` that exports ``build_client()``,
mounts a single-page UI at ``/``, and exposes a JSON API:

- ``GET  /api/agents``                       list discovered agents + tool names
- ``POST /api/agents/{name}/chat``           send a message; returns text + trace
- ``POST /api/agents/{name}/tools/{tool}``   invoke a specific tool directly

Discovery scans the playground's parent directory (``examples/``) for
subdirectories containing an ``agent.py`` whose module exports
``build_client``. The cheap pre-check is a substring search for
``def build_client`` against the file contents — avoids importing every
example on startup. Each agent is imported lazily on first reference
and the resulting :class:`eap_core.EnterpriseLLM` cached for the
server's lifetime.

Side effect: ``sys.path`` is mutated per agent so sibling imports
(``from tools import ...``) resolve. Paths accumulate across loads —
acceptable for a localhost-only playground.
"""

from __future__ import annotations

import importlib.util
import logging
import sys
import traceback
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.middleware.trustedhost import TrustedHostMiddleware

# Make tracing.py importable when server.py is run as a script *or*
# imported through ``sys.path.insert(0, '.../playground')``. The
# fall-back covers the in-tree TestClient smoke test where the
# playground dir might not yet be on ``sys.path``.
_LOG = logging.getLogger(__name__)

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from tracing import (  # noqa: E402  (path bootstrap above must run first)
    get_current_trace,
    install_trace,
)

_EXAMPLES_ROOT = _HERE.parent  # ``examples/``
_STATIC = _HERE / "static"

# Module-level registries — populated lazily.
_agents: dict[str, Any] = {}
_agent_modules: dict[str, Any] = {}
_agent_errors: dict[str, str] = {}


# ---- Discovery -----------------------------------------------------------


def _discover_agents() -> dict[str, Path]:
    """Return ``{agent_name: path/to/agent.py}`` for every example that
    exposes ``build_client``. Skips the playground itself and any
    project without an ``agent.py``.
    """
    found: dict[str, Path] = {}
    if not _EXAMPLES_ROOT.is_dir():
        return found
    for project_dir in sorted(_EXAMPLES_ROOT.iterdir()):
        if not project_dir.is_dir() or project_dir.name == "playground":
            continue
        agent_py = project_dir / "agent.py"
        if not agent_py.is_file():
            continue
        try:
            source = agent_py.read_text(encoding="utf-8")
        except OSError:
            continue
        # Cheap substring guard. Cheaper than importing every example
        # on startup, and good enough — the only false positive would
        # be a comment containing the literal text, which would then
        # surface as a load error in ``/api/agents`` anyway.
        if "def build_client" in source:
            found[project_dir.name] = agent_py
    return found


def _purge_sibling_modules(agent_dir: Path) -> None:
    """Evict cached top-level packages that collide across examples.

    Multiple example agents have their own ``tools/`` subpackage. If
    agent A is imported first, ``sys.modules['tools']`` is bound to
    A's package; importing agent B's ``from tools.x import y`` then
    silently uses A's package and fails with ``ModuleNotFoundError``.

    We solve this by purging any already-imported top-level module
    whose file lives under a *different* example directory whenever a
    new agent is loaded. Names like ``eap_core`` (loaded from
    ``site-packages``) are left untouched.
    """
    examples_root = _EXAMPLES_ROOT.resolve()
    target_dir = agent_dir.resolve()
    for mod_name in list(sys.modules):
        if mod_name.startswith("_playground_agents."):
            continue
        mod = sys.modules.get(mod_name)
        mod_file = getattr(mod, "__file__", None) or getattr(mod, "__path__", None)
        # ``__path__`` is a list-like for regular packages — take its
        # first entry. Namespace packages expose ``_NamespacePath``
        # which is iterable but NOT a ``list``; coerce defensively and
        # bail out if nothing string-like comes out the other side.
        if mod_file is not None and not isinstance(mod_file, str):
            try:
                mod_file = next(iter(mod_file), None)
            except TypeError:
                mod_file = None
        # ``Path()`` doesn't accept ``bytes`` — narrow to ``str`` only
        # here so mypy sees the same precondition the runtime enforces.
        if not isinstance(mod_file, str):
            continue
        try:
            mod_path = Path(mod_file).resolve()
        except (OSError, ValueError, TypeError):
            continue
        try:
            rel = mod_path.relative_to(examples_root)
        except ValueError:
            continue  # not under examples/ — leave it alone
        # First path component is the example project name.
        owning_example = examples_root / rel.parts[0]
        if owning_example != target_dir:
            del sys.modules[mod_name]


def _load_agent(name: str) -> Any:
    """Lazy-load + cache an agent by name.

    On first reference: add the agent's directory to ``sys.path``,
    import its ``agent.py`` via ``importlib.util``, call
    ``build_client()``, install the trace middleware + registry
    wrapper, and cache the resulting client.
    """
    if name in _agents:
        return _agents[name]
    if name in _agent_errors:
        raise HTTPException(500, _agent_errors[name])

    discovered = _discover_agents()
    if name not in discovered:
        raise HTTPException(404, f"agent {name!r} not found")
    agent_py = discovered[name]

    # Evict cached top-level modules (``tools``, ``configs``, …) from
    # other examples so this agent's sibling imports bind to its own
    # subpackages, not whichever example loaded first.
    _purge_sibling_modules(agent_py.parent)

    # Sibling imports (``from tools import …``) need the agent's
    # directory on ``sys.path``. We insert at position 0 so this
    # example's ``tools/`` wins over any other example whose directory
    # is still on the path from a prior load.
    agent_dir = str(agent_py.parent)
    if agent_dir in sys.path:
        sys.path.remove(agent_dir)
    sys.path.insert(0, agent_dir)

    module_name = f"_playground_agents.{name.replace('-', '_')}"
    spec = importlib.util.spec_from_file_location(module_name, agent_py)
    if spec is None or spec.loader is None:
        msg = f"failed to build module spec for {agent_py}"
        _agent_errors[name] = msg
        raise HTTPException(500, msg)

    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        msg = f"failed to import agent {name!r}: {exc}\n{traceback.format_exc()}"
        _agent_errors[name] = msg
        raise HTTPException(500, msg) from exc

    build = getattr(module, "build_client", None)
    if build is None:
        msg = f"agent {name!r} has no ``build_client`` function"
        _agent_errors[name] = msg
        raise HTTPException(500, msg)

    try:
        client = build()
    except Exception as exc:
        msg = f"build_client() raised for agent {name!r}: {exc}\n{traceback.format_exc()}"
        _agent_errors[name] = msg
        raise HTTPException(500, msg) from exc

    # Best-effort tracing installation. Failures here are not fatal —
    # the chat endpoint still works, the trace panel is just empty.
    try:
        install_trace(client)
    except Exception:
        _LOG.warning("install_trace failed for agent %r", name, exc_info=True)

    _agents[name] = client
    _agent_modules[name] = module
    return client


# ---- API models ----------------------------------------------------------


class AgentInfo(BaseModel):
    name: str
    description: str = ""
    tool_names: list[str] = []
    error: str | None = None


class ChatRequest(BaseModel):
    message: str


class ChatResponse(BaseModel):
    text: str
    trace: list[dict[str, Any]] = []


class ToolInvocation(BaseModel):
    arguments: dict[str, Any] = {}


class ToolResult(BaseModel):
    result: Any = None


# ---- App + routes --------------------------------------------------------


app = FastAPI(title="EAP-Core Playground")

# Block DNS-rebind attacks. The playground binds to 127.0.0.1 by
# design, but a malicious page on ``evil.example.com`` whose A record
# resolves to 127.0.0.1 can still drive cross-site requests against
# this server unless the ``Host`` header is validated. Reject any
# request whose ``Host`` is not one of the localhost spellings (with
# or without the playground's default port). ``TestClient`` defaults
# the host to ``testserver``; tests therefore override the ``Host``
# header explicitly rather than embed the test-only sentinel into the
# production allow-list.
app.add_middleware(
    TrustedHostMiddleware,
    allowed_hosts=[
        "127.0.0.1",
        "127.0.0.1:8765",
        "localhost",
        "localhost:8765",
    ],
)


@app.get("/api/agents", response_model=list[AgentInfo])  # type: ignore[misc,untyped-decorator,unused-ignore]
async def list_agents() -> list[AgentInfo]:
    """List every example agent the playground discovered.

    Load failures surface as ``AgentInfo`` entries with a populated
    ``error`` field rather than crashing the listing — the frontend
    can still show the agent dropdown and report which entries are
    broken.
    """
    discovered = _discover_agents()
    result: list[AgentInfo] = []
    for name in discovered:
        try:
            client = _load_agent(name)
        except HTTPException as exc:
            result.append(
                AgentInfo(name=name, description="", tool_names=[], error=str(exc.detail))
            )
            continue
        except Exception as exc:  # belt + braces
            result.append(AgentInfo(name=name, description="", tool_names=[], error=str(exc)))
            continue

        registry = getattr(client, "_tool_registry", None)
        tool_names: list[str] = []
        if registry is not None:
            try:
                tool_names = sorted(t.name for t in registry.list_tools())
            except Exception:
                tool_names = []

        doc = (_agent_modules[name].__doc__ or "").strip()
        description = doc.split("\n", 1)[0] if doc else ""
        result.append(AgentInfo(name=name, description=description, tool_names=tool_names))
    return result


@app.post("/api/agents/{name}/chat", response_model=ChatResponse)  # type: ignore[misc,untyped-decorator,unused-ignore]
async def chat(name: str, body: ChatRequest) -> ChatResponse:
    """Send a single message to the agent. Returns the response text
    plus the per-request tool-call trace captured by the playground's
    tracing wrapper.

    Each call is independent — the playground does not maintain
    multi-turn conversation state. Agents that need history can wire
    their own ``MemoryStore``.
    """
    client = _load_agent(name)
    try:
        result = await client.generate_text(body.message)
    except Exception as exc:
        raise HTTPException(500, f"generate_text raised: {exc}") from exc

    # ``generate_text`` returns a ``Response`` (``.text``). Be liberal
    # in case a future runtime returns a plain string.
    text = result.text if hasattr(result, "text") else str(result)
    return ChatResponse(text=text, trace=get_current_trace())


@app.post("/api/agents/{name}/tools/{tool}", response_model=ToolResult)  # type: ignore[misc,untyped-decorator,unused-ignore]
async def invoke_tool(name: str, tool: str, body: ToolInvocation) -> ToolResult:
    """Invoke a tool on the agent directly, bypassing the LLM. Useful
    for testing tool wiring without burning LLM tokens.

    Goes through ``EnterpriseLLM.invoke_tool`` so the full middleware
    pipeline (policy gates, observability spans, identity plumbing)
    fires exactly as it would in production.
    """
    client = _load_agent(name)
    registry = getattr(client, "_tool_registry", None)
    if registry is None:
        raise HTTPException(400, f"agent {name!r} has no tool registry")
    # Pre-check that the tool is known to this agent's registry before
    # dispatching. Without this, ``client.invoke_tool`` raises
    # ``MCPError("tool not found in registry")`` which the broad
    # ``except`` below would map to a 500 — symmetric with the agent-
    # not-found path which correctly returns 404. ``McpToolRegistry.get``
    # returns ``ToolSpec | None`` so a ``None`` here means the tool is
    # genuinely absent.
    if registry.get(tool) is None:
        raise HTTPException(404, f"tool {tool!r} not found on agent {name!r}")
    try:
        result = await client.invoke_tool(tool, body.arguments)
    except Exception as exc:
        raise HTTPException(500, f"tool {tool!r} raised: {exc}") from exc
    return ToolResult(result=result)


# ---- Static files (frontend; populated by T2) ----------------------------


if _STATIC.is_dir():
    app.mount("/static", StaticFiles(directory=_STATIC), name="static")


@app.get("/")  # type: ignore[misc,untyped-decorator,unused-ignore]
async def index() -> FileResponse:
    """Serve the SPA shell. Returns a 503 until T2 ships the frontend."""
    index_html = _STATIC / "index.html"
    if not index_html.is_file():
        raise HTTPException(
            503,
            "frontend not built yet — see examples/playground/README.md "
            "(static/index.html will be added by T2)",
        )
    return FileResponse(index_html)


def main() -> None:
    """Entry point for ``python server.py``."""
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8765, log_level="info")


if __name__ == "__main__":
    main()
