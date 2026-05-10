"""GCP Vertex AI adapter (shape-correct stub)."""
from __future__ import annotations

import os
from collections.abc import AsyncIterator

from eap_core.config import RuntimeConfig
from eap_core.runtimes.base import BaseRuntimeAdapter, ModelInfo, RawChunk, RawResponse
from eap_core.types import Request

_GUIDE = (
    "Wire credentials and replace this stub. See docs/runtimes/vertex.md. "
    "Set EAP_ENABLE_REAL_RUNTIMES=1 to perform real calls (requires the [gcp] extra)."
)


def _real_runtimes_enabled() -> bool:
    return os.environ.get("EAP_ENABLE_REAL_RUNTIMES") == "1"


class VertexRuntimeAdapter(BaseRuntimeAdapter):
    name = "vertex"

    def __init__(self, config: RuntimeConfig) -> None:
        self._config = config

    async def generate(self, req: Request) -> RawResponse:
        if not _real_runtimes_enabled():
            raise NotImplementedError(_GUIDE)
        try:
            import vertexai  # type: ignore[import-not-found]
            from vertexai.generative_models import GenerativeModel  # type: ignore[import-not-found]
        except ImportError as e:
            raise ImportError("Vertex adapter requires the [gcp] extra: pip install eap-core[gcp]") from e
        vertexai.init(
            project=self._config.options.get("project"),
            location=self._config.options.get("location", "us-central1"),
        )
        model = GenerativeModel(self._config.model)
        prompt = "\n".join(m.content if isinstance(m.content, str) else "" for m in req.messages)
        resp = model.generate_content(prompt)
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
            raise NotImplementedError(_GUIDE)
        raise NotImplementedError("Vertex streaming not implemented in walking skeleton.")

    async def list_models(self) -> list[ModelInfo]:
        if not _real_runtimes_enabled():
            return [ModelInfo(name=self._config.model, provider="vertex")]
        return [ModelInfo(name=self._config.model, provider="vertex")]
