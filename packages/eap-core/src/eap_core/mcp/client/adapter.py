"""Adapter from MCP server tools to local EAP-Core ``ToolSpec`` entries.

Replaces the v1.0 example shim
(``examples/cross-domain-agent/mcp_client_adapter.py``)'s
``build_tool_specs`` function with an SDK-level equivalent that:

- Iterates the pool's handles, namespacing each remote tool as
  ``<server-name>__<remote-tool-name>`` so multiple servers can coexist
  in one :class:`McpToolRegistry` without colliding on names like
  ``query_sql`` (every validation server exposes one).
- Builds the forwarder via the :func:`_build_forwarder_spec` factory
  rather than inlining ``async def _forward`` inside the ``for`` loop.
  Inlining would close over the LOOP variables ``handle`` and
  ``remote_tool_name``, and every forwarder would invoke the LAST tool on
  the LAST server. The factory pattern pins the per-iteration values via
  function parameters. The test
  ``test_forwarder_invokes_correct_remote_tool_with_kwargs`` is the
  load-bearing mutation guard against this regression.
- Decodes ``CallToolResult.content`` with the same pattern as the v0.7.1
  fix in ``eap_core.mcp.server._serialize_for_text_content``: try
  ``json.loads`` first, fall back to raw ``.text`` for primitives
  (str/int that were ``str()``-cast server-side).
- Catches :class:`McpServerDisconnectedError`, invokes
  :meth:`McpClientPool.reconnect`, and then re-raises. The caller (the
  LLM tool-picker or a higher-level retry policy) decides whether to
  re-issue the call against the fresh session. The forwarder does NOT
  auto-retry — auto-retry semantics are deferred to v1.2 alongside the
  proper per-handle ``AsyncExitStack`` unwind.

Non-text content (``ImageContent``, ``EmbeddedResource``) is passed
through as the raw upstream object — the agent layer decides what to do
with images and resources. Decoding them is out of scope for v1.1.

Output-schema validation (v1.1, opt-in via
:attr:`McpServerConfig.validate_output_schemas`) threads each tool's
remote ``outputSchema`` (captured at pool-spawn time on
:attr:`McpServerHandle.tool_output_schemas`) into the forwarder. After
decoding the response the forwarder hands it to :func:`_maybe_validate`,
which performs a shallow required-keys check and raises
:class:`McpOutputSchemaError` on mismatch. The validator is intentionally
shallow — pydantic v2 doesn't ship a JSON-Schema-to-Model compiler and
adding the ``jsonschema`` package as a dep for a feature most servers
don't even use today would be over-engineered. A deeper validator is
flagged as a v1.2 follow-up.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from eap_core.mcp.client.errors import (
    McpOutputSchemaError,
    McpServerDisconnectedError,
)
from eap_core.mcp.registry import McpToolRegistry
from eap_core.mcp.types import ToolSpec

if TYPE_CHECKING:
    from eap_core.mcp.client.pool import McpClientPool


def build_tool_registry(pool: McpClientPool) -> McpToolRegistry:
    """Build an :class:`McpToolRegistry` from a live :class:`McpClientPool`.

    For every tool on every server reported by ``pool.handles()``, registers
    a forwarder :class:`ToolSpec` named ``<server-name>__<tool-name>`` whose
    ``fn`` calls through ``pool.session(server_name).call_tool(...)``.

    This function is intentionally NOT in
    ``eap_core.mcp.client.__all__`` — callers should use
    :meth:`McpClientPool.build_tool_registry` (which delegates here). Keeping
    the function importable is useful for the example-migration shim in T4,
    but it isn't part of the documented public surface.
    """
    registry = McpToolRegistry()
    for handle in pool.handles():
        # Only consult the per-tool ``outputSchema`` map when the config
        # explicitly opts in. When opt-out (default), the factory binds
        # ``schema_to_validate=None`` so the forwarder skips validation
        # entirely without per-call attribute lookup.
        validate = handle.config.validate_output_schemas
        for remote_tool_name in handle.tool_names:
            local_name = f"{handle.config.name}__{remote_tool_name}"
            schema_to_validate: dict[str, Any] | None = None
            if validate:
                schema_to_validate = handle.tool_output_schemas.get(remote_tool_name)
            spec = _build_forwarder_spec(
                pool=pool,
                server_name=handle.config.name,
                remote_name=remote_tool_name,
                local_name=local_name,
                schema_to_validate=schema_to_validate,
            )
            registry.register(spec)
    return registry


def _build_forwarder_spec(
    *,
    pool: McpClientPool,
    server_name: str,
    remote_name: str,
    local_name: str,
    schema_to_validate: dict[str, Any] | None = None,
) -> ToolSpec:
    """Factory that builds one forwarder ``ToolSpec``.

    All four arguments are captured by FUNCTION PARAMETER, not by loop
    variable. The inner ``_forward`` closure binds the parameter names,
    which are fresh per call to this factory — so every forwarder ends up
    calling its OWN ``server_name`` / ``remote_name``, not the last
    iteration's values.

    Same closure-capture lesson as the v1.0 example shim's ``_build_one``
    helper. The test
    ``test_forwarder_invokes_correct_remote_tool_with_kwargs`` exercises
    at least two tools on the same server so the bug would manifest as
    "both forwarders call tool B" when only one should.
    """

    async def _forward(**kwargs: Any) -> Any:
        try:
            session = pool.session(server_name)
            response = await session.call_tool(remote_name, kwargs)
        except McpServerDisconnectedError:
            # Spawn a fresh session/subprocess so the next call routes
            # through it, then re-raise so the caller decides whether
            # to retry. Auto-retry is deferred to v1.2 — see adapter
            # docstring.
            await pool.reconnect(server_name)
            raise
        decoded = _decode_response(response)
        return _maybe_validate(
            decoded,
            schema=schema_to_validate,
            server_name=server_name,
            tool=remote_name,
        )

    return ToolSpec(
        name=local_name,
        description=f"[remote: {server_name}] {remote_name}",
        input_schema={"type": "object"},  # Permissive — the remote validates.
        output_schema=None,
        fn=_forward,
        requires_auth=False,
        is_async=True,
    )


def _decode_response(response: Any) -> Any:
    """Decode an upstream ``CallToolResult`` into an agent-level value.

    Mirrors the v0.7.1 server-side serialisation contract (see
    ``eap_core.mcp.server._serialize_for_text_content``):

    - ``None`` if ``response.content`` is empty.
    - For the common single-``TextContent`` shape: try ``json.loads`` first
      (BaseModel/dict/list returns are JSON-encoded server-side); fall
      through to the raw ``.text`` for primitives (str/int that were
      ``str()``-cast).
    - For non-text content (``ImageContent``, ``EmbeddedResource``), pass
      the raw upstream object through. The agent layer decides what to do
      with images/resources; decoding is out of scope for v1.1.
    """
    if not response.content:
        return None
    first = response.content[0]
    if not hasattr(first, "text"):
        # ImageContent / EmbeddedResource — passthrough. The caller can
        # inspect ``response.content`` directly for rich types.
        return first
    try:
        return json.loads(first.text)
    except (json.JSONDecodeError, ValueError):
        # Primitive str/int returns are ``str()``-cast server-side and
        # won't parse as JSON. Return raw text.
        return first.text


def _maybe_validate(
    decoded: Any,
    *,
    schema: dict[str, Any] | None,
    server_name: str,
    tool: str,
) -> Any:
    """Shallow output-schema validation for a decoded tool response.

    When ``schema`` is ``None`` (the most common case — most MCP servers
    don't yet publish ``outputSchema``) this is a no-op pass-through. When
    a schema is present, the function performs a minimal required-keys
    check against the response shape and raises
    :class:`McpOutputSchemaError` on mismatch; otherwise it returns the
    ``decoded`` value unchanged.

    The implementation is intentionally **shallow**: it only honours
    ``type: "object"`` schemas with a ``required`` list, asserting every
    listed key is present in the decoded dict. Nested-shape validation,
    type-level coercion, ``$ref`` resolution, etc. are NOT performed.
    Reasoning:

    - pydantic v2 doesn't ship a JSON-Schema → Model compiler. A deep
      validator would require adding ``jsonschema`` as a dependency for
      a feature most servers don't even use (most MCP tools don't yet
      advertise an ``outputSchema``). The cost/benefit of dragging
      ``jsonschema`` into eap-core's base deps doesn't pay off in v1.1.
    - The shallow check catches the highest-value class of contract
      drift: a remote server adding/renaming/removing top-level keys
      between deployments. Subtler shape drift (a list field becoming
      a scalar, an integer field becoming a string) is left for v1.2
      alongside the proper JSON-Schema validator.

    The ``server_name`` argument is currently informational (used in
    docstrings and for callers grepping logs); future deeper validation
    may include it in the error message. It is accepted today to avoid
    an additional signature churn when the deeper validator lands.
    """
    if schema is None:
        return decoded
    # Only validate "type": "object" schemas with a "required" list.
    # Anything else (no type, primitive type, schema-less dict) is
    # treated as "no actionable constraint" — return the value as-is.
    if not isinstance(schema, dict) or schema.get("type") != "object":
        return decoded
    required = schema.get("required") or []
    if not isinstance(required, list) or not required:
        return decoded
    if not isinstance(decoded, dict):
        raise McpOutputSchemaError(
            tool=tool,
            payload=decoded,
            schema=schema,
            reason=(f"expected object matching outputSchema, got {type(decoded).__name__}"),
        )
    missing = [k for k in required if k not in decoded]
    if missing:
        raise McpOutputSchemaError(
            tool=tool,
            payload=decoded,
            schema=schema,
            reason=f"missing required keys: {sorted(missing)}",
        )
    return decoded
