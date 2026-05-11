"""Bridge between remote MCP servers (subprocess over stdio) and
EAP-Core's local @mcp_tool / McpToolRegistry surface.

This module exists because eap_core.mcp ships server-side primitives
(McpToolRegistry, @mcp_tool, run_stdio, build_mcp_server) but no
first-class client. An agent that wants to consume an external MCP
server has to:

1. Spawn the server as a subprocess.
2. Open an MCP stdio session (mcp.client.stdio.stdio_client).
3. List its tools.
4. For each remote tool, build a local wrapper that forwards
   call_tool requests through the open session.

This adapter does (1)-(4). It returns a list of ToolSpec values
ready for ``McpToolRegistry.register()``.

LIMITATION (see README.md): this is a per-agent shim, not a
general-purpose SDK feature. The official path would be a new
``eap_core.mcp.client`` module with structured config, session
lifecycle (pool/retry/timeout), output-schema validation, and
observability spans around remote calls. None of those live here.
"""

from __future__ import annotations

import json
from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import Any

from eap_core.mcp.types import ToolSpec


@dataclass
class ServerHandle:
    """Handle to one running MCP server subprocess. Created by
    ``connect_servers``; closed by exiting the AsyncExitStack returned
    alongside it."""

    name: str
    session: Any  # mcp.client.ClientSession - typed loosely so this
    #                module doesn't hard-import the upstream package at
    #                module-load time.
    tool_names: list[str]


async def connect_servers(
    server_configs: list[dict[str, Any]],
    stack: AsyncExitStack,
) -> list[ServerHandle]:
    """Spawn each MCP server subprocess and open an MCP stdio session
    to it. Returns one ``ServerHandle`` per server.

    Caller owns the ``AsyncExitStack`` - when the stack exits, all
    sessions and subprocesses are torn down.

    ``server_configs`` items shape::

        {"name": "bankdw", "command": "python", "args": ["server.py"],
         "cwd": Path("...")}

    Optional keys: ``env`` (dict[str, str]). When omitted the subprocess
    inherits the parent process environment.
    """
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    handles: list[ServerHandle] = []
    for cfg in server_configs:
        params = StdioServerParameters(
            command=cfg["command"],
            args=cfg["args"],
            cwd=str(cfg["cwd"]) if cfg.get("cwd") else None,
            env=cfg.get("env"),
        )
        read, write = await stack.enter_async_context(stdio_client(params))
        session = await stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        tools_response = await session.list_tools()
        handles.append(
            ServerHandle(
                name=cfg["name"],
                session=session,
                tool_names=[t.name for t in tools_response.tools],
            )
        )
    return handles


def build_tool_specs(handles: list[ServerHandle]) -> list[ToolSpec]:
    """For every remote tool on every connected server, build a local
    ``ToolSpec`` whose ``fn`` forwards to that remote tool. The remote
    tool name is namespaced as ``<server-name>__<tool-name>`` to avoid
    collisions (both validation servers expose ``query_sql``).

    Description is preserved from the remote ``tools/list`` response is
    available; the local description is augmented with a ``[remote: ...]``
    prefix so a downstream LLM tool-picker can see which server backs
    each tool. Input schema is left as a permissive ``{"type": "object"}``
    because we don't re-fetch the remote schema here; the remote
    validates on call.
    """
    specs: list[ToolSpec] = []
    for handle in handles:
        for remote_tool in handle.tool_names:
            local_name = f"{handle.name}__{remote_tool}"
            specs.append(_build_one(handle, remote_tool, local_name))
    return specs


def _build_one(handle: ServerHandle, remote_name: str, local_name: str) -> ToolSpec:
    """Factory that captures ``handle`` + ``remote_name`` as function
    parameters (NOT loop variables) so the closure inside ``_forward``
    binds the correct values for each remote tool.

    Inlining the ``async def _forward`` inside the ``for`` loop in
    ``build_tool_specs`` would close over the LOOP variables, and every
    forwarder would end up invoking the LAST tool on the LAST handle.
    Extracting this factory pins the per-iteration values.
    """

    async def _forward(**kwargs: Any) -> Any:
        response = await handle.session.call_tool(remote_name, kwargs)
        # ``response.content`` is a list[TextContent | ImageContent |
        # EmbeddedResource]. For the validation servers' DuckDB tools
        # it's a single TextContent whose ``.text`` holds the result
        # serialised by ``eap_core.mcp.server._serialize_for_text_content``
        # (JSON for BaseModel / dict / list, str() for primitives).
        if not response.content:
            return None
        first = response.content[0]
        if not hasattr(first, "text"):
            return None
        try:
            return json.loads(first.text)
        except (json.JSONDecodeError, ValueError):
            # Primitive str/int return — server-side str() output.
            return first.text

    return ToolSpec(
        name=local_name,
        description=f"[remote: {handle.name}] {remote_name}",
        input_schema={"type": "object"},  # Permissive - the remote validates.
        output_schema=None,
        fn=_forward,
        requires_auth=False,
        is_async=True,
    )
