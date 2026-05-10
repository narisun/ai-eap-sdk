"""AWS Bedrock AgentCore adapter (shape-correct stub).

Real network calls execute only when ``EAP_ENABLE_REAL_RUNTIMES=1``.
``boto3`` is lazy-imported inside the call paths so absence of the
``[aws]`` extra does not break import.
"""
from __future__ import annotations

import os
from collections.abc import AsyncIterator

from eap_core.config import RuntimeConfig
from eap_core.runtimes.base import BaseRuntimeAdapter, ModelInfo, RawChunk, RawResponse
from eap_core.types import Request

_GUIDE = (
    "Wire credentials and replace this stub. See docs/runtimes/bedrock.md. "
    "Set EAP_ENABLE_REAL_RUNTIMES=1 to perform real calls (requires the [aws] extra)."
)


def _real_runtimes_enabled() -> bool:
    return os.environ.get("EAP_ENABLE_REAL_RUNTIMES") == "1"


class BedrockRuntimeAdapter(BaseRuntimeAdapter):
    name = "bedrock"

    def __init__(self, config: RuntimeConfig) -> None:
        self._config = config

    async def generate(self, req: Request) -> RawResponse:
        if not _real_runtimes_enabled():
            raise NotImplementedError(_GUIDE)
        try:
            import boto3  # type: ignore[import-not-found]
        except ImportError as e:
            raise ImportError("Bedrock adapter requires the [aws] extra: pip install eap-core[aws]") from e
        client = boto3.client("bedrock-runtime", region_name=self._config.options.get("region"))
        resp = client.converse(
            modelId=self._config.model,
            messages=[{"role": m.role, "content": [{"text": m.content if isinstance(m.content, str) else ""}]} for m in req.messages],
        )
        text = resp["output"]["message"]["content"][0]["text"]
        usage = resp.get("usage", {})
        return RawResponse(
            text=text,
            usage={
                "input_tokens": usage.get("inputTokens", 0),
                "output_tokens": usage.get("outputTokens", 0),
            },
            finish_reason=resp.get("stopReason"),
            raw=resp,
        )

    async def stream(self, req: Request) -> AsyncIterator[RawChunk]:
        if not _real_runtimes_enabled():
            raise NotImplementedError(_GUIDE)
        raise NotImplementedError("Bedrock streaming not implemented in walking skeleton.")

    async def list_models(self) -> list[ModelInfo]:
        if not _real_runtimes_enabled():
            return [ModelInfo(name=self._config.model, provider="bedrock")]
        try:
            import boto3  # type: ignore[import-not-found]
        except ImportError as e:
            raise ImportError("Bedrock adapter requires the [aws] extra") from e
        client = boto3.client("bedrock", region_name=self._config.options.get("region"))
        models = client.list_foundation_models().get("modelSummaries", [])
        return [ModelInfo(name=m["modelId"], provider="bedrock") for m in models]
