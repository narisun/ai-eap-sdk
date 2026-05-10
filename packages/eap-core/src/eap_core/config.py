"""Configuration models for EAP-Core."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, field_validator


class RuntimeConfig(BaseModel):
    provider: str
    model: str
    options: dict[str, Any] = Field(default_factory=dict)

    @field_validator("model")
    @classmethod
    def _model_nonempty(cls, v: str) -> str:
        if not v:
            raise ValueError("model must be non-empty")
        return v


class IdentityConfig(BaseModel):
    client_id: str = "local-agent"
    idp_url: str | None = None
    private_key_pem: str | None = None
    default_audience: str | None = None
    token_ttl_seconds: int = 300


class EvalConfig(BaseModel):
    judge_runtime: RuntimeConfig = Field(
        default_factory=lambda: RuntimeConfig(provider="local", model="judge-stub")
    )
    threshold: float = 0.7
    scorers: list[str] = Field(default_factory=lambda: ["faithfulness"])
