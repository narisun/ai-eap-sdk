"""Test fixtures for users of EAP-Core."""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from eap_core.client import EnterpriseLLM
from eap_core.config import RuntimeConfig
from eap_core.middleware.observability import ObservabilityMiddleware
from eap_core.middleware.pii import PiiMaskingMiddleware
from eap_core.middleware.policy import JsonPolicyEvaluator, PolicyMiddleware
from eap_core.middleware.sanitize import PromptInjectionMiddleware
from eap_core.middleware.validate import OutputValidationMiddleware
from eap_core.types import Context

_PERMIT_ALL = {
    "version": "1",
    "rules": [
        {"id": "permit-all-in-tests", "effect": "permit", "principal": "*", "action": "*", "resource": "*"},
    ],
}


def make_test_client(
    *,
    model: str = "echo-1",
    extra_middlewares=None,
) -> EnterpriseLLM:
    """A pre-wired EnterpriseLLM with LocalRuntimeAdapter and a permissive policy."""
    chain = [
        PromptInjectionMiddleware(),
        PiiMaskingMiddleware(),
        ObservabilityMiddleware(),
        PolicyMiddleware(JsonPolicyEvaluator(_PERMIT_ALL)),
        OutputValidationMiddleware(),
    ]
    if extra_middlewares:
        chain.extend(extra_middlewares)
    return EnterpriseLLM(RuntimeConfig(provider="local", model=model), middlewares=chain)


@contextmanager
def capture_traces() -> Iterator[list[dict]]:
    """Collects ctx.metadata snapshots after each request runs.

    Hooks into ObservabilityMiddleware via a local subclass so we don't
    require the OTel SDK.
    """
    captured: list[dict] = []

    original = ObservabilityMiddleware.on_response

    async def _on_response(self, resp, ctx: Context):
        result = await original(self, resp, ctx)
        captured.append(dict(ctx.metadata))
        return result

    ObservabilityMiddleware.on_response = _on_response  # type: ignore[method-assign]
    try:
        yield captured
    finally:
        ObservabilityMiddleware.on_response = original  # type: ignore[method-assign]


def assert_pii_round_trip(original: str, processed: str, vault: dict[str, str]) -> None:
    """Asserts that every original PII fragment is captured in the vault."""
    for token, value in vault.items():
        assert value in original, f"vault entry {token}={value!r} not found in original"
        assert token in processed, f"token {token} not present in processed text"
