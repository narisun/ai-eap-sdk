"""Pin the eap-cli package's public install surface.

eap-cli forwards eap-core's optional extras (pii, otel, aws, gcp, mcp,
a2a, eval, policy-cedar, all). The README documents this; this test
locks the contract so a future pyproject.toml edit can't silently
drop a forwarder.
"""

from __future__ import annotations

from importlib.metadata import metadata


def test_eap_cli_forwards_eap_core_extras() -> None:
    md = metadata("eap-cli")
    extras = set(md.get_all("Provides-Extra") or [])
    expected = {"pii", "otel", "aws", "gcp", "mcp", "a2a", "eval", "policy-cedar", "all", "dev"}
    missing = expected - extras
    assert not missing, (
        f"eap-cli is missing extras: {sorted(missing)}. "
        f"Update packages/eap-cli/pyproject.toml::project.optional-dependencies "
        f"to forward eap-core's extras."
    )
