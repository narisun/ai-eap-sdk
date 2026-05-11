"""transfer_funds — auth-required, idempotent write.

`requires_auth=True` engages the NHI flow: the policy middleware
checks for an audience-scoped token on every call, and the dispatcher
acquires one via the configured identity before the function fires.
"""

from __future__ import annotations

from eap_core.mcp import default_registry, mcp_tool

_LEDGER: dict[str, dict] = {}


@mcp_tool(description="Transfer funds between bank accounts.", requires_auth=True)
async def transfer_funds(
    from_id: str,
    to_id: str,
    amount_cents: int,
    idempotency_key: str,
) -> dict:
    if idempotency_key in _LEDGER:
        return _LEDGER[idempotency_key]
    result = {
        "status": "ok",
        "from_id": from_id,
        "to_id": to_id,
        "amount_cents": amount_cents,
        "idempotency_key": idempotency_key,
    }
    _LEDGER[idempotency_key] = result
    return result


default_registry().register(transfer_funds.spec)
