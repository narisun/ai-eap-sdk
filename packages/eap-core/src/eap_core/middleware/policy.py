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

from eap_core.exceptions import PolicyConfigurationError, PolicyDeniedError
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
        # a trusted source. ``EnterpriseLLM.generate_text``/``stream_text``/
        # ``invoke_tool`` populate ``ctx.metadata['policy.action']`` and
        # ``['policy.resource']`` from values the SDK derives itself (the
        # tool name, the model name) — values the caller cannot influence
        # via ``Request.metadata``. ``Request.metadata`` is caller-mutable
        # and a bad caller could otherwise spoof ``action='tool:lookup_account'``
        # while actually invoking ``transfer_funds`` (H9 / L-N6).
        #
        # No fallback to ``req.metadata`` here: if a non-``EnterpriseLLM``
        # caller wires ``PolicyMiddleware`` into a custom ``MiddlewarePipeline``
        # without setting the trusted slot, refuse the request loudly rather
        # than silently authorizing against caller-controlled input.
        if "policy.action" not in ctx.metadata:
            raise PolicyConfigurationError(
                "PolicyMiddleware called without ctx.metadata['policy.action'] set — "
                "EnterpriseLLM.generate_text/stream_text/invoke_tool always set this; "
                "if you've wired PolicyMiddleware into a custom pipeline, populate it "
                "from a trusted source before passing the request."
            )
        if "policy.resource" not in ctx.metadata:
            raise PolicyConfigurationError(
                "PolicyMiddleware called without ctx.metadata['policy.resource'] set — "
                "EnterpriseLLM.generate_text/stream_text/invoke_tool always set this; "
                "if you've wired PolicyMiddleware into a custom pipeline, populate it "
                "from a trusted source before passing the request."
            )
        action = ctx.metadata["policy.action"]
        resource = ctx.metadata["policy.resource"]
        decision = self._eval.evaluate(ctx.identity, action, resource)
        if not decision.allow:
            raise PolicyDeniedError(rule_id=decision.rule_id, reason=decision.reason)
        ctx.metadata["policy.matched_rule"] = decision.rule_id
        return req
