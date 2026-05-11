"""Pin the eap-cli package's public install surface.

eap-cli forwards eap-core's optional extras (pii, otel, aws, gcp, mcp,
a2a, eval, policy-cedar, all). The README documents this; this test
locks the contract so a future pyproject.toml edit can't silently
drop a forwarder.
"""

from __future__ import annotations

import re
from importlib.metadata import metadata

_FORWARDED = {"pii", "otel", "aws", "gcp", "mcp", "a2a", "eval", "policy-cedar"}


def test_eap_cli_forwards_eap_core_extras() -> None:
    md = metadata("eap-cli")

    # 1. Every forwarder + 'all' + 'dev' must appear in Provides-Extra.
    extras = set(md.get_all("Provides-Extra") or [])
    expected = _FORWARDED | {"all", "dev"}
    missing = expected - extras
    assert not missing, (
        f"eap-cli is missing extras: {sorted(missing)}. "
        f"Update packages/eap-cli/pyproject.toml::project.optional-dependencies."
    )

    # 2. Each forwarder must actually bind to eap-core[<extra>] in
    # Requires-Dist. Without this, `pip install eap-cli[aws]` could
    # be declared but silently install no eap-core extra — the bug
    # v0.6.2 was supposed to prevent.
    requires = md.get_all("Requires-Dist") or []
    for extra in _FORWARDED:
        pattern = re.compile(
            rf"^eap-core\[[^]]*\b{re.escape(extra)}\b[^]]*\];\s*"
            rf"extra\s*==\s*['\"]{re.escape(extra)}['\"]\s*$"
        )
        assert any(pattern.match(r) for r in requires), (
            f"Extra {extra!r} does not forward to eap-core[{extra}] "
            f"in Requires-Dist. Got:\n  " + "\n  ".join(requires)
        )

    # 3. The 'all' aggregator must pull every forwarder via eap-core.
    all_lines = [r for r in requires if "extra == 'all'" in r or 'extra == "all"' in r]
    assert len(all_lines) == 1, f"Expected one Requires-Dist line for extra 'all', got: {all_lines}"
    for sub in _FORWARDED:
        assert sub in all_lines[0], f"`eap-cli[all]` must include {sub!r}; got: {all_lines[0]}"
