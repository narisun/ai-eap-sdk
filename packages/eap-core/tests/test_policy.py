import pytest

from eap_core.exceptions import PolicyDeniedError
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
    req = Request(
        model="m",
        messages=[Message(role="user", content="hi")],
        metadata={"action": "read", "resource": "doc:1"},
    )
    out = await mw.on_request(req, ctx)
    assert out is req


async def test_forbids_when_forbid_rule_matches():
    mw = PolicyMiddleware(JsonPolicyEvaluator(PERMIT_READS))
    ctx = Context()
    req = Request(
        model="m",
        messages=[Message(role="user", content="hi")],
        metadata={"action": "transfer", "resource": "acct:1"},
    )
    with pytest.raises(PolicyDeniedError) as ei:
        await mw.on_request(req, ctx)
    assert ei.value.rule_id == "deny-writes-default"


async def test_default_deny_when_no_rule_matches():
    mw = PolicyMiddleware(JsonPolicyEvaluator({"version": "1", "rules": []}))
    ctx = Context()
    req = Request(
        model="m",
        messages=[Message(role="user", content="hi")],
        metadata={"action": "x", "resource": "y"},
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
    req = Request(
        model="m",
        messages=[Message(role="user", content="hi")],
        metadata={"action": "write", "resource": "x"},
    )
    out = await mw.on_request(req, ctx_op)
    assert out is req

    ctx_user = Context()
    ctx_user.identity = type("I", (), {"roles": ["viewer"]})()
    with pytest.raises(PolicyDeniedError):
        await mw.on_request(req, ctx_user)
