import pytest

from eap_core.exceptions import PolicyConfigurationError, PolicyDeniedError
from eap_core.middleware.policy import JsonPolicyEvaluator, PolicyMiddleware
from eap_core.types import Context, Message, Request

PERMIT_READS = {
    "version": "1",
    "rules": [
        {
            "id": "allow-reads",
            "effect": "permit",
            "principal": "*",
            "action": ["read"],
            "resource": "*",
        },
        {
            "id": "deny-writes-default",
            "effect": "forbid",
            "principal": "*",
            "action": ["write", "transfer"],
            "resource": "*",
        },
    ],
}


async def test_permits_when_action_matches_permit_rule():
    mw = PolicyMiddleware(JsonPolicyEvaluator(PERMIT_READS))
    ctx = Context()
    ctx.metadata["policy.action"] = "read"
    ctx.metadata["policy.resource"] = "doc:1"
    req = Request(
        model="m",
        messages=[Message(role="user", content="hi")],
    )
    out = await mw.on_request(req, ctx)
    assert out is req


async def test_forbids_when_forbid_rule_matches():
    mw = PolicyMiddleware(JsonPolicyEvaluator(PERMIT_READS))
    ctx = Context()
    ctx.metadata["policy.action"] = "transfer"
    ctx.metadata["policy.resource"] = "acct:1"
    req = Request(
        model="m",
        messages=[Message(role="user", content="hi")],
    )
    with pytest.raises(PolicyDeniedError) as ei:
        await mw.on_request(req, ctx)
    assert ei.value.rule_id == "deny-writes-default"


async def test_default_deny_when_no_rule_matches():
    mw = PolicyMiddleware(JsonPolicyEvaluator({"version": "1", "rules": []}))
    ctx = Context()
    ctx.metadata["policy.action"] = "x"
    ctx.metadata["policy.resource"] = "y"
    req = Request(
        model="m",
        messages=[Message(role="user", content="hi")],
    )
    with pytest.raises(PolicyDeniedError):
        await mw.on_request(req, ctx)


async def test_unless_clause_with_principal_role():
    rules = {
        "version": "1",
        "rules": [
            {
                "id": "deny-writes-without-role",
                "effect": "forbid",
                "principal": "*",
                "action": ["write"],
                "resource": "*",
                "unless": {"principal_has_role": "operator"},
            },
            {
                "id": "allow-writes-for-operator",
                "effect": "permit",
                "principal": "*",
                "action": ["write"],
                "resource": "*",
            },
        ],
    }
    mw = PolicyMiddleware(JsonPolicyEvaluator(rules))
    ctx_op = Context()
    ctx_op.identity = type("I", (), {"roles": ["operator"]})()
    ctx_op.metadata["policy.action"] = "write"
    ctx_op.metadata["policy.resource"] = "x"
    req = Request(
        model="m",
        messages=[Message(role="user", content="hi")],
    )
    out = await mw.on_request(req, ctx_op)
    assert out is req

    ctx_user = Context()
    ctx_user.identity = type("I", (), {"roles": ["viewer"]})()
    ctx_user.metadata["policy.action"] = "write"
    ctx_user.metadata["policy.resource"] = "x"
    with pytest.raises(PolicyDeniedError):
        await mw.on_request(req, ctx_user)


# ---- H9: action/resource derived inside SDK, not from caller metadata ----


async def test_policy_action_is_derived_inside_sdk_not_from_caller_metadata():
    """A malicious caller cannot bypass tool:transfer_funds policy by
    setting metadata['action'] = 'tool:lookup_account' before invoke_tool."""
    from eap_core.client import EnterpriseLLM
    from eap_core.config import RuntimeConfig
    from eap_core.mcp import McpToolRegistry, ToolSpec

    policy = {
        "rules": [
            {
                "id": "deny-writes",
                "effect": "forbid",
                "principal": "*",
                "action": ["tool:transfer_funds"],
                "resource": "*",
            },
            {
                "id": "permit-reads",
                "effect": "permit",
                "principal": "*",
                "action": ["tool:lookup_account"],
                "resource": "*",
            },
        ]
    }
    reg = McpToolRegistry()

    async def _t(**_):
        return {"ok": True}

    reg.register(
        ToolSpec(
            name="transfer_funds",
            description="t",
            input_schema={"type": "object"},
            output_schema=None,
            fn=_t,
            requires_auth=False,
            is_async=True,
        )
    )
    client = EnterpriseLLM(
        RuntimeConfig(provider="local", model="echo-1"),
        middlewares=[PolicyMiddleware(JsonPolicyEvaluator(policy))],
        tool_registry=reg,
    )
    # Even if a caller tried to spoof, action is derived from tool_name.
    with pytest.raises(PolicyDeniedError, match="deny-writes"):
        await client.invoke_tool("transfer_funds", {})


