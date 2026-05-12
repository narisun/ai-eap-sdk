"""Regression tests for Luhn validation on credit-card PII (P1-6)."""

from __future__ import annotations

from eap_core.middleware.pii import PiiMaskingMiddleware, _passes_luhn
from eap_core.types import Context, Message, Request

# Real Luhn-valid card numbers (Visa, Mastercard, Amex test PANs).
VALID_VISA = "4111111111111111"
VALID_MASTERCARD = "5555555555554444"
VALID_AMEX = "378282246310005"  # 15 digits

# Invalid sequences that match the 16-digit regex but fail Luhn.
INVALID_16 = "1234567890123456"  # fails Luhn
INVALID_FORMATTED = "1234 5678 9012 3456"  # fails Luhn after digit-extraction


async def test_valid_visa_is_masked() -> None:
    mw = PiiMaskingMiddleware()
    ctx = Context()
    req = Request(
        model="m",
        messages=[Message(role="user", content=f"my card is {VALID_VISA}")],
    )
    out = await mw.on_request(req, ctx)
    masked_text = out.messages[0].content
    assert VALID_VISA not in masked_text
    # Token has the form <CREDIT_CARD_xxxx> or <AMEX_xxxx> (15-digit Amex
    # is checked first by regex ordering); accept either label here.
    assert "<CREDIT_CARD_" in masked_text or "<AMEX_" in masked_text


async def test_valid_mastercard_is_masked() -> None:
    mw = PiiMaskingMiddleware()
    ctx = Context()
    req = Request(
        model="m",
        messages=[Message(role="user", content=f"card: {VALID_MASTERCARD}")],
    )
    out = await mw.on_request(req, ctx)
    masked_text = out.messages[0].content
    assert VALID_MASTERCARD not in masked_text
    assert "<CREDIT_CARD_" in masked_text


async def test_valid_amex_is_masked() -> None:
    mw = PiiMaskingMiddleware()
    ctx = Context()
    req = Request(
        model="m",
        messages=[Message(role="user", content=f"charge {VALID_AMEX} please")],
    )
    out = await mw.on_request(req, ctx)
    masked_text = out.messages[0].content
    assert VALID_AMEX not in masked_text
    assert "<AMEX_" in masked_text


async def test_invalid_16_digit_sequence_is_not_masked() -> None:
    """16 contiguous digits that fail Luhn must be left in place."""
    mw = PiiMaskingMiddleware()
    ctx = Context()
    req = Request(
        model="m",
        messages=[Message(role="user", content=f"order number {INVALID_16}")],
    )
    out = await mw.on_request(req, ctx)
    masked_text = out.messages[0].content
    # Luhn fails — must NOT be masked.
    assert INVALID_16 in masked_text
    assert "<CREDIT_CARD_" not in masked_text


async def test_invalid_formatted_16_digit_sequence_is_not_masked() -> None:
    """Space-separated 16-digit sequence failing Luhn is not masked either."""
    mw = PiiMaskingMiddleware()
    ctx = Context()
    req = Request(
        model="m",
        messages=[Message(role="user", content=f"id is {INVALID_FORMATTED}")],
    )
    out = await mw.on_request(req, ctx)
    masked_text = out.messages[0].content
    assert INVALID_FORMATTED in masked_text
    assert "<CREDIT_CARD_" not in masked_text


def test_luhn_valid_cards_pass() -> None:
    assert _passes_luhn(VALID_VISA) is True
    assert _passes_luhn(VALID_MASTERCARD) is True
    assert _passes_luhn(VALID_AMEX) is True
    # Separators must be ignored.
    assert _passes_luhn("4111-1111-1111-1111") is True
    assert _passes_luhn("4111 1111 1111 1111") is True


def test_luhn_invalid_cards_fail() -> None:
    assert _passes_luhn(INVALID_16) is False
    assert _passes_luhn("1234567890") is False  # wrong length
    assert _passes_luhn("4111111111111110") is False  # off-by-one tail
    assert _passes_luhn("") is False
