"""lookup_account — read-only account lookup.

Local in-memory accounts for the demo. In a real deployment this
tool would call your bank's account-service via the Gateway.
"""

from __future__ import annotations

from eap_core.mcp import mcp_tool

_ACCOUNTS: dict[str, dict] = {
    "acct-1": {"id": "acct-1", "balance_cents": 50000, "owner": "alice"},
    "acct-2": {"id": "acct-2", "balance_cents": 25000, "owner": "bob"},
}


@mcp_tool(description="Look up a bank account by id.")
async def lookup_account(account_id: str) -> dict:
    return _ACCOUNTS.get(
        account_id,
        {"id": account_id, "balance_cents": 0, "owner": None},
    )
