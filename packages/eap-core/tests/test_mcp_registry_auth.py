"""Tests for `McpToolRegistry.invoke` auth-required enforcement (C5).

The dispatcher must refuse to invoke any tool whose `ToolSpec.requires_auth`
is True unless the caller plumbs an `identity` through. Prior to this fix
the flag was decorative and `--auth-required` tools ran unauthenticated.
"""

from __future__ import annotations

import pytest

from eap_core.exceptions import IdentityError
from eap_core.mcp import McpToolRegistry, ToolSpec


async def _noop(**_: object) -> dict[str, object]:
    return {"ok": True}


def _spec(*, requires_auth: bool) -> ToolSpec:
    return ToolSpec(
        name="transfer_funds",
        description="t",
        input_schema={"type": "object"},
        output_schema=None,
        fn=_noop,
        requires_auth=requires_auth,
        is_async=True,
    )


async def test_invoke_refuses_when_auth_required_and_no_identity() -> None:
    reg = McpToolRegistry()
    reg.register(_spec(requires_auth=True))
    with pytest.raises(IdentityError, match="requires_auth"):
        await reg.invoke("transfer_funds", {})


async def test_invoke_refuses_when_auth_required_and_explicit_none_identity() -> None:
    reg = McpToolRegistry()
    reg.register(_spec(requires_auth=True))
    with pytest.raises(IdentityError, match="requires_auth"):
        await reg.invoke("transfer_funds", {}, identity=None)


async def test_invoke_allows_when_auth_required_and_identity_present() -> None:
    reg = McpToolRegistry()
    reg.register(_spec(requires_auth=True))
    fake_identity = object()
    result = await reg.invoke("transfer_funds", {}, identity=fake_identity)
    assert result == {"ok": True}


async def test_invoke_allows_when_auth_not_required_and_no_identity() -> None:
    """Tools that opt out of auth must still run without an identity."""
    reg = McpToolRegistry()
    reg.register(_spec(requires_auth=False))
    result = await reg.invoke("transfer_funds", {})
    assert result == {"ok": True}
