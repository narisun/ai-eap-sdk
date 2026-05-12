"""BaseRuntimeAdapter ABC and adapter-side data types."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from typing import Any, ClassVar

from pydantic import BaseModel, Field

from eap_core.types import Request


class RawResponse(BaseModel):
    text: str
    usage: dict[str, int] = Field(default_factory=dict)
    finish_reason: str | None = None
    raw: dict[str, Any] = Field(default_factory=dict)


class RawChunk(BaseModel):
    index: int
    text: str
    finish_reason: str | None = None
    usage: dict[str, int] = Field(default_factory=dict)


class ModelInfo(BaseModel):
    name: str
    provider: str | None = None
    capabilities: list[str] = Field(default_factory=list)


class BaseRuntimeAdapter(ABC):
    """Vendor-neutral runtime adapter ABC.

    Exception contract: implementations MUST translate vendor-specific
    exceptions into the canonical types in :mod:`eap_core.runtimes.errors`
    (``RuntimeAdapterError`` and subclasses). See
    ``BedrockRuntimeAdapter._map_botocore_error`` and
    ``VertexRuntimeAdapter._map_google_error`` for reference mappings.

    Adapters MAY raise :class:`RuntimeAdapterError` directly (with a
    helpful message) for vendor errors that don't fit a more specific
    subclass — keep the original exception accessible via ``__cause__``
    so audit logs can inspect the vendor payload (i.e., use
    ``raise CanonicalError(msg) from vendor_exc``).
    """

    name: ClassVar[str]

    @abstractmethod
    async def generate(self, req: Request) -> RawResponse: ...

    @abstractmethod
    async def stream(self, req: Request) -> AsyncIterator[RawChunk]: ...

    @abstractmethod
    async def list_models(self) -> list[ModelInfo]: ...

    async def aclose(self) -> None:
        return None
