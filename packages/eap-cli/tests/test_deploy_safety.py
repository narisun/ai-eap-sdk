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
    deny, allow = _load_eapignore(project)
    assert deny == ["agent.py"]
    assert allow == []


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


def test_packager_excludes_files_under_nested_env_directory(tmp_path: Path) -> None:
    """L-N2: a ``src/.env/config.py`` (where ``.env`` is a SUBDIR, not top-level)
    must not be staged. Deny-list matching has to consider every path segment,
    not just the top-level prefix.
    """
    project = tmp_path / "p"
    _seed_minimal_project(project)
    nested = project / "src" / ".env"
    nested.mkdir(parents=True)
    (nested / "config.py").write_text("SECRET = 'hunter2'\n")
    # Also seed a nested .git/ — same deny semantics.
    (project / "vendor" / ".git").mkdir(parents=True)
    (project / "vendor" / ".git" / "HEAD").write_text("ref: refs/heads/main\n")

    target = package_agentcore(project)
    assert not (target / "src" / ".env" / "config.py").exists(), (
        "nested .env/config.py leaked into package"
    )
    assert not (target / "vendor" / ".git" / "HEAD").exists(), (
        "nested .git/HEAD leaked into package"
    )
    manifest_text = (target / ".eap-manifest.txt").read_text()
    assert ".env/config.py" not in manifest_text
    assert ".git/HEAD" not in manifest_text


def test_packager_skips_new_build_cache_dirs(tmp_path: Path) -> None:
    """L-N5: common build/cache dirs (.terraform, .next, .nuxt, .cache, build,
    target, .tox, .coverage, htmlcov) must be pruned by ``_DEFAULT_SKIP_DIRS``."""
    project = tmp_path / "p"
    _seed_minimal_project(project)
    cache_dirs = [
        ".terraform",
        ".next",
        ".nuxt",
        ".cache",
        "build",
        "target",
        ".tox",
        ".coverage",
        "htmlcov",
    ]
    for cache in cache_dirs:
        d = project / cache
        d.mkdir()
        (d / "huge.bin").write_text("x" * 1024)

    target = package_agentcore(project)
    for cache in cache_dirs:
        assert not (target / cache).exists(), (
            f"{cache}/ leaked into package — missing from _DEFAULT_SKIP_DIRS"
        )


def test_generated_handlers_are_ruff_f401_clean(tmp_path: Path) -> None:
    """L-N3: handler templates ship without unused imports — ruff F401/F811 clean
    in both auth-wired and unauthenticated modes, for both runtimes.
    """
    import shutil
    import subprocess

    from eap_cli.scaffolders.deploy import package_agentcore, package_vertex_agent_engine

    ruff = shutil.which("ruff")
    if ruff is None:
        pytest.skip("ruff binary not on PATH")

    auth_modes: list[dict[str, object] | None] = [
        None,  # --allow-unauthenticated
        {
            "discovery_url": "https://idp/.well-known/openid-configuration",
            "issuer": "https://idp",
            "audiences": ["my-agent"],
        },
    ]
    for auth in auth_modes:
        project = tmp_path / f"p-agentcore-{'auth' if auth else 'unauth'}"
        _seed_minimal_project(project)
        target = package_agentcore(project, auth=auth)
        handler = target / "handler.py"
        assert handler.is_file()
        result = subprocess.run(
            [ruff, "check", "--select=F401", "--select=F811", str(handler)],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, (
            f"agentcore handler (auth={auth is not None}) has unused/redefined "
            f"imports:\n{result.stdout}\n{result.stderr}"
        )

        project = tmp_path / f"p-vertex-{'auth' if auth else 'unauth'}"
        _seed_minimal_project(project)
        target = package_vertex_agent_engine(project, auth=auth)
        handler = target / "handler.py"
        assert handler.is_file()
        result = subprocess.run(
            [ruff, "check", "--select=F401", "--select=F811", str(handler)],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, (
            f"vertex handler (auth={auth is not None}) has unused/redefined "
            f"imports:\n{result.stdout}\n{result.stderr}"
        )


def test_eapignore_negation_reincludes_skip_dir(tmp_path: Path) -> None:
    """L-N1: ``!pattern`` in ``.eapignore`` re-includes a path that the default
    skip-dir set or deny-list would otherwise exclude. A project that legitimately
    needs to ship its ``build/`` (e.g. a pre-built frontend bundle) can opt back in.
    """
    project = tmp_path / "p"
    _seed_minimal_project(project)
    bundle = project / "build"
    bundle.mkdir()
    (bundle / "static.txt").write_text("frontend bundle")
    # ``build/`` is in _DEFAULT_SKIP_DIRS as of v0.6.0; negation re-includes.
    (project / ".eapignore").write_text("!build\n!build/*\n")

    target = package_agentcore(project)
    staged_bundle = target / "build" / "static.txt"
    assert staged_bundle.is_file(), (
        f"build/static.txt was not staged into {target} despite !build negation"
    )

    # Verify the iter contract too — easier to debug than the packaged tree.
    from eap_cli.scaffolders.deploy import _DEFAULT_DENY, _iter_included_files

    fresh = tmp_path / "p2"
    _seed_minimal_project(fresh)
    (fresh / "build").mkdir()
    (fresh / "build" / "static.txt").write_text("frontend bundle")
    visited = {
        p.relative_to(fresh)
        for p in _iter_included_files(fresh, _DEFAULT_DENY, (), ("build", "build/*"))
    }
    assert Path("build/static.txt") in visited, (
        "allow-pattern !build did not re-include build/static.txt"
    )


def test_eapignore_negation_reincludes_nested_segment(tmp_path: Path) -> None:
    """L-N1/L-N2 interplay: a nested ``.env/`` is deny-excluded by L-N2's
    segment-anywhere match. An allow pattern with the same segment opts it
    back in — i.e., the user explicitly says "yes, ship src/.env/templates/".
    """
    from eap_cli.scaffolders.deploy import _DEFAULT_DENY, _iter_included_files

    project = tmp_path / "p"
    _seed_minimal_project(project)
    nested = project / "src" / ".env"
    nested.mkdir(parents=True)
    (nested / "config.py").write_text("# templates, not secrets\n")

    # Without allow: nested .env excluded.
    visited_no_allow = {
        p.relative_to(project) for p in _iter_included_files(project, _DEFAULT_DENY, ())
    }
    assert Path("src/.env/config.py") not in visited_no_allow

    # With explicit allow: re-included.
    visited_allow = {
        p.relative_to(project)
        for p in _iter_included_files(project, _DEFAULT_DENY, (), ("src/.env", "src/.env/*"))
    }
    assert Path("src/.env/config.py") in visited_allow


def test_eapignore_negation_load_returns_split_lists(tmp_path: Path) -> None:
    """L-N1 unit: ``_load_eapignore`` returns ``(deny, allow)`` split by ``!``."""
    project = tmp_path / "p"
    _seed_minimal_project(project)
    (project / ".eapignore").write_text(
        "# comment\ninternal_notes.txt\n!dist\nbuild/cache\n!build/cache/keep.txt\n"
    )
    deny, allow = _load_eapignore(project)
    assert deny == ["internal_notes.txt", "build/cache"]
    assert allow == ["dist", "build/cache/keep.txt"]
