"""Smoke-tests for the shipped example projects.

After every change to the security gates (e.g. requires_auth,
allowed_audiences), these tests catch silent drift where a fix
lands in one example but not the others.

Examples are discovered dynamically — to opt OUT, add the directory
name to `_SKIP`. New examples get coverage automatically.
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_EXAMPLES = _REPO_ROOT / "examples"

# Bank-agent-shaped examples that expose `build_client()`. Excluded
# because their agent surface is structurally different (different
# protocols, no LLM client, etc.).
_SKIP = {"mcp-server-example"}


def _discover_examples() -> list[str]:
    if not _EXAMPLES.is_dir():
        return []
    return sorted(
        p.name for p in _EXAMPLES.iterdir() if (p / "agent.py").is_file() and p.name not in _SKIP
    )


def _purge_sibling_modules() -> None:
    """Drop bare-name sibling packages that examples ship (tools/,
    cloud_wiring) so successive parametrize cases don't import each
    other's modules."""
    for name in list(sys.modules):
        head = name.split(".")[0]
        if head in {"tools", "cloud_wiring"} or name.startswith("_example_"):
            del sys.modules[name]


def _import_example_agent(name: str):
    """Import the `agent` module from examples/<name>/agent.py.

    Sibling packages (tools/, cloud_wiring) are purged from sys.modules
    before and after each import so the cases stay order-independent.
    """
    agent_path = _EXAMPLES / name / "agent.py"
    if not agent_path.is_file():
        pytest.skip(f"example {name} not present")
    _purge_sibling_modules()
    sys.path.insert(0, str(_EXAMPLES / name))
    try:
        spec = importlib.util.spec_from_file_location(
            f"_example_{name.replace('-', '_')}", agent_path
        )
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.pop(0)
        _purge_sibling_modules()


@pytest.mark.parametrize("example_name", _discover_examples())
def test_example_build_client_does_not_crash(example_name: str):
    """Every bank-agent-shaped example must construct EnterpriseLLM
    without IdentityError.

    This is the C5 enforcement contract lock: a tool registered as
    `requires_auth=True` will refuse to dispatch unless the
    EnterpriseLLM is built with an identity. Examples that ship
    `requires_auth=True` tools must wire identity in `build_client()`.
    """
    module = _import_example_agent(example_name)
    build = getattr(module, "build_client", None)
    assert build is not None, (
        f"example {example_name} has no build_client() — "
        "the smoke contract is that every bank-agent-shaped example exposes build_client()"
    )
    # Construct without crash. If this raises, the example is broken
    # under the C5 gate (or any future gate the SDK adds).
    client = build()
    assert client is not None


@pytest.mark.parametrize("example_name", _discover_examples())
def test_example_run_path_exercises_dispatcher(example_name: str):
    """If the example exposes a runner (`run()` or `execute_transfer(...)`),
    exercise it. This ensures the dispatcher actually fires under the
    example's typical workload — not just at construction time."""
    module = _import_example_agent(example_name)
    run = getattr(module, "run", None)
    if run is not None:
        asyncio.run(run())
        return
    execute_transfer = getattr(module, "execute_transfer", None)
    if execute_transfer is not None:
        result = asyncio.run(execute_transfer("acct-1", "acct-2", 1000))
        assert isinstance(result, dict)
        assert result.get("status") in {"ok", "rejected", "payment_required"}
        return
    pytest.skip(
        f"example {example_name} has no run() or execute_transfer() — build_client smoke is enough"
    )
