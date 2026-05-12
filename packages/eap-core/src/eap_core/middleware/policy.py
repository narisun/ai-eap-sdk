"""Policy enforcement middleware.

Default evaluator is :class:`SimpleJsonPolicyEvaluator` — a small,
in-house JSON-shaped engine. **It is NOT Cedar-compatible**:

- matches are exact-string-or-wildcard (no Cedar ``like`` / ``in`` /
  attribute access);
- decision algorithm is forbid-before-permit with an optional one-shot
  ``unless`` clause keyed on ``principal.roles``;
- default action is deny.

For full Cedar semantics use :class:`CedarPolicyEvaluator` (requires
the ``[policy-cedar]`` extra).

Decision algorithm:

- Iterate rules in order; collect matching forbids and permits.
- If any forbid matches and its ``unless`` is not satisfied → DENY.
- Else if any permit matches → ALLOW.
- Else → DENY (default deny).

The legacy class name ``JsonPolicyEvaluator`` is kept as a module-level
attribute that emits a :class:`DeprecationWarning` on first lookup
(PEP 562 ``__getattr__``). Switch to :class:`SimpleJsonPolicyEvaluator`
to silence the warning.
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict

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


# ---------------------------------------------------------------------------
# Pydantic models — load-time validation for the JSON document shape
# ---------------------------------------------------------------------------


class PolicyRule(BaseModel):
    """Validated shape of a single :class:`SimpleJsonPolicyEvaluator` rule.

    ``extra="forbid"`` catches typos (``"rsource"`` instead of
    ``"resource"``) at load time instead of silently passing the
    typo'd rule through to evaluation where it would never match.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    effect: Literal["permit", "forbid"]
    principal: str | list[str] = "*"
    action: str | list[str] = "*"
    resource: str | list[str] = "*"
    unless: dict[str, Any] | None = None


class PolicyDocument(BaseModel):
    """Validated :class:`SimpleJsonPolicyEvaluator` policy document.

    Accepts the legacy ``{"version": "1", "rules": [...]}`` shape (the
    ``version`` field is currently ignored but tolerated for forward
    compatibility — explicitly opted out of ``extra="forbid"`` at this
    level).
    """

    rules: list[PolicyRule]

    # NOTE: deliberately NO ``model_config = ConfigDict(extra="forbid")``
    # at the document level — callers ship documents with a top-level
    # ``"version"`` key (and may extend with metadata) and we don't want
    # to break those. Strict validation is at the per-rule level.


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


class SimpleJsonPolicyEvaluator:
    """Minimal default-deny policy evaluator for development and small deployments.

    **NOT Cedar-compatible.** Semantics:

    - Per-rule fields ``principal``/``action``/``resource`` are matched
      as exact strings or the wildcard ``"*"`` (lists are OR'd).
    - ``forbid`` rules win over ``permit`` rules (forbid-before-permit).
    - A ``forbid`` rule may carry ``unless`` with a single
      ``principal_has_role`` key to suppress the forbid for a privileged
      role.
    - Default is DENY when no rule matches.

    The document is validated by :class:`PolicyDocument` at construction
    time, so malformed rules (unknown ``effect``, missing ``id``,
    misspelled fields) raise :class:`pydantic.ValidationError` here
    rather than failing silently at evaluation time.
    """

    def __init__(self, document: dict[str, Any]) -> None:
        # Fail-fast: validate the document shape before keeping any
        # state. Raises ``pydantic.ValidationError`` for malformed rules.
        validated = PolicyDocument.model_validate(document)
        # Convert back to plain dicts so the matcher's
        # ``r.get("principal", "*")`` style keeps working unchanged.
        self._rules: list[dict[str, Any]] = [
            r.model_dump(exclude_none=False) for r in validated.rules
        ]

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


def __getattr__(name: str) -> Any:
    """Module-level deprecation shim (PEP 562).

    ``JsonPolicyEvaluator`` is the legacy v1.x name for
    :class:`SimpleJsonPolicyEvaluator`. Imports continue to work, but
    every reference emits a :class:`DeprecationWarning` so callers see
    the rename.
    """
    if name == "JsonPolicyEvaluator":
        warnings.warn(
            "JsonPolicyEvaluator is deprecated and will be removed in v2.0. "
            "Use SimpleJsonPolicyEvaluator instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        return SimpleJsonPolicyEvaluator
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


class CedarPolicyEvaluator:
    """Real Cedar engine adapter. Requires the ``[policy-cedar]`` extra.

    Unlike :class:`SimpleJsonPolicyEvaluator` (which takes a JSON
    document and runs an in-house matcher), this evaluator takes a
    Cedar DSL policy text and delegates to :func:`cedarpy.is_authorized`
    for the decision. Use this when you need Cedar's full semantics
    (entity hierarchies, ABAC context, ``like`` / ``in`` operators,
    ``when`` / ``unless`` clauses with attribute access).

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
        diagnostics = result.diagnostics
        reasons = diagnostics.reasons
        errors = diagnostics.errors
        rule_id = reasons[0] if reasons else "cedar-default"
        allow = decision == cedarpy.Decision.Allow
        # Build the human-readable reason. For non-Allow decisions we
        # join ``diagnostics.errors`` into the reason string so the
        # actual parse/evaluation failure (e.g. malformed policy text,
        # malformed entity UID) propagates to ``PolicyDeniedError.reason``
        # and is visible in audit logs — otherwise an operator only sees
        # the bare ``NoDecision`` enum and has nothing to debug from.
        if decision == cedarpy.Decision.NoDecision and errors:
            reason = f"cedar decision: NoDecision; errors: {'; '.join(errors)}"
        elif decision == cedarpy.Decision.Deny and not reasons and errors:
            # Defensive: Cedar should set reasons on a normal Deny match,
            # but if it doesn't and there's an error condition, surface it.
            reason = f"cedar decision: Deny; errors: {'; '.join(errors)}"
        else:
            reason = f"cedar decision: {decision.value}"
        return PolicyDecision(
            allow=allow,
            rule_id=rule_id,
            reason=reason,
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
