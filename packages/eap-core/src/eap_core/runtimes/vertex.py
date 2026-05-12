"""GCP Vertex AI adapter (shape-correct stub)."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

from eap_core.config import RuntimeConfig
from eap_core.exceptions import RealRuntimeDisabledError
from eap_core.runtimes.base import BaseRuntimeAdapter, ModelInfo, RawChunk, RawResponse
from eap_core.runtimes.errors import (
    RuntimeAdapterError,
    RuntimeAuthError,
    RuntimeContextLengthError,
    RuntimeRateLimitError,
    RuntimeServerError,
    RuntimeTimeoutError,
)
from eap_core.types import Request

_GUIDE = (
    "Wire credentials and replace this stub. See docs/runtimes/vertex.md. "
    "Set EAP_ENABLE_REAL_RUNTIMES=1 to perform real calls (requires the [gcp] extra)."
)


def _real_runtimes_enabled() -> bool:
    return os.environ.get("EAP_ENABLE_REAL_RUNTIMES") == "1"


def _map_google_error(exc: Exception) -> RuntimeAdapterError:
    """Map ``google.api_core.exceptions`` failures to canonical EAP-Core errors.

    Callers MUST chain the original vendor exception via ``raise ... from exc``
    so ``__cause__`` preserves the underlying payload for audit inspection.
    """
    try:
        from google.api_core.exceptions import (
            DeadlineExceeded,
            InternalServerError,
            InvalidArgument,
            PermissionDenied,
            ResourceExhausted,
            ServiceUnavailable,
            Unauthenticated,
        )
    except ImportError:
        return RuntimeAdapterError(str(exc))

    if isinstance(exc, (PermissionDenied, Unauthenticated)):
        return RuntimeAuthError(str(exc))
    if isinstance(exc, ResourceExhausted):
        return RuntimeRateLimitError(str(exc))
    if isinstance(exc, DeadlineExceeded):
        return RuntimeTimeoutError(str(exc))
    if isinstance(exc, (InternalServerError, ServiceUnavailable)):
        return RuntimeServerError(str(exc))
    if isinstance(exc, InvalidArgument) and "context" in str(exc).lower():
        return RuntimeContextLengthError(str(exc))
    return RuntimeAdapterError(str(exc))


class VertexRuntimeAdapter(BaseRuntimeAdapter):
    name = "vertex"

    def __init__(self, config: RuntimeConfig) -> None:
        self._config = config

    async def generate(self, req: Request) -> RawResponse:
        if not _real_runtimes_enabled():
            raise RealRuntimeDisabledError(_GUIDE)
        try:
            import vertexai  # type: ignore[import-untyped,unused-ignore]
            from vertexai.generative_models import (
                GenerativeModel,  # type: ignore[import-untyped,unused-ignore]
            )
        except ImportError as e:
            raise ImportError(
                "Vertex adapter requires the [gcp] extra: pip install eap-core[gcp]"
            ) from e
        vertexai.init(
            project=self._config.options.get("project"),
            location=self._config.options.get("location", "us-central1"),
        )
        model = GenerativeModel(self._config.model)
        prompt = "\n".join(m.content if isinstance(m.content, str) else "" for m in req.messages)
        try:
            resp = model.generate_content(prompt)
        except Exception as exc:
            raise _map_google_error(exc) from exc
        return RawResponse(
            text=resp.text,
            usage={
                "input_tokens": getattr(resp.usage_metadata, "prompt_token_count", 0),
                "output_tokens": getattr(resp.usage_metadata, "candidates_token_count", 0),
            },
            raw={"resp": str(resp)},
        )

    async def stream(self, req: Request) -> AsyncIterator[RawChunk]:
        if not _real_runtimes_enabled():
            raise RealRuntimeDisabledError(_GUIDE)
        raise NotImplementedError("Vertex streaming not implemented in walking skeleton.")

    async def list_models(self) -> list[ModelInfo]:
        if not _real_runtimes_enabled():
            return [ModelInfo(name=self._config.model, provider="vertex")]
        return [ModelInfo(name=self._config.model, provider="vertex")]
