"""LocalRuntimeAdapter — deterministic in-memory runtime."""
from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel

from eap_core.config import RuntimeConfig
from eap_core.runtimes.base import BaseRuntimeAdapter, ModelInfo, RawChunk, RawResponse
from eap_core.types import Message, Request


def _flatten_prompt(messages: list[Message]) -> str:
    parts: list[str] = []
    for m in messages:
        if isinstance(m.content, str):
            parts.append(m.content)
        else:
            parts.extend(p.get("text", "") for p in m.content if isinstance(p, dict))
    return "\n".join(parts)


def _load_responses() -> list[dict[str, Any]]:
    candidates = [
        Path.cwd() / "responses.yaml",
        Path.home() / ".eap" / "local_responses.yaml",
    ]
    for c in candidates:
        if c.is_file():
            data = yaml.safe_load(c.read_text()) or {}
            return data.get("responses", [])
    return []


def _synthesize_default(schema: type[BaseModel]) -> dict[str, Any]:
    """Build a minimum valid instance using model field defaults / type defaults."""
    from pydantic_core import PydanticUndefinedType

    out: dict[str, Any] = {}
    for name, field in schema.model_fields.items():
        if (
            field.default is not None
            and not callable(field.default)
            and not isinstance(field.default, PydanticUndefinedType)
        ):
            out[name] = field.default
            continue
        if field.default_factory is not None:
            try:
                out[name] = field.default_factory()
                continue
            except Exception:  # noqa: BLE001
                pass
        ann = field.annotation
        if ann is str:
            out[name] = ""
        elif ann is int:
            out[name] = 0
        elif ann is float:
            out[name] = 0.0
        elif ann is bool:
            out[name] = False
        elif ann is list or getattr(ann, "__origin__", None) is list:
            out[name] = []
        elif ann is dict or getattr(ann, "__origin__", None) is dict:
            out[name] = {}
        else:
            out[name] = None
    return out


class LocalRuntimeAdapter(BaseRuntimeAdapter):
    name = "local"

    def __init__(self, config: RuntimeConfig) -> None:
        self._config = config

    async def generate(self, req: Request) -> RawResponse:
        prompt = _flatten_prompt(req.messages)

        schema = req.metadata.get("output_schema")
        if isinstance(schema, type) and issubclass(schema, BaseModel):
            obj = _synthesize_default(schema)
            return RawResponse(
                text=json.dumps(obj),
                usage={"input_tokens": len(prompt.split()), "output_tokens": len(json.dumps(obj).split())},
                finish_reason="stop",
            )

        for entry in _load_responses():
            if entry.get("match") and entry["match"] in prompt:
                text = entry["text"]
                return RawResponse(
                    text=text,
                    usage={"input_tokens": len(prompt.split()), "output_tokens": len(text.split())},
                    finish_reason="stop",
                )

        text = f"[local-runtime] received {len(prompt.split())} tokens, model={req.model}"
        return RawResponse(
            text=text,
            usage={"input_tokens": len(prompt.split()), "output_tokens": len(text.split())},
            finish_reason="stop",
        )

    async def stream(self, req: Request) -> AsyncIterator[RawChunk]:
        full = (await self.generate(req)).text
        for i, word in enumerate(full.split(" ")):
            await asyncio.sleep(0)
            yield RawChunk(index=i, text=word + " ", finish_reason=None)

    async def list_models(self) -> list[ModelInfo]:
        return [ModelInfo(name=self._config.model or "echo-1", provider="local")]