async def test_policy_action_cannot_be_spoofed_via_upstream_middleware():
    """An upstream middleware that mutates ``req.metadata['action']`` MUST
    NOT be able to redirect the policy decision — the SDK plumbs the
    canonical action through ``ctx.metadata`` which we prefer over the
    caller-mutable ``Request.metadata``."""
    from eap_core.client import EnterpriseLLM
    from eap_core.config import RuntimeConfig
    from eap_core.mcp import McpToolRegistry, ToolSpec
    from eap_core.middleware.base import PassthroughMiddleware

    policy = {
        "rules": [
            {
                "id": "deny-writes",
                "effect": "forbid",
                "principal": "*",
                "action": ["tool:transfer_funds"],
                "resource": "*",
            },
            {
                "id": "permit-reads",
                "effect": "permit",
                "principal": "*",
                "action": ["tool:lookup_account"],
                "resource": "*",
            },
        ]
    }
    reg = McpToolRegistry()

    async def _t(**_):
        return {"ok": True}

    reg.register(
        ToolSpec(
            name="transfer_funds",
            description="t",
            input_schema={"type": "object"},
            output_schema=None,
            fn=_t,
            requires_auth=False,
            is_async=True,
        )
    )

    class SpoofingMiddleware(PassthroughMiddleware):
        name = "spoofer"

        async def on_request(self, req: Request, ctx: Context) -> Request:
            # Try to redirect the policy decision by rewriting req.metadata.
            req.metadata["action"] = "tool:lookup_account"
            req.metadata["resource"] = "lookup_account"
            return req

    # SpoofingMiddleware runs BEFORE PolicyMiddleware so it gets the first
    # chance to mutate metadata — the worst case for our trust model.
    client = EnterpriseLLM(
        RuntimeConfig(provider="local", model="echo-1"),
        middlewares=[
            SpoofingMiddleware(),
            PolicyMiddleware(JsonPolicyEvaluator(policy)),
        ],
        tool_registry=reg,
    )
    with pytest.raises(PolicyDeniedError, match="deny-writes"):
        await client.invoke_tool("transfer_funds", {})


async def test_policy_action_membership_not_truthiness():
    """``PolicyMiddleware`` must probe ``ctx.metadata`` for membership of
    ``policy.action``/``policy.resource`` rather than truthiness. If the
    trusted slot is present but falsy (e.g. ``""``), the middleware MUST
    still use it instead of silently falling back to the caller-mutable
    ``req.metadata`` — that fall-through is the H9 spoofing path. This
    test will fail if anyone reverts the fix from membership probing back
    to ``ctx.metadata.get(...) or req.metadata.get(...)``."""
    rules = {
        "version": "1",
        "rules": [
            {
                "id": "permit-reads",
                "effect": "permit",
                "principal": "*",
                "action": ["read"],
                "resource": "*",
            },
        ],
    }
    mw = PolicyMiddleware(JsonPolicyEvaluator(rules))
    ctx = Context()
    # Trusted slot is present but empty — a stand-in for any falsy value.
    # ``or`` would treat this as missing and fall through to ``req.metadata``,
    # letting the caller's spoofed "read" pass. Membership probing pins the
    # decision to the (empty) trusted action, which matches no permit and
    # falls through to default deny.
    ctx.metadata["policy.action"] = ""
    ctx.metadata["policy.resource"] = ""
    req = Request(
        model="m",
        messages=[Message(role="user", content="hi")],
        metadata={"action": "read", "resource": "doc:1"},
    )
    with pytest.raises(PolicyDeniedError):
        await mw.on_request(req, ctx)


@pytest.mark.asyncio
async def test_policy_middleware_refuses_without_trusted_action():
    """PolicyMiddleware must NOT fall back to caller-mutable req.metadata.
    A request reaching the middleware without ctx.metadata['policy.action']
    set is a programming error — fail loudly."""
    from eap_core.middleware.policy import JsonPolicyEvaluator, PolicyMiddleware
    from eap_core.types import Context, Request

    mw = PolicyMiddleware(JsonPolicyEvaluator({"rules": []}))
    req = Request(model="m", messages=[], metadata={"action": "tool:transfer"})  # spoof attempt
    ctx = Context()  # no policy.* metadata set
    with pytest.raises(PolicyConfigurationError, match=r"policy\.action"):
        await mw.on_request(req, ctx)


@pytest.mark.asyncio
async def test_policy_middleware_refuses_without_trusted_resource():
    """Even when policy.action is set, missing policy.resource must
    still refuse rather than fall back to caller-mutable req.metadata."""
    from eap_core.middleware.policy import JsonPolicyEvaluator, PolicyMiddleware
    from eap_core.types import Context, Request

    mw = PolicyMiddleware(JsonPolicyEvaluator({"rules": []}))
    req = Request(model="m", messages=[], metadata={"resource": "acct:1"})
    ctx = Context()
    ctx.metadata["policy.action"] = "read"
    with pytest.raises(PolicyConfigurationError, match=r"policy\.resource"):
        await mw.on_request(req, ctx)
