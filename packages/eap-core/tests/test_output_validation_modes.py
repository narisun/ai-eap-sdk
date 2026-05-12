"""Regression tests for OutputValidationMiddleware modes (P2-10)."""

from typing import Any, Literal

import pytest
from pydantic import BaseModel

from eap_core.exceptions import OutputValidationError
from eap_core.middleware.validate import OutputValidationMiddleware
from eap_core.types import Context, Response


class _Demo(BaseModel):
    name: str
    age: int


async def _validate(
    mode: Literal["strict_json", "extract_json", "provider_native"],
    text: str,
    *,
    payload: Any = None,
) -> Response:
    mw = OutputValidationMiddleware(mode=mode)
    ctx = Context(request_id="r")
    ctx.metadata["output_schema"] = _Demo
    resp = Response(text=text, payload=payload)
    return await mw.on_response(resp, ctx)


# strict_json (default, backwards-compat)


async def test_strict_json_parses_clean_json() -> None:
    out = await _validate("strict_json", '{"name": "alice", "age": 30}')
    assert isinstance(out.payload, _Demo)
    assert out.payload.name == "alice"


async def test_strict_json_rejects_fenced_block() -> None:
    """strict mode doesn't unwrap fences — backwards-compat."""
    with pytest.raises(OutputValidationError):
        await _validate("strict_json", '```json\n{"name": "alice", "age": 30}\n```')


# extract_json


async def test_extract_json_unwraps_fenced_block() -> None:
    out = await _validate(
        "extract_json", 'Here you go:\n```json\n{"name": "alice", "age": 30}\n```\nLMK.'
    )
    assert isinstance(out.payload, _Demo)


async def test_extract_json_unwraps_fenced_block_without_lang_tag() -> None:
    out = await _validate("extract_json", '```\n{"name": "bob", "age": 40}\n```')
    assert out.payload.name == "bob"


async def test_extract_json_finds_first_object_with_prose() -> None:
    out = await _validate(
        "extract_json",
        'The answer is: {"name": "carol", "age": 50}. Hope that helps!',
    )
    assert out.payload.name == "carol"


async def test_extract_json_handles_nested_braces() -> None:
    out = await _validate(
        "extract_json",
        'Result: {"name": "dan", "age": 25, "extra": {"a": 1}}. Done.',
    )
    assert out.payload.name == "dan"


async def test_extract_json_handles_braces_in_strings() -> None:
    """Braces inside JSON strings must not break the depth counter."""
    out = await _validate(
        "extract_json",
        'Reply: {"name": "eve {brace}", "age": 60}.',
    )
    assert out.payload.name == "eve {brace}"


async def test_extract_json_raises_when_no_json_present() -> None:
    with pytest.raises(OutputValidationError):
        await _validate("extract_json", "just prose, no json")


# extract_json — M1 scanner retry past failed candidates (v1.8.1)


async def test_extract_json_recovers_when_fenced_block_contains_garbage() -> None:
    """If a fenced block contains unparseable garbage, the scanner finds JSON later in the text."""
    out = await _validate(
        "extract_json",
        '```json\n{garbage}\n```\nactual: {"name": "alice", "age": 30}',
    )
    assert out.payload.name == "alice"


async def test_extract_json_recovers_past_multiple_failed_candidates() -> None:
    """Multiple failed candidates -> scanner advances past each and finds valid JSON eventually."""
    out = await _validate(
        "extract_json",
        '{not json} also {still not json} but here: {"name": "bob", "age": 40}',
    )
    assert out.payload.name == "bob"


async def test_extract_json_caps_pathological_input() -> None:
    """Pathological input doesn't DoS the scanner — bounded by _MAX_CANDIDATE_ATTEMPTS.

    The cap is a finite bound, not a tight one — it just guarantees the
    scanner halts on inputs with hundreds of garbage candidates. Reasonable
    real inputs (a handful of failed candidates before valid JSON) sail
    through. We use 20 garbage candidates here (well below the 32 cap) so
    the valid trailing JSON is still found.
    """
    # 20 failed mini-candidates, then a valid one (within the 32 cap).
    garbage = " ".join(["{nope}"] * 20)
    out = await _validate(
        "extract_json",
        f'{garbage} {{"name": "carol", "age": 50}}',
    )
    assert out.payload.name == "carol"


async def test_extract_json_cap_bounds_runaway_input() -> None:
    """Truly pathological input (well past the cap) is rejected — no DoS.

    With 100 garbage candidates and the cap at 32, the scanner gives
    up before reaching the valid JSON at the end. The point: the
    scanner halts in O(cap) iterations regardless of input size.
    """
    garbage = " ".join(["{nope}"] * 100)
    with pytest.raises(OutputValidationError):
        await _validate(
            "extract_json",
            f'{garbage} {{"name": "dan", "age": 99}}',
        )


# provider_native


async def test_provider_native_uses_payload_when_set() -> None:
    out = await _validate("provider_native", text="ignored", payload={"name": "frank", "age": 70})
    assert out.payload.name == "frank"


async def test_provider_native_falls_back_to_strict_when_payload_none() -> None:
    out = await _validate("provider_native", text='{"name": "grace", "age": 80}', payload=None)
    assert out.payload.name == "grace"
