"""A2A AgentCard model and builder."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from eap_core.mcp.registry import McpToolRegistry


class Skill(BaseModel):
    name: str
    description: str
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] | None = None
    requires_auth: bool = False


class AgentCard(BaseModel):
    name: str
    description: str
    version: str = "0.1.0"
    skills: list[Skill] = Field(default_factory=list)
    endpoints: dict[str, str] = Field(default_factory=dict)
    authentication: dict[str, Any] | None = None


def build_card(
    *,
    name: str,
    description: str,
    skills_from: McpToolRegistry,
    auth: str | None = None,
    endpoints: dict[str, str] | None = None,
    version: str = "0.1.0",
) -> AgentCard:
    skills = [
        Skill(
            name=spec.name,
            description=spec.description,
            input_schema=spec.input_schema,
            output_schema=spec.output_schema,
            requires_auth=spec.requires_auth,
        )
        for spec in skills_from.list_tools()
    ]
    return AgentCard(
        name=name,
        description=description,
        version=version,
        skills=skills,
        endpoints=endpoints or {},
        authentication={"type": auth} if auth else None,
    )
