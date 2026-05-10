"""EAP-Core SDK — public API.

The most common imports are re-exported at the package root so users
can write::

    from eap_core import EnterpriseLLM, RuntimeConfig, mcp_tool

For deeper APIs (custom middleware, runtime adapters, identity, eval
internals), import from the submodule directly::

    from eap_core.middleware import Middleware, MiddlewarePipeline
    from eap_core.runtimes import BaseRuntimeAdapter, AdapterRegistry
"""

from eap_core._version import __version__

# A2A AgentCard (for agent discoverability)
from eap_core.a2a import AgentCard, Skill, build_card

# Core client + config
from eap_core.client import EnterpriseLLM
from eap_core.config import EvalConfig, IdentityConfig, RuntimeConfig

# Eval framework
from eap_core.eval import (
    DeterministicJudge,
    EvalCase,
    EvalReport,
    EvalRunner,
    FaithfulnessScorer,
    Trajectory,
    TrajectoryRecorder,
)

# Exceptions (most-used)
from eap_core.exceptions import (
    EapError,
    IdentityError,
    OutputValidationError,
    PolicyDeniedError,
    PromptInjectionError,
    RuntimeAdapterError,
)

# MCP tool authoring + registry
from eap_core.mcp import MCPError, McpToolRegistry, ToolSpec, default_registry, mcp_tool

# Public data types
from eap_core.types import Chunk, Context, Message, Request, Response

__all__ = [  # noqa: RUF022 — grouped semantically, not alphabetically
    # version
    "__version__",
    # client + config
    "EnterpriseLLM",
    "RuntimeConfig",
    "IdentityConfig",
    "EvalConfig",
    # types
    "Chunk",
    "Context",
    "Message",
    "Request",
    "Response",
    # exceptions
    "EapError",
    "IdentityError",
    "OutputValidationError",
    "PolicyDeniedError",
    "PromptInjectionError",
    "RuntimeAdapterError",
    # MCP
    "McpToolRegistry",
    "MCPError",
    "ToolSpec",
    "default_registry",
    "mcp_tool",
    # A2A
    "AgentCard",
    "Skill",
    "build_card",
    # eval
    "DeterministicJudge",
    "EvalCase",
    "EvalReport",
    "EvalRunner",
    "FaithfulnessScorer",
    "Trajectory",
    "TrajectoryRecorder",
]
