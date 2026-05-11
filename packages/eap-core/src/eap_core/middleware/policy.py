"""Policy enforcement middleware.

Default evaluator is a small JSON-based engine modeled on Cedar's
principal/action/resource/condition shape. Optional cedarpy adapter
swaps in real Cedar semantics when the [policy-cedar] extra is
installed.

Decision algorithm:
- Iterate rules in order; collect matching forbids and permits.
- If any forbid matches and its `unless` is not satisfied → DENY.
- Else if any permit matches → ALLOW.
- Else → DENY (default deny).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from eap_core.exceptions import PolicyDeniedError
from eap_core.middleware.base import PassthroughMiddleware
from eap_core.types import Context, Request


@dataclass
class PolicyDecision:
    allow: bool
    rule_id: str
    reason: str


class PolicyEvaluator(Protocol):
    def evaluate(self, principal: Any, action: str, resource: str) -> PolicyDecision: ...


def _matches(value: str, pattern: str | list[str]) -> bool:
    if isinstance(pattern, list):
        return any(_matches(value, p) for p in pattern)
    return pattern in ("*", value)


def _condition_holds(condition: dict[str, Any], principal: Any) -> bool:
    role = condition.get("principal_has_role")
    if role is not None:
        roles = getattr(principal, "roles", []) if principal is not None else []
        return role in roles
    return True


class JsonPolicyEvaluator:
    def __init__(self, document: dict[str, Any]) -> None:
        self._rules = document.get("rules", [])

    def evaluate(self, principal: Any, action: str, resource: str) -> PolicyDecision:
        principal_id = getattr(principal, "client_id", "*") if principal else "*"
        for r in self._rules:
            if r["effect"] != "forbid":
                continue
            if not _matches(principal_id, r.get("principal", "*")):
                continue
            if not _matches(action, r.get("action", "*")):
                continue
            if not _matches(resource, r.get("resource", "*")):
                continue
            unless = r.get("unless")
            if unless is None or not _condition_holds(unless, principal):
                return PolicyDecision(False, r["id"], "matched forbid rule")
        for r in self._rules:
            if r["effect"] != "permit":
                continue
            if not _matches(principal_id, r.get("principal", "*")):
                continue
            if not _matches(action, r.get("action", "*")):
                continue
            if not _matches(resource, r.get("resource", "*")):
                continue
            return PolicyDecision(True, r["id"], "matched permit rule")
        return PolicyDecision(False, "default-deny", "no rule matched")


class PolicyMiddleware(PassthroughMiddleware):
    name = "policy"

    def __init__(self, evaluator: PolicyEvaluator) -> None:
        self._eval = evaluator

    async def on_request(self, req: Request, ctx: Context) -> Request:
        # ``action``/``resource`` are authorization inputs and MUST come from
        # a trusted source. When the SDK plumbed canonical values through
        # ``ctx.metadata`` (set inside ``EnterpriseLLM.invoke_tool`` /
        # ``generate_text``), prefer those over anything in ``req.metadata`` —
        # ``Request.metadata`` is caller-mutable and a bad caller could
        # otherwise spoof ``action='tool:lookup_account'`` while actually
        # invoking ``transfer_funds`` (H9).
        #
        # Probe membership rather than truthiness: a caller (or a middleware)
        # that managed to set ``ctx.metadata['policy.action'] = ''`` must not
        # cause the trusted slot to silently yield to ``req.metadata`` via
        # ``or``'s falsy fall-through. The trust invariant lives here, not in
        # the SDK call sites that populate ``ctx.metadata``.
        if "policy.action" in ctx.metadata:
            action = ctx.metadata["policy.action"]
        else:
            action = req.metadata.get("action", "generate_text")
        if "policy.resource" in ctx.metadata:
            resource = ctx.metadata["policy.resource"]
        else:
            resource = req.metadata.get("resource", req.model)
        decision = self._eval.evaluate(ctx.identity, action, resource)
        if not decision.allow:
            raise PolicyDeniedError(rule_id=decision.rule_id, reason=decision.reason)
        ctx.metadata["policy.matched_rule"] = decision.rule_id
        return req
