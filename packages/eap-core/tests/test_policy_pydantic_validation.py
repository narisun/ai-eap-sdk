"""Regression tests for SimpleJsonPolicyEvaluator + Pydantic validation (P1-4)."""

from __future__ import annotations

import warnings

import pytest
from pydantic import ValidationError

from eap_core.middleware.policy import (
    PolicyDocument,
    PolicyRule,
    SimpleJsonPolicyEvaluator,
)


def test_simple_evaluator_is_canonical_name() -> None:
    """The class is canonically named SimpleJsonPolicyEvaluator."""
    assert SimpleJsonPolicyEvaluator.__name__ == "SimpleJsonPolicyEvaluator"


def test_json_policy_evaluator_alias_emits_deprecation_warning() -> None:
    """Accessing the legacy name via PEP 562 __getattr__ warns and
    returns the canonical class."""
    with pytest.warns(DeprecationWarning, match="SimpleJsonPolicyEvaluator"):
        from eap_core.middleware.policy import JsonPolicyEvaluator
    assert JsonPolicyEvaluator is SimpleJsonPolicyEvaluator


def test_json_policy_evaluator_alias_constructs_identically() -> None:
    """Behavioral parity: the legacy alias builds an instance whose
    evaluate() returns the same decisions as the canonical name."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        from eap_core.middleware.policy import JsonPolicyEvaluator

    doc = {
        "rules": [
            {
                "id": "permit-reads",
                "effect": "permit",
                "principal": "*",
                "action": ["read"],
                "resource": "*",
            },
        ]
    }
    legacy = JsonPolicyEvaluator(doc)
    canonical = SimpleJsonPolicyEvaluator(doc)
    decision_a = legacy.evaluate(None, "read", "doc:1")
    decision_b = canonical.evaluate(None, "read", "doc:1")
    assert decision_a.allow == decision_b.allow
    assert decision_a.rule_id == decision_b.rule_id


def test_pydantic_rejects_unknown_effect() -> None:
    """``effect`` must be literally 'permit' or 'forbid'."""
    bad = {"rules": [{"id": "r1", "effect": "wat", "action": "*", "resource": "*"}]}
    with pytest.raises(ValidationError):
        PolicyDocument.model_validate(bad)


def test_pydantic_rejects_missing_id() -> None:
    """Rule must carry an ``id`` for audit-trail correlation."""
    bad = {"rules": [{"effect": "permit", "action": "*", "resource": "*"}]}
    with pytest.raises(ValidationError):
        PolicyDocument.model_validate(bad)


def test_pydantic_rejects_extra_rule_fields() -> None:
    """extra='forbid' catches typos like 'rsource' instead of 'resource'."""
    bad = {"rules": [{"id": "r1", "effect": "permit", "rsource": "*"}]}
    with pytest.raises(ValidationError):
        PolicyDocument.model_validate(bad)


def test_evaluator_load_time_validates_via_pydantic() -> None:
    """Malformed rules must fail at __init__, NOT at evaluate()."""
    bad = {"rules": [{"id": "r1", "effect": "INVALID"}]}
    with pytest.raises(ValidationError):
        SimpleJsonPolicyEvaluator(bad)


def test_evaluator_preserves_existing_semantics_permit_and_forbid() -> None:
    """A valid policy doc evaluates identically to v1.6.x behavior."""
    doc = {
        "rules": [
            {
                "id": "p1",
                "effect": "permit",
                "principal": "*",
                "action": "tool:lookup",
                "resource": "*",
            },
            {
                "id": "f1",
                "effect": "forbid",
                "principal": "*",
                "action": "tool:transfer",
                "resource": "*",
            },
        ]
    }
    ev = SimpleJsonPolicyEvaluator(doc)
    allow_decision = ev.evaluate(None, "tool:lookup", "any")
    deny_decision = ev.evaluate(None, "tool:transfer", "any")
    assert allow_decision.allow is True
    assert allow_decision.rule_id == "p1"
    assert deny_decision.allow is False
    assert deny_decision.rule_id == "f1"


def test_evaluator_accepts_legacy_version_top_level_key() -> None:
    """Documents with ``"version": "1"`` at the top level are accepted
    (the field is tolerated even though it's not modeled). Catches the
    regression where strict extra='forbid' at the document level would
    reject every existing real-world policy."""
    doc = {
        "version": "1",
        "rules": [
            {"id": "r", "effect": "permit", "action": "*", "resource": "*"},
        ],
    }
    # Must not raise.
    ev = SimpleJsonPolicyEvaluator(doc)
    decision = ev.evaluate(None, "anything", "anywhere")
    assert decision.allow is True


def test_policy_rule_defaults_to_wildcards() -> None:
    """Wildcards are the default for principal/action/resource."""
    r = PolicyRule(id="any", effect="permit")
    assert r.principal == "*"
    assert r.action == "*"
    assert r.resource == "*"
    assert r.unless is None
