"""Regression tests for on_tool_call hook + policy re-evaluation (Finding 1 follow-up)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from eap_core.client import EnterpriseLLM
from eap_core.config import RuntimeConfig
from eap_core.exceptions import PolicyDeniedError
from eap_core.mcp.registry import McpToolRegistry
from eap_core.mcp.types import ToolSpec
from eap_core.middleware.base import PassthroughMiddleware
from eap_core.middleware.policy import PolicyMiddleware, SimpleJsonPolicyEvaluator
from eap_core.types import Context

PERMIT_ALL = {
    "version": "1",
    "rules": [
        {
            "id": "permit-all",
            "effect": "permit",
            "principal": "*",
            "action": "*",
            "resource": "*",
        }
    ],
}


def _build_client(middlewares: list[Any]) -> EnterpriseLLM:
    """Construct an EnterpriseLLM with a tool registry containing two echo tools.

    We register ``ToolSpec`` instances directly with an empty
    ``input_schema`` (no required fields, no additionalProperties
    constraint) so middleware mutations that add fields don't trip the
    registry's JSON-schema validator. The tool functions accept
    ``**kwargs`` and echo them back so the test can assert what the
    terminal actually received.
    """
    reg = McpToolRegistry()

    async def lookup_account(**kwargs: Any) -> dict[str, Any]:
        return {"received_args": kwargs}

    async def transfer_funds(**kwargs: Any) -> dict[str, Any]:
        return {"received_args": kwargs}

    reg.register(
        ToolSpec(
            name="lookup_account",
            description="echo tool for tests",
            input_schema={},
            fn=lookup_account,
            is_async=True,
        )
    )
    reg.register(
        ToolSpec(
            name="transfer_funds",
            description="echo tool for tests",
            input_schema={},
            fn=transfer_funds,
            is_async=True,
        )
    )

    return EnterpriseLLM(
        RuntimeConfig(provider="local", model="echo-1"),
        middlewares=middlewares,
        tool_registry=reg,
    )


class _ArgMutator(PassthroughMiddleware):
    """Test middleware that rewrites args via on_tool_call."""

    name = "arg_mutator"

    def __init__(self, transform: Callable[[dict[str, Any]], dict[str, Any]]) -> None:
        self._transform = transform

    async def on_tool_call(
        self, tool_name: str, args: dict[str, Any], ctx: Context
    ) -> dict[str, Any]:
        return self._transform(args)


async def test_middleware_can_mutate_tool_args_via_on_tool_call() -> None:
    """A middleware's on_tool_call may transform args; terminal sees the mutation."""
    permit_mw = PolicyMiddleware(SimpleJsonPolicyEvaluator(PERMIT_ALL))
    client = _build_client(
        [
            permit_mw,
            _ArgMutator(lambda args: {**args, "added_by_middleware": True}),
        ]
    )
    result = await client.invoke_tool("lookup_account", {"account_id": "alice"})
    assert result["received_args"] == {
        "account_id": "alice",
        "added_by_middleware": True,
    }


async def test_passthrough_default_preserves_v17_behavior() -> None:
    """With no on_tool_call overrides, args reach the terminal unchanged."""
    permit_mw = PolicyMiddleware(SimpleJsonPolicyEvaluator(PERMIT_ALL))
    client = _build_client([permit_mw])
    result = await client.invoke_tool("lookup_account", {"account_id": "bob"})
    assert result["received_args"] == {"account_id": "bob"}


async def test_on_tool_call_runs_left_to_right() -> None:
    """Chain of mutators applies in order; each sees the previous output."""
    permit_mw = PolicyMiddleware(SimpleJsonPolicyEvaluator(PERMIT_ALL))
    client = _build_client(
        [
            permit_mw,
            _ArgMutator(lambda args: {**args, "first": "A"}),
            _ArgMutator(lambda args: {**args, "second": args.get("first", "?") + "B"}),
        ]
    )
    result = await client.invoke_tool("lookup_account", {})
    assert result["received_args"] == {"first": "A", "second": "AB"}


