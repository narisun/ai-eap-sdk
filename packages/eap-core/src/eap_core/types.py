"""Public data types for EAP-Core."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Role = Literal["system", "user", "assistant", "tool"]


class Message(BaseModel):
    model_config = ConfigDict(frozen=False)
    role: Role
    content: str | list[dict[str, Any]]
    name: str | None = None


class Request(BaseModel):
    model: str
    messages: list[Message] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    schema_name: str | None = None
    stream: bool = False
    options: dict[str, Any] = Field(default_factory=dict)


class Response(BaseModel):
    text: str
    payload: Any = None
    usage: dict[str, int] = Field(default_factory=dict)
    finish_reason: str | None = None
    raw: dict[str, Any] = Field(default_factory=dict)


class Chunk(BaseModel):
    index: int
    text: str
    finish_reason: str | None = None


@dataclass
class Context:
    """Per-request mutable container shared across middlewares."""

    vault: dict[str, str] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    span: Any = None
    identity: Any = None
    request_id: str = ""
