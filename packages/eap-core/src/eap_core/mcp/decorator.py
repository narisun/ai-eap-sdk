"""@mcp_tool decorator — generates JSON Schema from type hints."""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Any, Protocol, get_type_hints

from pydantic import TypeAdapter

from eap_core.mcp.types import ToolSpec


class _SpecCarrier(Protocol):
    """Structural protocol for functions decorated by `@mcp_tool`.

    The decorator attaches a `.spec` attribute to the wrapped callable so
    downstream code (registry, tests, server) can read tool metadata
    without a separate lookup. Typing the decorator's return as this
    protocol lets mypy see the `.spec` attribute on consumers.
    """

    spec: ToolSpec

    def __call__(self, *args: Any, **kwargs: Any) -> Any: ...


def _schema_for_param(annotation: Any) -> dict[str, Any]:
    try:
        return TypeAdapter(annotation).json_schema()
    except Exception:
        return {"type": "object"}


def _build_input_schema(fn: Callable[..., Any]) -> dict[str, Any]:
    sig = inspect.signature(fn)
    # ``include_extras=True`` preserves ``Annotated[T, Field(...)]``
    # metadata so pydantic's ``TypeAdapter`` incorporates Field
    # constraints (ge, le, description, etc.) into the generated JSON
    # schema. Without it, ``Annotated[int, Field(ge=1, le=1000)] = 100``
    # collapses to bare ``{"type": "integer"}``.
    hints = get_type_hints(fn, include_extras=True)
    properties: dict[str, Any] = {}
    required: list[str] = []
    for name, param in sig.parameters.items():
        if name == "self":
            continue
        ann = hints.get(name, str)
        prop = _schema_for_param(ann)
        if param.default is not inspect.Parameter.empty:
            # Capture the function default in the schema. JSON Schema
            # treats ``default`` as informational metadata — clients
            # can show it in prompts; it doesn't constrain validation.
            prop = {**prop, "default": param.default}
        properties[name] = prop
        if param.default is inspect.Parameter.empty:
            required.append(name)
    schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return schema


def _build_output_schema(fn: Callable[..., Any]) -> dict[str, Any] | None:
    hints = get_type_hints(fn, include_extras=True)
    ret = hints.get("return")
    if ret is None or ret is type(None):
        return None
    return _schema_for_param(ret)


def mcp_tool(
    *,
    name: str | None = None,
    description: str | None = None,
    requires_auth: bool = False,
) -> Callable[[Callable[..., Any]], _SpecCarrier]:
    def wrap(fn: Callable[..., Any]) -> _SpecCarrier:
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
        return fn  # type: ignore[return-value]

    return wrap