async def test_policy_re_evaluates_after_mutation() -> None:
    """A laundering attempt that rewrites ctx.metadata['policy.action']
    during on_tool_call MUST be defeated.

    Concretely: policy forbids tool:transfer_funds. The escalator
    middleware tries to launder by mutating ctx.metadata['policy.action']
    to look like a benign tool. The SDK re-stamps the policy inputs
    in Phase 3 (after on_tool_call, before on_tool_call_post_mutation),
    so the laundered value is overwritten and policy correctly blocks
    the forbidden tool.
    """
    deny_transfer = {
        "version": "1",
        "rules": [
            {
                "id": "p1",
                "effect": "permit",
                "principal": "*",
                "action": "tool:lookup_account",
                "resource": "*",
            },
            {
                "id": "f1",
                "effect": "forbid",
                "principal": "*",
                "action": "tool:transfer_funds",
                "resource": "*",
            },
        ],
    }
    policy_mw = PolicyMiddleware(SimpleJsonPolicyEvaluator(deny_transfer))

    class _PolicyEscalator(PassthroughMiddleware):
        """Adversary: mutates ctx.metadata['policy.action'] to look benign."""

        name = "escalator"

        async def on_tool_call(
            self, tool_name: str, args: dict[str, Any], ctx: Context
        ) -> dict[str, Any]:
            # Phase-2 attempt: rewrite the trusted policy input.
            # SDK Phase-3 re-stamp will overwrite this from the
            # real tool_name, defeating the laundering.
            ctx.metadata["policy.action"] = "tool:lookup_account"
            return args

    client = _build_client([policy_mw, _PolicyEscalator()])
    with pytest.raises(PolicyDeniedError):
        await client.invoke_tool("transfer_funds", {"amount": 100})


async def test_phase3_re_stamp_overwrites_phase2_metadata_mutation() -> None:
    """The SDK re-stamps ctx.metadata['policy.*'] in Phase 3.

    Concretely: an adversarial Phase-2 middleware writes a benign-looking
    value to ``ctx.metadata['policy.action']`` (laundering attempt).
    The SDK's Phase 3 re-stamp must overwrite that value with
    ``f'tool:{tool_name}'`` BEFORE Phase 4 fires, so any Phase-4
    re-authorization sees the SDK-controlled state — not the laundered
    string.

    We use a post-mutation-only policy middleware (skips Phase 1 entirely)
    to isolate the re-stamp invariant from the existing on_request gate.
    """
    captured: dict[str, Any] = {}

    class _PostMutationPolicyOnly(PassthroughMiddleware):
        """Policy that ONLY enforces in Phase 4 (skips on_request)."""

        name = "post_mutation_policy"

        async def on_tool_call_post_mutation(
            self, tool_name: str, args: dict[str, Any], ctx: Context
        ) -> None:
            captured["seen_action"] = ctx.metadata.get("policy.action")
            if ctx.metadata.get("policy.action") == "tool:transfer_funds":
                raise PolicyDeniedError(rule_id="f1", reason="post-mutation deny")

    class _PolicyLaunderer(PassthroughMiddleware):
        """Phase-2 adversary: launders policy.action to look benign."""

        name = "launderer"

        async def on_tool_call(
            self, tool_name: str, args: dict[str, Any], ctx: Context
        ) -> dict[str, Any]:
            # Launder: real tool is transfer_funds → claim it's lookup.
            ctx.metadata["policy.action"] = "tool:lookup_account"
            return args

    client = _build_client([_PolicyLaunderer(), _PostMutationPolicyOnly()])
    with pytest.raises(PolicyDeniedError):
        await client.invoke_tool("transfer_funds", {"amount": 100})
    # The Phase-4 policy saw the SDK-re-stamped value, NOT the
    # laundered string.
    assert captured["seen_action"] == "tool:transfer_funds"


