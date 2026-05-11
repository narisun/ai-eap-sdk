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


class CedarPolicyEvaluator:
    """Real Cedar engine adapter. Requires the ``[policy-cedar]`` extra.

    Unlike :class:`JsonPolicyEvaluator` (which takes a JSON document and
    runs an in-house matcher), this evaluator takes a Cedar DSL policy
    text and delegates to :func:`cedarpy.is_authorized` for the decision.
    Use this when you need Cedar's full semantics (entity hierarchies,
    ABAC context, ``like`` / ``in`` operators, ``when`` / ``unless``
    clauses with attribute access).

    The evaluator maps EAP-Core's ``(principal, action, resource)``
    triple onto Cedar's request shape:

    - principal: ``User::"<client_id>"`` (or ``Unknown::"anonymous"``
      when no ``client_id`` is resolvable)
    - action:    ``Action::"<action>"``
    - resource:  ``Resource::"<resource>"``
    - context:   empty dict (subclass and override ``_context`` to inject)

    Custom entity hierarchies (the third argument to
    :func:`cedarpy.is_authorized`) default to ``[]`` — callers needing
    groups or attributes should subclass and override :meth:`_entities`.
    """

    def __init__(self, policy_text: str) -> None:
        try:
            import cedarpy  # noqa: F401
        except ImportError as e:
            raise PolicyConfigurationError(
                "CedarPolicyEvaluator requires the [policy-cedar] extra "
                "(install with `pip install eap-core[policy-cedar]`)."
            ) from e
        self._policy_text = policy_text

    def _entities(self) -> list[dict[str, Any]]:
        """Entity store passed to Cedar. Default: empty list.

        Override to inject entity hierarchies (parents, attributes) when
        your policies need them.
        """
        return []

    def evaluate(self, principal: Any, action: str, resource: str) -> PolicyDecision:
        import cedarpy

        principal_id = getattr(principal, "client_id", None) if principal else None
        principal_uid = f'User::"{principal_id}"' if principal_id else 'Unknown::"anonymous"'
        request = {
            "principal": principal_uid,
            "action": f'Action::"{action}"',
            "resource": f'Resource::"{resource}"',
            "context": {},
        }
        result = cedarpy.is_authorized(
            request=request,
            policies=self._policy_text,
            entities=self._entities(),
        )
        # cedarpy>=4.x returns an AuthzResult dataclass-like object with
        # ``.decision`` (a ``Decision`` enum: Allow/Deny/NoDecision) and
        # ``.diagnostics`` (with ``.reasons`` list[str] of policy ids and
        # ``.errors`` list). See Step 1.1 of the v0.7.0 plan for the
        # discovery output that pinned this shape.
        decision = result.decision
        reasons = result.diagnostics.reasons
        rule_id = reasons[0] if reasons else "cedar-default"
        allow = decision == cedarpy.Decision.Allow
        return PolicyDecision(
            allow=allow,
            rule_id=rule_id,
            reason=f"cedar decision: {decision.value}",
        )


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
