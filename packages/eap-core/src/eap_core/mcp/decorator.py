"""@mcp_tool decorator — generates JSON Schema from type hints."""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Any, get_type_hints

from pydantic import TypeAdapter

from eap_core.mcp.types import ToolSpec


def _schema_for_param(annotation: Any) -> dict[str, Any]:
    try:
        return TypeAdapter(annotation).json_schema()
    except Exception:
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