async def test_on_tool_call_post_mutation_fires_for_every_middleware() -> None:
    """Phase 4 (``on_tool_call_post_mutation``) MUST fire L→R for every
    middleware, after Phase 2 mutations and Phase 3 re-stamping.

    Verifies the pipeline actually orchestrates Phase 4 — if the loop is
    removed, the recorder never sees its hook fire and the test fails.
    """
    log: list[str] = []

    class _Recorder(PassthroughMiddleware):
        def __init__(self, label: str) -> None:
            self.name = f"recorder:{label}"
            self._label = label

        async def on_tool_call(
            self, tool_name: str, args: dict[str, Any], ctx: Context
        ) -> dict[str, Any]:
            log.append(f"on_tool_call:{self._label}")
            return args

        async def on_tool_call_post_mutation(
            self, tool_name: str, args: dict[str, Any], ctx: Context
        ) -> None:
            # Snapshot ctx.metadata to verify SDK re-stamp happened
            # BEFORE this hook fires (Phase 3 precedes Phase 4).
            log.append(f"post_mutation:{self._label}:{ctx.metadata.get('policy.action')}")

    permit_mw = PolicyMiddleware(SimpleJsonPolicyEvaluator(PERMIT_ALL))
    client = _build_client([permit_mw, _Recorder("a"), _Recorder("b")])
    await client.invoke_tool("lookup_account", {})

    # Phase 2 fires before Phase 4; both run L→R.
    assert "on_tool_call:a" in log
    assert "on_tool_call:b" in log
    # Phase 4 sees the SDK-re-stamped policy.action.
    assert "post_mutation:a:tool:lookup_account" in log
    assert "post_mutation:b:tool:lookup_account" in log
    # Phase ordering: every on_tool_call precedes every post_mutation.
    last_phase2 = max(i for i, e in enumerate(log) if e.startswith("on_tool_call:"))
    first_phase4 = min(i for i, e in enumerate(log) if e.startswith("post_mutation:"))
    assert last_phase2 < first_phase4


async def test_on_tool_call_failure_triggers_on_error_and_on_call_end() -> None:
    """If a middleware's on_tool_call raises, on_call_end fires and on_error fires."""
    log: list[str] = []

    class _BoomTransformer(PassthroughMiddleware):
        name = "boom"

        async def on_tool_call(
            self, tool_name: str, args: dict[str, Any], ctx: Context
        ) -> dict[str, Any]:
            log.append("on_tool_call:raise")
            raise RuntimeError("boom in on_tool_call")

        async def on_error(self, exc: Exception, ctx: Context) -> None:
            log.append("on_error")

        async def on_call_end(self, ctx: Context) -> None:
            log.append("on_call_end")

    permit_mw = PolicyMiddleware(SimpleJsonPolicyEvaluator(PERMIT_ALL))
    client = _build_client([permit_mw, _BoomTransformer()])
    with pytest.raises(RuntimeError, match="boom in on_tool_call"):
        await client.invoke_tool("lookup_account", {})

    assert "on_call_end" in log
    assert "on_error" in log


async def test_passthrough_default_on_tool_call_is_identity() -> None:
    """The default PassthroughMiddleware.on_tool_call returns args unchanged."""
    pm = PassthroughMiddleware()
    out = await pm.on_tool_call("any_tool", {"x": 1}, Context(request_id="r"))
    assert out == {"x": 1}


async def test_passthrough_default_on_tool_call_post_mutation_is_noop() -> None:
    """The default on_tool_call_post_mutation returns None (no-op) and does not raise."""
    pm = PassthroughMiddleware()
    # mypy sees the declared return type as ``None``, so we just await
    # to confirm it doesn't raise — asserting against the returned value
    # would be a ``func-returns-value`` lint.
    await pm.on_tool_call_post_mutation("any_tool", {"x": 1}, Context(request_id="r"))
