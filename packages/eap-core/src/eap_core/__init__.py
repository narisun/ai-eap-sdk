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

# Discovery abstraction (agent / tool / MCP-server registry)
from eap_core.discovery import AgentRegistry, InMemoryAgentRegistry

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
from eap_core.mcp import MCPError, McpToolRegistry, ToolSpec, mcp_tool

# Memory abstraction
from eap_core.memory import InMemoryStore, MemoryStore

# Payments abstraction (x402, AP2, etc.)
from eap_core.payments import (
    InMemoryPaymentBackend,
    PaymentBackend,
    PaymentRequired,
)

# Sandbox abstraction (code + browser execution)
from eap_core.sandbox import (
    BrowserSandbox,
    CodeSandbox,
    InProcessCodeSandbox,
    NoopBrowserSandbox,
    SandboxResult,
)

# Security abstraction (threat detection)
from eap_core.security import (
    RegexThreatDetector,
    ThreatAssessment,
    ThreatDetector,
)

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
    # memory
    "InMemoryStore",
    "MemoryStore",
    # sandbox
    "BrowserSandbox",
    "CodeSandbox",
    "InProcessCodeSandbox",
    "NoopBrowserSandbox",
    "SandboxResult",
    # discovery
    "AgentRegistry",
    "InMemoryAgentRegistry",
    # payments
    "InMemoryPaymentBackend",
    "PaymentBackend",
    "PaymentRequired",
    # security
    "RegexThreatDetector",
    "ThreatAssessment",
    "ThreatDetector",
]
