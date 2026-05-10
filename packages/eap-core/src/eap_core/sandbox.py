"""Sandbox abstractions for agentic AI applications.

EAP-Core treats sandboxed code execution and browser automation as
two of the highest-risk agentic capabilities. They must flow through
the same middleware chain as any other tool — sanitize, PII mask,
policy check, observability — before reaching the sandbox.

This module defines the vendor-neutral ``CodeSandbox`` and
``BrowserSandbox`` Protocols. Cloud-managed implementations live in
``eap_core.integrations.{agentcore, vertex}``. In-process defaults
here are useful for tests and local development.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass
class SandboxResult:
    """Result of executing code in a sandbox.

    Fields mirror common patterns across AgentCore Code Interpreter,
    Vertex Agent Sandbox, and local subprocess execution.
    """

    stdout: str = ""
    stderr: str = ""
    exit_code: int = 0
    artifacts: dict[str, str] = field(default_factory=dict)
    """Map of artifact name → URI (e.g., S3/GCS path) when the sandbox
    produces files. Empty when no files were written or kept."""


@runtime_checkable
class CodeSandbox(Protocol):
    """Sandboxed code execution.

    Implementations include:
    - ``InProcessCodeSandbox`` (here) — runs in a Python subprocess for
      tests; not actually sandboxed.
    - ``eap_core.integrations.agentcore.AgentCoreCodeSandbox`` —
      AgentCore Code Interpreter (real sandbox, requires AWS).
    - ``eap_core.integrations.vertex.VertexCodeSandbox`` —
      Vertex Agent Sandbox (real sandbox, requires GCP).
    """

    name: str

    async def execute(self, language: str, code: str) -> SandboxResult: ...


@runtime_checkable
class BrowserSandbox(Protocol):
    """Cloud-based browser for web automation.

    Implementations include:
    - ``NoopBrowserSandbox`` (here) — for tests; raises on real method calls.
    - ``eap_core.integrations.agentcore.AgentCoreBrowserSandbox`` —
      AgentCore Browser tool.
    - ``eap_core.integrations.vertex.VertexBrowserSandbox`` —
      Vertex Agent Sandbox (browser path).
    """

    name: str

    async def navigate(self, url: str) -> dict[str, Any]: ...
    async def click(self, selector: str) -> dict[str, Any]: ...
    async def fill(self, selector: str, value: str) -> dict[str, Any]: ...
    async def extract_text(self, selector: str = "body") -> str: ...
    async def screenshot(self) -> bytes: ...


class InProcessCodeSandbox:
    """Python-subprocess code execution for tests / local development.

    NOT actually sandboxed — runs in a subprocess. Only ``language="python"``
    is supported. Useful for unit tests of agents that call
    ``client.invoke_tool("execute_code", ...)``; do NOT use in production.

    Production code paths should use ``AgentCoreCodeSandbox`` or
    ``VertexCodeSandbox`` (both raise ``NotImplementedError`` without
    ``EAP_ENABLE_REAL_RUNTIMES=1``, which keeps misconfigurations loud).
    """

    name: str = "in_process_code_sandbox"

    async def execute(self, language: str, code: str) -> SandboxResult:
        if language != "python":
            return SandboxResult(
                stderr=f"InProcessCodeSandbox only supports python, got {language!r}",
                exit_code=2,
            )
        import asyncio
        import sys

        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            code,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await proc.communicate()
        return SandboxResult(
            stdout=out.decode("utf-8", errors="replace"),
            stderr=err.decode("utf-8", errors="replace"),
            exit_code=proc.returncode or 0,
        )


class NoopBrowserSandbox:
    """A browser sandbox that never makes network calls.

    Every method returns a stub result. Use in tests where you don't
    want to drag in Playwright or a real cloud browser. Production
    code paths must use a real cloud-backed implementation.
    """

    name: str = "noop_browser"

    async def navigate(self, url: str) -> dict[str, Any]:
        return {"url": url, "status": "noop"}

    async def click(self, selector: str) -> dict[str, Any]:
        return {"selector": selector, "status": "noop"}

    async def fill(self, selector: str, value: str) -> dict[str, Any]:
        return {"selector": selector, "value": value, "status": "noop"}

    async def extract_text(self, selector: str = "body") -> str:
        return ""

    async def screenshot(self) -> bytes:
        return b""


__all__ = [
    "BrowserSandbox",
    "CodeSandbox",
    "InProcessCodeSandbox",
    "NoopBrowserSandbox",
    "SandboxResult",
]
