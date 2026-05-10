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
