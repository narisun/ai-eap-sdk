"""Vendor-neutral payment backend abstractions for agentic AI.

Agentic AI is converging on standardized microtransaction protocols
(AWS's x402, Google's AP2, others). EAP-Core abstracts behind the
``PaymentBackend`` Protocol so the same agent code works with either
backend by config change.

Two pieces:

- ``PaymentRequired`` — exception raised by tool wrappers when an
  upstream service responds with HTTP 402. Carries enough metadata
  for a backend to sign and retry.
- ``PaymentBackend`` Protocol — `start_session`, `authorize`,
  budget bookkeeping. Cloud implementations live under
  ``eap_core.integrations.{agentcore, vertex}``.

In-process default: ``InMemoryPaymentBackend`` (no real signing,
just budget bookkeeping — for tests).
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


class PaymentRequired(Exception):  # noqa: N818 — matches HTTP 402 "Payment Required"
    """Raised by a tool when an upstream service responds with HTTP 402.

    Carries the payment-required metadata so a ``PaymentBackend`` can
    sign and retry the original call.
    """

    def __init__(
        self,
        *,
        amount_cents: int,
        currency: str,
        merchant: str,
        original_url: str,
        raw: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(f"payment required: {amount_cents} {currency} to {merchant}")
        self.amount_cents = amount_cents
        self.currency = currency
        self.merchant = merchant
        self.original_url = original_url
        self.raw = raw or {}


@runtime_checkable
class PaymentBackend(Protocol):
    """Vendor-neutral payment Protocol.

    Implementations include:
    - ``InMemoryPaymentBackend`` (here) — for tests.
    - ``eap_core.integrations.agentcore.PaymentClient`` —
      AgentCore Payments via x402.
    - ``eap_core.integrations.vertex.AP2PaymentClient`` —
      Google Agent Payment Protocol (AP2).
    """

    name: str

    @property
    def remaining_cents(self) -> int: ...

    @property
    def spent_cents(self) -> int: ...

    def can_afford(self, amount_cents: int) -> bool: ...

    async def start_session(self) -> str: ...

    async def authorize(self, req: PaymentRequired) -> dict[str, Any]:
        """Sign the payment and return a receipt.

        Caller uses the receipt to retry the original call (typically
        by re-issuing the HTTP request with the receipt in an
        ``X-Payment-Receipt`` header per x402 / AP2 convention).
        """
        ...


class InMemoryPaymentBackend:
    """In-process payment backend for tests.

    Tracks budget bookkeeping (``remaining_cents``, ``spent_cents``,
    ``can_afford``) but never actually signs payments. ``authorize``
    deducts from the budget and returns a fake receipt. Use only in
    tests.
    """

    name: str = "in_memory_payment"

    def __init__(
        self,
        *,
        max_spend_cents: int = 100,
        session_id: str = "in-memory-session",
    ) -> None:
        self._max = max_spend_cents
        self._spent = 0
        self._session_id = session_id

    @property
    def remaining_cents(self) -> int:
        return max(self._max - self._spent, 0)

    @property
    def spent_cents(self) -> int:
        return self._spent

    def can_afford(self, amount_cents: int) -> bool:
        return amount_cents <= self.remaining_cents

    async def start_session(self) -> str:
        return self._session_id

    async def authorize(self, req: PaymentRequired) -> dict[str, Any]:
        if not self.can_afford(req.amount_cents):
            raise RuntimeError(
                f"InMemoryPaymentBackend out of budget: "
                f"need {req.amount_cents}, have {self.remaining_cents}"
            )
        self._spent += req.amount_cents
        return {
            "receipt": f"fake-receipt-{self._session_id}",
            "amount_cents": req.amount_cents,
            "currency": req.currency,
            "merchant": req.merchant,
        }


__all__ = [
    "InMemoryPaymentBackend",
    "PaymentBackend",
    "PaymentRequired",
]
