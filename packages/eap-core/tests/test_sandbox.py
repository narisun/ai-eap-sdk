"""Tests for ``InProcessCodeSandbox`` mandatory limits (C7).

``InProcessCodeSandbox`` is "not actually sandboxed" — runs the model's
code in a Python subprocess that inherits the parent's environment.
The least we can do is bound runtime and input size; both must be
explicit so misuse is loud, not silent.
"""

from __future__ import annotations

import asyncio

import pytest

from eap_core.sandbox import InProcessCodeSandbox, SandboxResult


@pytest.mark.asyncio
async def test_in_process_sandbox_timeout_kills_runaway():
    sb = InProcessCodeSandbox(timeout_seconds=1, max_code_bytes=10_000)
    result = await sb.execute("python", "import time; time.sleep(30); print('never')")
    assert result.exit_code != 0
    assert "timeout" in result.stderr.lower() or "killed" in result.stderr.lower()


@pytest.mark.asyncio
async def test_in_process_sandbox_rejects_oversized_input():
    sb = InProcessCodeSandbox(timeout_seconds=5, max_code_bytes=100)
    huge = "x = 1\n" * 1000
    result = await sb.execute("python", huge)
    assert result.exit_code == 2
    assert "max_code_bytes" in result.stderr


def test_in_process_sandbox_construction_requires_explicit_limits():
    with pytest.raises(TypeError):
        InProcessCodeSandbox()  # type: ignore[call-arg]


def test_in_process_sandbox_construction_rejects_nonpositive_values():
    with pytest.raises(ValueError):
        InProcessCodeSandbox(timeout_seconds=0, max_code_bytes=1000)
    with pytest.raises(ValueError):
        InProcessCodeSandbox(timeout_seconds=5, max_code_bytes=0)


@pytest.mark.asyncio
async def test_in_process_sandbox_timeout_handles_race_with_natural_exit():
    """Process that exits naturally right around the timeout boundary
    should still return a clean SandboxResult, not raise.

    If the child exits in the microseconds between ``asyncio.wait_for``
    deadline-fire and ``proc.kill()`` being called, ``proc.kill()`` raises
    ``ProcessLookupError``. The sandbox must swallow that — its contract
    is to return a ``SandboxResult``, not raise.
    """
    sb = InProcessCodeSandbox(timeout_seconds=0.1, max_code_bytes=10_000)
    # Sleep slightly shorter than timeout so the race window is narrow but real.
    result = await sb.execute("python", "import time; time.sleep(0.05)")
    assert isinstance(result, SandboxResult)


@pytest.mark.asyncio
async def test_in_process_sandbox_spawn_failure_returns_result(monkeypatch):
    """A subprocess spawn error returns a SandboxResult, doesn't raise."""

    async def _boom(*a, **kw):
        raise OSError("test: no fork for you")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _boom)
    sb = InProcessCodeSandbox(timeout_seconds=5, max_code_bytes=10_000)
    result = await sb.execute("python", "print('hi')")
    assert result.exit_code == 2
    assert "spawn failed" in result.stderr
