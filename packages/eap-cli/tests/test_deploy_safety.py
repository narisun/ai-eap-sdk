"""Deploy packager safety: deny-list + .eapignore + manifest (C9)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from eap_cli.scaffolders.deploy import _load_eapignore, _stage_project, package_agentcore


def _seed_minimal_project(project: Path) -> None:
    project.mkdir()
    (project / "agent.py").write_text("async def answer(q): return q\n")
    (project / "pyproject.toml").write_text('[project]\nname="p"\nversion="0.1.0"\n')


def test_packager_excludes_secret_files(tmp_path: Path) -> None:
    project = tmp_path / "p"
    _seed_minimal_project(project)
    (project / ".env").write_text("AWS_SECRET_KEY=hunter2\n")
    (project / "credentials.json").write_text('{"key": "secret"}\n')
    (project / "prod.pem").write_text("-----BEGIN PRIVATE KEY-----\n")
    (project / "terraform.tfstate").write_text('{"resources": []}')
    (project / ".git").mkdir()
    (project / ".git" / "HEAD").write_text("ref: refs/heads/main\n")

    target = package_agentcore(project)
    for forbidden in [".env", "credentials.json", "prod.pem", "terraform.tfstate", ".git/HEAD"]:
        assert not (target / forbidden).exists(), f"{forbidden} leaked into package"

    manifest = target / ".eap-manifest.txt"
    assert manifest.is_file()
    assert ".env" not in manifest.read_text()


def test_packager_honors_eapignore(tmp_path: Path) -> None:
    project = tmp_path / "p"
    _seed_minimal_project(project)
    (project / "do_not_ship.txt").write_text("internal")
    (project / ".eapignore").write_text("do_not_ship.txt\n")
    target = package_agentcore(project)
    assert not (target / "do_not_ship.txt").exists()


def test_packager_skips_symlinks_pointing_outside_tree(tmp_path: Path) -> None:
    """Symlinks (especially to host secrets) must never be staged.

    A file-pointing symlink has ``is_file() is True``; without a symlink
    check ``read_bytes()`` dereferences the target and stages the secret
    payload at the link's name (C9 leak).
    """
    secret = tmp_path / "host_secret"
    secret.write_text("AKIA_FAKE_SUPER_SECRET\n")

    project = tmp_path / "p"
    _seed_minimal_project(project)
    # Point an innocently named file at an out-of-tree "secret".
    (project / "harmless.txt").symlink_to(secret)

    target = package_agentcore(project)
    staged = target / "harmless.txt"
    assert not staged.exists(), "symlink to out-of-tree file leaked into package"
    manifest = (target / ".eap-manifest.txt").read_text()
    assert "harmless.txt" not in manifest


def test_default_deny_is_case_insensitive(tmp_path: Path) -> None:
    """Uppercase secret-named files must still be denied on POSIX."""
    project = tmp_path / "p"
    _seed_minimal_project(project)
    (project / ".ENV").write_text("AWS_SECRET_KEY=hunter2\n")
    (project / "Credentials.json").write_text('{"k":"v"}\n')
    (project / "PROD.PEM").write_text("-----BEGIN PRIVATE KEY-----\n")
    (project / "ID_RSA").write_text("ssh-private-key\n")

    target = package_agentcore(project)
    for forbidden in [".ENV", "Credentials.json", "PROD.PEM", "ID_RSA"]:
        assert not (target / forbidden).exists(), (
            f"{forbidden} leaked into package (case-sensitive deny)"
        )


def test_envrc_is_denied(tmp_path: Path) -> None:
    """``direnv``'s ``.envrc`` is the canonical place secrets live; deny it."""
    project = tmp_path / "p"
    _seed_minimal_project(project)
    (project / ".envrc").write_text("export AWS_SECRET_ACCESS_KEY=hunter2\n")

    target = package_agentcore(project)
    assert not (target / ".envrc").exists(), ".envrc leaked into package"
    assert ".envrc" not in (target / ".eap-manifest.txt").read_text()


def test_stage_project_cleans_partial_state(tmp_path: Path) -> None:
    """A previous partial-write must not leak into the next staging run."""
    project = tmp_path / "p"
    _seed_minimal_project(project)
    target = tmp_path / "dist"
    target.mkdir()
    # Pre-existing stale artifact from a hypothetical broken prior run.
    (target / "STALE_LEFTOVER.txt").write_text("from a previous broken run\n")
    _stage_project(project, target)
    assert not (target / "STALE_LEFTOVER.txt").exists()
    assert (target / "agent.py").is_file()
    assert (target / ".eap-manifest.txt").is_file()


def test_eapignore_comment_with_leading_whitespace(tmp_path: Path) -> None:
    """Indented comment lines must not be treated as patterns."""
    project = tmp_path / "p"
    _seed_minimal_project(project)
    # Indented ``# comment``; without the strip-first fix this becomes
    # a literal pattern and matches a file named ``   # comment``.
    (project / ".eapignore").write_text("   # this is a comment\nagent.py\n")
    patterns = _load_eapignore(project)
    assert patterns == ("agent.py",)


def test_stage_project_empty_package_raises(tmp_path: Path) -> None:
    """``.eapignore`` of ``*`` would silently empty the package — reject it."""
    project = tmp_path / "p"
    _seed_minimal_project(project)
    (project / ".eapignore").write_text("*\n")
    target = tmp_path / "dist"
    with pytest.raises(RuntimeError, match="no project files"):
        _stage_project(project, target)


def test_iter_skips_node_modules_without_descending(tmp_path: Path) -> None:
    """``os.walk`` should prune skip-dirs before scanning their contents."""
    from eap_cli.scaffolders.deploy import _DEFAULT_DENY, _iter_included_files

    project = tmp_path / "p"
    _seed_minimal_project(project)
    deep = project / "node_modules" / "evil"
    deep.mkdir(parents=True)
    (deep / "leak.txt").write_text("should not be visited")

    visited = {p.relative_to(project) for p in _iter_included_files(project, _DEFAULT_DENY, ())}
    # No path under node_modules should appear.
    assert not any(part == "node_modules" for p in visited for part in p.parts)
    # The good files should be present.
    assert Path("agent.py") in visited
    assert Path("pyproject.toml") in visited


def test_stage_project_wraps_read_errors_with_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A raw OSError must be re-raised with deploy context for the user."""
    project = tmp_path / "p"
    _seed_minimal_project(project)
    target = tmp_path / "dist"

    real_write_bytes = Path.write_bytes

    def boom(self: Path, data: bytes) -> int:
        if self.name == "agent.py":
            raise PermissionError(13, "denied")
        return real_write_bytes(self, data)

    monkeypatch.setattr(Path, "write_bytes", boom)
    with pytest.raises(RuntimeError, match="deploy package step"):
        _stage_project(project, target)


def test_stage_project_yields_absolute_sources_relative_paths(tmp_path: Path) -> None:
    """Smoke test of the new ``_iter_included_files`` contract."""
    from eap_cli.scaffolders.deploy import _DEFAULT_DENY, _iter_included_files

    project = tmp_path / "p"
    _seed_minimal_project(project)
    for src in _iter_included_files(project, _DEFAULT_DENY, ()):
        # New contract: yields absolute Paths under ``project``.
        assert src.is_absolute() or src.exists()
        assert (
            src.is_relative_to(project)
            if hasattr(src, "is_relative_to")
            else src.resolve().is_relative_to(project.resolve())
        )
        assert os.fspath(src.relative_to(project))
