"""Regression test for deploy Dockerfile install strategy (P2-11)."""

from __future__ import annotations

import re

from eap_cli.scaffolders import deploy

# All three known Dockerfile templates in the deploy scaffolder.
_TEMPLATES = [
    "_DOCKERFILE_TEMPLATE",
    "_AGENTCORE_DOCKERFILE",
    "_VERTEX_DOCKERFILE",
]


def _get_template(name: str) -> str:
    val = getattr(deploy, name, None)
    if val is None:
        return ""
    return val if isinstance(val, str) else ""


def test_all_dockerfiles_use_build_arg_for_eap_core_source() -> None:
    """Generated Dockerfiles must not assume eap-core is reachable on the default index.

    Every Dockerfile must declare ARG EAP_CORE_SOURCE (defaultable) so users
    on internal registries can override without editing the template.
    """
    missing = []
    for name in _TEMPLATES:
        tpl = _get_template(name)
        if not tpl:
            continue
        if "ARG EAP_CORE_SOURCE" not in tpl:
            missing.append(name)
    assert not missing, f"templates missing EAP_CORE_SOURCE build arg: {missing}"


def test_all_dockerfiles_install_eap_core_via_arg() -> None:
    """The RUN pip install line must reference the EAP_CORE_SOURCE build arg."""
    bad = []
    for name in _TEMPLATES:
        tpl = _get_template(name)
        if not tpl:
            continue
        # Must reference the build arg, NOT a bare `eap-core` argument.
        if not re.search(r"RUN pip install[^\n]*\${EAP_CORE_SOURCE}", tpl):
            bad.append(name)
    assert not bad, f"templates installing eap-core without ARG reference: {bad}"


def test_default_eap_core_source_pins_to_git_url() -> None:
    """The default for EAP_CORE_SOURCE must be a git-pinned URL so out-of-box builds work."""
    for name in _TEMPLATES:
        tpl = _get_template(name)
        if not tpl:
            continue
        # Find the ARG EAP_CORE_SOURCE default value.
        m = re.search(r'ARG EAP_CORE_SOURCE="?([^\n"]+)"?', tpl)
        if m is None:
            continue
        default = m.group(1)
        assert "git+https://" in default, (
            f"template {name}: EAP_CORE_SOURCE default should be a git URL "
            f"(eap-core is not on public PyPI yet); got {default!r}"
        )
