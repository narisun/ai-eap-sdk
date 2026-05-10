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
    """Per-request mutable container shared across middlewares.

    Fields:
        vault: PII re-identification table; keys are ``<TYPE_xxxxxxxx>``
            tokens, values are the original PII fragments. Scoped to
            this request only.
        metadata: Free-form scratch space shared across middlewares.
            Convention: namespace keys (``gen_ai.*``, ``policy.*``,
            ``tenant.*``, etc.).
        span: Active OpenTelemetry span if observability middleware ran.
        identity: ``NonHumanIdentity`` for this request.
        request_id: UUID for tracing and correlation.
        memory_store: Optional backend implementing the ``MemoryStore``
            Protocol from ``eap_core.memory``. Tools and middleware
            can read/write conversational or long-term agent memory
            here when a backend is wired in.
        session_id: Identifier for the session this request belongs to.
            Memory operations use this to isolate per-session data.
    """

    vault: dict[str, str] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    span: Any = None
    identity: Any = None
    request_id: str = ""
    memory_store: Any = None
    session_id: str = ""
