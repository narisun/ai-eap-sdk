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

import asyncio
import sys
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

    NOT actually sandboxed — runs in a Python subprocess that inherits
    the parent's environment, filesystem, and network. Both
    ``timeout_seconds`` and ``max_code_bytes`` are required to make the
    failure mode explicit; production paths must use
    ``AgentCoreCodeSandbox`` or ``VertexCodeSandbox``.
    """

    name: str = "in_process_code_sandbox"

    def __init__(self, *, timeout_seconds: float, max_code_bytes: int) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be > 0")
        if max_code_bytes <= 0:
            raise ValueError("max_code_bytes must be > 0")
        self._timeout = timeout_seconds
        self._max_bytes = max_code_bytes

    async def execute(self, language: str, code: str) -> SandboxResult:
        if len(code.encode("utf-8")) > self._max_bytes:
            return SandboxResult(
                stderr=f"input exceeds max_code_bytes={self._max_bytes}",
                exit_code=2,
            )
        if language != "python":
            return SandboxResult(
                stderr=f"InProcessCodeSandbox only supports python, got {language!r}",
                exit_code=2,
            )
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable,
                "-c",
                code,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as e:
            return SandboxResult(
                stderr=f"subprocess spawn failed: {e}",
                exit_code=2,
            )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=self._timeout)
            return SandboxResult(
                stdout=out.decode("utf-8", errors="replace"),
                stderr=err.decode("utf-8", errors="replace"),
                exit_code=proc.returncode or 0,
            )
        except TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass  # process exited between timeout fire and kill — benign race
            try:
                await proc.communicate()
            except Exception:  # noqa: S110 - best-effort drain of killed process
                pass
            return SandboxResult(
                stderr=f"execution killed: timeout after {self._timeout}s",
                exit_code=124,
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
