"""Cedar engine adapter — decision parity with SimpleJsonPolicyEvaluator
for representative scenarios + Cedar-only feature coverage.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

pytest.importorskip("cedarpy")
pytestmark = pytest.mark.extras

from eap_core.middleware.policy import (
    CedarPolicyEvaluator,
    SimpleJsonPolicyEvaluator,
)


@dataclass
class _Principal:
    client_id: str


# ---- decision-parity matrix ------------------------------------------------

# Each row: (description, json_doc, cedar_doc, principal, action, resource,
# expected_allow). The matrix locks the common cases — permit-all,
# default-deny, action filter, principal pinning both ways — so cedarpy
# bumps that change Cedar's interpretation of these primitives surface
# behavior drift.
PARITY_MATRIX: list[tuple[str, dict[str, Any], str, _Principal | None, str, str, bool]] = [
    (
        "permit-all matches",
        {
            "rules": [
                {
                    "id": "p1",
                    "effect": "permit",
                    "principal": "*",
                    "action": "*",
                    "resource": "*",
                }
            ]
        },
        "permit (principal, action, resource);",
        _Principal("alice"),
        "read",
        "doc:1",
        True,
    ),
    (
        "default-deny when no rule",
        {"rules": []},
        "",  # empty policy set in Cedar: deny by default
        _Principal("alice"),
        "read",
        "doc:1",
        False,
    ),
    (
        "forbid blocks specific action",
        {
            "rules": [
                {
                    "id": "p",
                    "effect": "permit",
                    "principal": "*",
                    "action": "*",
                    "resource": "*",
                },
                {
                    "id": "f",
                    "effect": "forbid",
                    "principal": "*",
                    "action": "transfer",
                    "resource": "*",
                },
            ]
        },
        "permit (principal, action, resource);\n"
        'forbid (principal, action == Action::"transfer", resource);',
        _Principal("alice"),
        "transfer",
        "acct:1",
        False,
    ),
    (
        "principal pinning permits the right client",
        {
            "rules": [
                {
                    "id": "p",
                    "effect": "permit",
                    "principal": "alice",
                    "action": "*",
                    "resource": "*",
                }
            ]
        },
        'permit (principal == User::"alice", action, resource);',
        _Principal("alice"),
        "read",
        "doc:1",
        True,
    ),
    (
        "principal pinning denies the wrong client",
        {
            "rules": [
                {
                    "id": "p",
                    "effect": "permit",
                    "principal": "alice",
                    "action": "*",
                    "resource": "*",
                }
            ]
        },
        'permit (principal == User::"alice", action, resource);',
        _Principal("bob"),
        "read",
        "doc:1",
        False,
    ),
]


@pytest.mark.parametrize(
    "desc,json_doc,cedar_doc,principal,action,resource,expected_allow",
    PARITY_MATRIX,
    ids=[row[0] for row in PARITY_MATRIX],
)
def test_cedar_parity_with_json_evaluator(
    desc: str,
    json_doc: dict[str, Any],
    cedar_doc: str,
    principal: _Principal | None,
    action: str,
    resource: str,
    expected_allow: bool,
) -> None:
    json_eval = SimpleJsonPolicyEvaluator(json_doc)
    cedar_eval = CedarPolicyEvaluator(cedar_doc)
    json_decision = json_eval.evaluate(principal, action, resource)
    cedar_decision = cedar_eval.evaluate(principal, action, resource)
    assert json_decision.allow == expected_allow, (
        f"JSON evaluator disagreed: {desc} -> {json_decision}"
    )
    assert cedar_decision.allow == expected_allow, (
        f"Cedar evaluator disagreed: {desc} -> {cedar_decision}"
    )


# ---- Cedar-only features (no JSON-evaluator equivalent) --------------------


def test_cedar_when_clause_with_context() -> None:
    """Cedar's ``when { ... }`` clauses are richer than JSON's ``unless``.
    This locks in a feature the JSON evaluator can't express.
    """
    policy = (
        'permit (principal, action == Action::"read", resource)\n'
        'when { principal == User::"alice" };'
    )
    e = CedarPolicyEvaluator(policy)
    assert e.evaluate(_Principal("alice"), "read", "doc:1").allow is True
    assert e.evaluate(_Principal("bob"), "read", "doc:1").allow is False


def test_cedar_anonymous_principal_when_no_client_id() -> None:
    """When no client_id is resolvable, the evaluator maps the principal
    to ``Unknown::"anonymous"`` — a Cedar policy that pins
    ``principal == User::"..."`` must deny such a request.
    """
    policy = 'permit (principal == User::"alice", action, resource);'
    e = CedarPolicyEvaluator(policy)
    # principal=None reaches the Unknown::"anonymous" branch.
    decision = e.evaluate(None, "read", "doc:1")
    assert decision.allow is False
    assert decision.reason.startswith("cedar decision:")


def test_cedar_malformed_policy_surfaces_diagnostics_in_reason() -> None:
    """Malformed policy text drives cedarpy to ``Decision.NoDecision``
    with ``diagnostics.errors`` populated. The evaluator must surface
    those errors in ``PolicyDecision.reason`` (joined into the string)
    so operators see the actual parse failure in audit logs rather than
    just the bare ``NoDecision`` enum.
    """
    e = CedarPolicyEvaluator("not valid cedar syntax")
    decision = e.evaluate(_Principal("alice"), "read", "doc:1")
    assert decision.allow is False
    assert "errors:" in decision.reason


def test_cedar_missing_extra_raises_configuration_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If cedarpy is not importable, construction must raise
    PolicyConfigurationError — not a bare ImportError that confuses the
    operator about what's missing.
    """
    import builtins
    import sys

    from eap_core.exceptions import PolicyConfigurationError

    # Drop any cached cedarpy entry so the import in CedarPolicyEvaluator
    # actually runs, then make the import itself raise ImportError.
    monkeypatch.delitem(sys.modules, "cedarpy", raising=False)
    real_import = builtins.__import__

    def fake_import(
        name: str,
        globals_: Any = None,
        locals_: Any = None,
        fromlist: Any = (),
        level: int = 0,
    ) -> Any:
        if name == "cedarpy":
            raise ImportError("simulated missing cedarpy")
        return real_import(name, globals_, locals_, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    with pytest.raises(PolicyConfigurationError, match=r"\[policy-cedar\] extra"):
        CedarPolicyEvaluator("permit (principal, action, resource);")


# ---- entity-store depth (H8) ----------------------------------------------
#
# These tests cover Cedar features beyond the decision-parity matrix:
#   * entity attributes referenced via ``principal.<attr>``
#   * entity hierarchy referenced via ``principal in Group::"..."``
#   * missing-attribute behavior (Cedar emits an evaluation error,
#     evaluator must surface it cleanly without crashing)
#   * schema validation (``cedarpy.validate_policies``) — catching
#     mistyped entity types before they reach production
#   * round-trip JSON policy form (``policies_to_json_str`` /
#     ``policies_from_json_str``) — exercise the alternate ingest path
#
# Step 1.1 investigation summary (cedarpy 4.x):
#   - ``is_authorized(request, policies, entities, schema=None)`` accepts
#     a list-of-dicts entity store with ``uid`` / ``attrs`` / ``parents``.
#   - ``validate_policies(policies, schema)`` returns a
#     ``ValidationResult`` with ``validation_passed`` and ``errors``.
#   - Template-policy *linking* (filling in ``?principal`` slots) is
#     NOT exposed: a policy containing a slot is parsed as a template
#     and a request against it returns ``Decision.Deny`` with no
#     reasons/errors (template can't authorize on its own). We therefore
#     do not test template instantiation — only that the surface
#     gracefully denies on an unlinked template.
#
# Pattern: subclass ``CedarPolicyEvaluator`` and override the
# ``_entities()`` hook to inject the test entity store. The production
# class stays minimal (option (b) in the plan).


def _make_evaluator_with_entities(
    policy_text: str, entities: list[dict[str, Any]]
) -> CedarPolicyEvaluator:
    """Build a CedarPolicyEvaluator subclass that injects ``entities``
    into the Cedar request.

    Production callers that need an entity store should subclass and
    override ``_entities`` in their own evaluator; this factory does the
    same thing inline for tests.
    """

    class _E(CedarPolicyEvaluator):
        def _entities(self) -> list[dict[str, Any]]:
            return entities

    return _E(policy_text)


def test_cedar_entity_attribute_admin_role_permits() -> None:
    """Policy: ``when { principal.role == "admin" }``. Entity store
    provides ``User::"alice"`` with ``role: "admin"``. Expect permit.
    """
    policy = 'permit (principal, action, resource) when { principal.role == "admin" };'
    entities = [
        {
            "uid": {"type": "User", "id": "alice"},
            "attrs": {"role": "admin"},
            "parents": [],
        },
    ]
    e = _make_evaluator_with_entities(policy, entities)
    decision = e.evaluate(_Principal("alice"), "read", "doc:1")
    assert decision.allow is True
    assert decision.rule_id == "policy0"


def test_cedar_entity_attribute_wrong_role_denies() -> None:
    """Same policy as above; entity has ``role: "user"`` instead of
    ``"admin"``. Expect deny — the ``when`` clause must not match.
    """
    policy = 'permit (principal, action, resource) when { principal.role == "admin" };'
    entities = [
        {
            "uid": {"type": "User", "id": "alice"},
            "attrs": {"role": "user"},
            "parents": [],
        },
    ]
    e = _make_evaluator_with_entities(policy, entities)
    decision = e.evaluate(_Principal("alice"), "read", "doc:1")
    assert decision.allow is False
    assert decision.reason == "cedar decision: Deny"


def test_cedar_entity_hierarchy_group_membership_permits() -> None:
    """Policy: ``principal in Group::"engineers"``. Entity store wires
    ``User::"alice"`` as a child of ``Group::"engineers"``. Expect permit.
    """
    policy = 'permit (principal in Group::"engineers", action, resource);'
    entities: list[dict[str, Any]] = [
        {
            "uid": {"type": "User", "id": "alice"},
            "attrs": {},
            "parents": [{"type": "Group", "id": "engineers"}],
        },
        {
            "uid": {"type": "Group", "id": "engineers"},
            "attrs": {},
            "parents": [],
        },
    ]
    e = _make_evaluator_with_entities(policy, entities)
    decision = e.evaluate(_Principal("alice"), "read", "doc:1")
    assert decision.allow is True


def test_cedar_entity_hierarchy_non_member_denied() -> None:
    """Same policy; ``User::"bob"`` is in the entity store but has no
    ``Group::"engineers"`` parent. Expect deny — the ``in`` clause must
    not match for non-members.
    """
    policy = 'permit (principal in Group::"engineers", action, resource);'
    entities = [
        {
            "uid": {"type": "User", "id": "bob"},
            "attrs": {},
            "parents": [],
        },
        {
            "uid": {"type": "Group", "id": "engineers"},
            "attrs": {},
            "parents": [],
        },
    ]
    e = _make_evaluator_with_entities(policy, entities)
    decision = e.evaluate(_Principal("bob"), "read", "doc:1")
    assert decision.allow is False


def test_cedar_missing_entity_attribute_surfaces_clean_error() -> None:
    """Policy expects ``principal.tier`` but the entity store doesn't
    provide that attribute. Cedar treats this as an evaluation error
    (decision becomes ``Deny`` with ``diagnostics.errors`` populated).
    The evaluator must surface the error message in ``reason`` rather
    than crashing or returning a confusing bare ``Deny``.

    Notes on the cedarpy 4.x contract observed in Step 1.1:
      - decision: ``Decision.Deny`` (not ``NoDecision``)
      - reasons: ``[]`` (no policy matched cleanly)
      - errors: ``["error while evaluating policy `policy0`: "
                  "`User::\"alice\"` does not have the attribute `tier`"]``
    The Deny+errors+no-reasons combination is exactly the defensive
    branch in ``CedarPolicyEvaluator.evaluate``.
    """
    policy = 'permit (principal, action, resource) when { principal.tier == "gold" };'
    entities = [
        {
            "uid": {"type": "User", "id": "alice"},
            "attrs": {"role": "admin"},  # no ``tier``
            "parents": [],
        },
    ]
    e = _make_evaluator_with_entities(policy, entities)
    decision = e.evaluate(_Principal("alice"), "read", "doc:1")
    assert decision.allow is False
    # Either the NoDecision-with-errors branch or the Deny-with-errors
    # defensive branch should fire — both surface ``errors:`` in the
    # reason. Bare ``cedar decision: Deny`` would mean the error was
    # silently dropped, which is the regression we're guarding against.
    assert "errors:" in decision.reason
    assert "tier" in decision.reason


def test_cedar_schema_validation_accepts_well_typed_policy() -> None:
    """A policy that uses only entity types declared in the schema
    passes ``cedarpy.validate_policies`` with ``validation_passed=True``
    and an empty ``errors`` list.
    """
    import cedarpy

    schema = {
        "": {
            "entityTypes": {
                "User": {
                    "shape": {
                        "type": "Record",
                        "attributes": {"role": {"type": "String"}},
                    }
                },
                "Resource": {"shape": {"type": "Record", "attributes": {}}},
            },
            "actions": {
                "read": {
                    "appliesTo": {
                        "principalTypes": ["User"],
                        "resourceTypes": ["Resource"],
                    }
                },
            },
        }
    }
    policy = (
        'permit (principal, action == Action::"read", resource)\n'
        '  when { principal.role == "admin" };'
    )
    result = cedarpy.validate_policies(policy, schema)
    assert result.validation_passed is True
    assert result.errors == []


def test_cedar_schema_validation_rejects_undeclared_entity_type() -> None:
    """A policy that references ``Bogus::"x"`` — an entity type that
    isn't declared in the schema — must fail validation. This is the
    line of defense for catching typos and stale policies before they
    ship to production, which Cedar's runtime would otherwise silently
    treat as no-match.
    """
    import cedarpy

    schema = {
        "": {
            "entityTypes": {
                "User": {"shape": {"type": "Record", "attributes": {}}},
                "Resource": {"shape": {"type": "Record", "attributes": {}}},
            },
            "actions": {
                "read": {
                    "appliesTo": {
                        "principalTypes": ["User"],
                        "resourceTypes": ["Resource"],
                    }
                },
            },
        }
    }
    bad_policy = 'permit (principal == Bogus::"x", action, resource);'
    result = cedarpy.validate_policies(bad_policy, schema)
    assert result.validation_passed is False
    assert result.errors, "expected at least one ValidationError"
    # The first error should mention the unrecognized entity type.
    joined = " ".join(getattr(err, "error", str(err)) for err in result.errors)
    assert "Bogus" in joined or "unrecognized" in joined


def test_cedar_schema_passed_to_is_authorized_still_permits() -> None:
    """Pass a schema *through* ``is_authorized`` (cedarpy supports the
    ``schema=`` kwarg). A request that conforms to the schema must still
    evaluate normally — schema enforcement at authz time tightens entity
    validation without changing decisions for valid input.

    This test exercises a code path we don't (yet) wire into
    ``CedarPolicyEvaluator`` itself; it locks in the cedarpy contract
    so a future SDK enhancement (schema-aware evaluator) has a known
    baseline to build on.
    """
    import cedarpy

    schema = {
        "": {
            "entityTypes": {
                "User": {
                    "shape": {
                        "type": "Record",
                        "attributes": {"role": {"type": "String"}},
                    }
                },
                "Resource": {"shape": {"type": "Record", "attributes": {}}},
            },
            "actions": {
                "read": {
                    "appliesTo": {
                        "principalTypes": ["User"],
                        "resourceTypes": ["Resource"],
                    }
                },
            },
        }
    }
    policy = 'permit (principal == User::"alice", action == Action::"read", resource);'
    request = {
        "principal": 'User::"alice"',
        "action": 'Action::"read"',
        "resource": 'Resource::"doc1"',
        "context": {},
    }
    entities = [
        {
            "uid": {"type": "User", "id": "alice"},
            "attrs": {"role": "admin"},
            "parents": [],
        },
    ]
    result = cedarpy.is_authorized(
        request=request, policies=policy, entities=entities, schema=schema
    )
    assert result.decision == cedarpy.Decision.Allow
    assert result.diagnostics.reasons == ["policy0"]


def test_cedar_policy_json_roundtrip_evaluates_identically() -> None:
    """``cedarpy.policies_to_json_str`` / ``policies_from_json_str``
    let callers ingest policies in JSON form (e.g. stored as a row in
    a config service) and convert back to Cedar DSL for evaluation.
    A round-trip must preserve decisions, otherwise the alternate
    ingest path is a silent integrity hole.
    """
    import cedarpy

    original = 'permit (principal == User::"alice", action, resource);'
    json_form = cedarpy.policies_to_json_str(original)
    # round-trip back to Cedar DSL
    roundtripped = cedarpy.policies_from_json_str(json_form)

    e_original = CedarPolicyEvaluator(original)
    e_roundtrip = CedarPolicyEvaluator(roundtripped)

    for who, expected in [("alice", True), ("bob", False)]:
        d_orig = e_original.evaluate(_Principal(who), "read", "doc:1")
        d_rt = e_roundtrip.evaluate(_Principal(who), "read", "doc:1")
        assert d_orig.allow is expected, f"original disagreed for {who}: {d_orig}"
        assert d_rt.allow is expected, f"roundtrip disagreed for {who}: {d_rt}"


def test_cedar_template_with_unlinked_slot_denies_without_crash() -> None:
    """cedarpy 4.x parses ``?principal`` as a template slot but does
    NOT expose a public API for *linking* templates to concrete
    principals at runtime. A request against an unlinked template
    therefore evaluates to ``Deny`` (no policies match) — not a crash.

    This test pins that contract: if a future cedarpy release starts
    auto-linking template slots from the request, the decision would
    flip to ``Allow`` and we'd want to know about it.
    """
    policy = "permit (principal == ?principal, action, resource);"
    e = CedarPolicyEvaluator(policy)
    decision = e.evaluate(_Principal("alice"), "read", "doc:1")
    assert decision.allow is False
    # Specifically, this is a plain Deny — not a NoDecision/errors path.
    assert decision.reason == "cedar decision: Deny"
