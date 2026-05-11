"""Cedar engine adapter — decision parity with JsonPolicyEvaluator
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
    JsonPolicyEvaluator,
)


@dataclass
class _Principal:
    client_id: str
    roles: tuple[str, ...] = ()


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
    json_eval = JsonPolicyEvaluator(json_doc)
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
