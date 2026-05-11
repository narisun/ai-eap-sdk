"""Deploy scaffolder branch + error coverage.

Targets the deploy.py paths the existing CLI-driven tests don't reach:

- Helper branches in ``_should_include`` / ``_allow_matches`` /
  ``_iter_included_files`` (directory prefix matches, segment-anywhere
  matches, exclude-subtree handling).
- ``package_aws`` / ``package_gcp`` / ``package_agentcore`` /
  ``package_vertex_agent_engine`` dry-run early-returns. The CLI returns
  before calling these helpers when ``--dry-run`` is passed, so they
  need direct unit invocations.
- ``package_aws`` error branches: empty-package guard, ``OSError`` while
  building the zip (must unlink the partial file).
- The subprocess-driven deploy helpers (``upload_aws``, ``deploy_gcp``,
  ``deploy_agentcore``, ``deploy_vertex_agent_engine``) — exercised
  with patched boto3 / subprocess so no real cloud or shell commands run.
"""

from __future__ import annotations

import sys
import zipfile
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from eap_cli.scaffolders.deploy import (
    _DEFAULT_DENY,
    _allow_matches,
    _iter_included_files,
    _should_include,
    deploy_agentcore,
    deploy_gcp,
    deploy_vertex_agent_engine,
    package_agentcore,
    package_aws,
    package_gcp,
    package_vertex_agent_engine,
    upload_aws,
)


def _seed_minimal_project(project: Path) -> None:
    project.mkdir()
    (project / "agent.py").write_text("async def answer(q): return q\n")
    (project / "pyproject.toml").write_text('[project]\nname="p"\nversion="0.1.0"\n')


# ---- _allow_matches branch coverage ---------------------------------------


def test_allow_matches_directory_prefix_with_star_suffix() -> None:
    """Covers deploy.py:123-126 — allow pattern ``dist/*`` matches both
    the ``dist`` directory itself and any file under it.
    """
    # ``s == prefix`` branch
    assert _allow_matches(Path("dist"), ("dist/*",)) is True
    # ``s.startswith(prefix + "/")`` branch
    assert _allow_matches(Path("dist/bundle.js"), ("dist/*",)) is True


def test_allow_matches_top_level_path_equality() -> None:
    """Covers deploy.py:127-128 — allow pattern ``build`` matches the
    directory itself (``s == pattern``) AND any file under it
    (``s.startswith(pattern + "/")``).
    """
    assert _allow_matches(Path("build"), ("build",)) is True
    assert _allow_matches(Path("build/output.bin"), ("build",)) is True


def test_allow_matches_segment_anywhere() -> None:
    """Covers deploy.py:130-132 — allow pattern matches even when the
    target segment is nested deep in the relative path (``/seg/``
    appearing anywhere inside ``/s/``).
    """
    # ``src/build/output`` — "build" appears as a middle segment.
    assert _allow_matches(Path("src/build/output.bin"), ("build",)) is True


def test_allow_matches_returns_false_when_no_pattern_hits() -> None:
    """Sanity: no pattern matches → False (so excluded paths stay excluded)."""
    assert _allow_matches(Path("secret.env"), ("dist", "build")) is False


# ---- _should_include directory prefix + segment branches -------------------


def test_should_include_default_deny_top_level_path_match() -> None:
    """Covers deploy.py:173-178 — top-level path equality + startswith
    branch for ``_DEFAULT_DENY`` patterns like ``.git`` (no /* suffix).
    A bare ``.git`` is at top level — the prefix-suffix branch shouldn't
    fire — but the path-equality branch must, so the file is excluded.
    """
    # ``.git/HEAD`` is a top-level deny via the ``sl.startswith(pl + "/")`` branch.
    assert _should_include(Path(".git/HEAD"), _DEFAULT_DENY, ()) is False


def test_should_include_user_deny_directory_prefix_match() -> None:
    """Covers deploy.py:191-195 — user deny pattern ending in ``/*`` excludes
    the directory itself via ``s == prefix``. fnmatch alone wouldn't match
    a bare directory path ``cache`` against ``cache/*``, so this branch
    is the load-bearing one for "user said ``cache/*``, file IS the dir".
    """
    # `s == prefix` branch: the directory path equals the stripped pattern.
    assert _should_include(Path("cache"), (), ("cache/*",)) is False


def test_should_include_user_deny_top_level_path_match() -> None:
    """Covers deploy.py:196-198 — user deny pattern ``private`` excludes
    the directory itself and any file at or under it via path-equality
    and startswith.
    """
    assert _should_include(Path("private/notes.md"), (), ("private",)) is False


def test_should_include_user_deny_segment_anywhere() -> None:
    """Covers deploy.py:199-201 — user deny pattern ``secret`` excludes
    a file even when ``secret`` is a nested segment, mirroring the
    default-deny segment-anywhere rule (L-N2).
    """
    assert _should_include(Path("src/secret/value.txt"), (), ("secret",)) is False


# ---- _iter_included_files exclude_subtree branch ---------------------------


def test_iter_included_files_excludes_subtree(tmp_path: Path) -> None:
    """Covers deploy.py:263-264 — when ``exclude_subtree`` resolves to
    a directory inside the project, ``os.walk`` must NOT descend into it
    even though it isn't on the skip-dir list. This is the packager-self-
    output guard.
    """
    project = tmp_path / "p"
    _seed_minimal_project(project)
    # ``my_output`` is not in _DEFAULT_SKIP_DIRS, so without the guard
    # files inside would be staged.
    output_dir = project / "my_output"
    output_dir.mkdir()
    (output_dir / "leak.txt").write_text("don't ship me")

    visited = {
        p.relative_to(project)
        for p in _iter_included_files(project, _DEFAULT_DENY, (), exclude_subtree=output_dir)
    }
    assert Path("agent.py") in visited
    assert Path("my_output/leak.txt") not in visited


def test_iter_included_files_allow_matches_dir_segment(tmp_path: Path) -> None:
    """Covers deploy.py:252-253 — ``_allow_matches_dir`` segment-anywhere
    branch lets an allow pattern rescue a deeply nested skip-dir like
    ``nested/build/`` when the user says ``!build``. Without this, the
    walk would prune ``build/`` even though the user opted it in.
    """
    project = tmp_path / "p"
    _seed_minimal_project(project)
    nested = project / "nested" / "build"
    nested.mkdir(parents=True)
    (nested / "bundle.txt").write_text("frontend bundle")

    visited = {
        p.relative_to(project) for p in _iter_included_files(project, _DEFAULT_DENY, (), ("build",))
    }
    # Without the segment-anywhere branch in _allow_matches_dir, the
    # walk would skip ``nested/build/`` and the bundle would be missing.
    assert Path("nested/build/bundle.txt") in visited


def test_iter_included_files_allow_pattern_with_subpath_descends_into_parent(
    tmp_path: Path,
) -> None:
    """Covers deploy.py:250-251 — ``_allow_matches_dir`` ``base.startswith(s + "/")``
    branch. When the allow pattern names a *subpath* of a skip-dir
    (``build/keep/``), ``_allow_matches_dir`` must say "yes, keep
    descending into ``build/``" so the file inside can be considered.
    Without this, the walk would prune ``build/`` and the kept file
    would never be reached.
    """
    project = tmp_path / "p"
    _seed_minimal_project(project)
    # ``build`` is in _DEFAULT_SKIP_DIRS, so without an allow pattern the
    # walk skips it entirely.
    keep_dir = project / "build" / "keep"
    keep_dir.mkdir(parents=True)
    (keep_dir / "ship.txt").write_text("ship me")

    # The allow pattern names a subpath: "build/keep" — _allow_matches_dir
    # must keep descending into "build" (the skip-dir) because the pattern
    # could match files inside it.
    visited = {
        p.relative_to(project)
        for p in _iter_included_files(project, _DEFAULT_DENY, (), ("build/keep",))
    }
    assert Path("build/keep/ship.txt") in visited


# ---- package_aws dry-run + error branches ---------------------------------


def test_package_aws_dry_run_returns_path_without_writing(tmp_path: Path) -> None:
    """Covers deploy.py:566 — dry_run skips out.mkdir and zip writing,
    returns the would-be target path."""
    project = tmp_path / "p"
    _seed_minimal_project(project)
    target = package_aws(project, dry_run=True)
    assert target == project / "dist" / "agent.zip"
    assert not (project / "dist").exists()


def test_package_aws_empty_package_raises(tmp_path: Path) -> None:
    """Covers deploy.py:574-577 — an .eapignore = '*' empties the package;
    the helper must raise rather than write a useless zip."""
    project = tmp_path / "p"
    _seed_minimal_project(project)
    (project / ".eapignore").write_text("*\n")
    with pytest.raises(RuntimeError, match="no project files"):
        package_aws(project)


def test_package_aws_unlinks_partial_zip_on_oserror(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Covers deploy.py:587-590 — a mid-zip OSError must unlink the partial
    file and re-raise as RuntimeError so the next run starts clean."""
    project = tmp_path / "p"
    _seed_minimal_project(project)

    def boom(self: zipfile.ZipFile, *args: Any, **kwargs: Any) -> None:
        # The first ``zf.write(...)`` call inside the try-block raises.
        # The zip file has already been created on disk (by the
        # ``zipfile.ZipFile(target, "w", ...)`` constructor) so the
        # ``target.exists()`` branch below it will fire.
        raise OSError(28, "no space left")

    monkeypatch.setattr(zipfile.ZipFile, "write", boom)
    with pytest.raises(RuntimeError, match="deploy package step"):
        package_aws(project)
    # Partial zip must have been unlinked.
    assert not (project / "dist" / "agent.zip").exists()


# ---- package_gcp / agentcore / vertex dry-run branches ---------------------


def test_package_gcp_dry_run_returns_path_without_staging(tmp_path: Path) -> None:
    """Covers deploy.py:599 — dry_run short-circuits before _stage_project
    runs, returns the would-be target path."""
    project = tmp_path / "p"
    _seed_minimal_project(project)
    target = package_gcp(project, dry_run=True)
    assert target == project / "dist" / "agent"
    assert not target.exists()


def test_package_agentcore_dry_run_returns_path_without_staging(tmp_path: Path) -> None:
    """Covers deploy.py:684 — dry_run short-circuits before _stage_project."""
    project = tmp_path / "p"
    _seed_minimal_project(project)
    target = package_agentcore(project, dry_run=True)
    assert target == project / "dist" / "agentcore"
    assert not target.exists()


def test_package_vertex_agent_engine_dry_run_returns_path_without_staging(
    tmp_path: Path,
) -> None:
    """Covers deploy.py:998 — dry_run short-circuits before _stage_project."""
    project = tmp_path / "p"
    _seed_minimal_project(project)
    target = package_vertex_agent_engine(project, dry_run=True)
    assert target == project / "dist" / "vertex-agent-engine"
    assert not target.exists()


# ---- Subprocess-driven deploy helpers (mocked) -----------------------------


def test_upload_aws_invokes_boto3_with_expected_args(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Covers deploy.py:612-617 — ``upload_aws`` builds the ``s3://bucket/key``
    URI from the zip name and delegates the actual upload to ``s3.upload_file``.
    """
    zip_path = tmp_path / "agent.zip"
    zip_path.write_bytes(b"PK\x03\x04 fake zip\n")

    fake_boto3 = MagicMock()
    fake_client = MagicMock()
    fake_boto3.client.return_value = fake_client

    # boto3 is lazy-imported inside upload_aws; inject our fake into sys.modules.
    monkeypatch.setitem(sys.modules, "boto3", fake_boto3)

    result = upload_aws(zip_path, "my-bucket")
    assert result == "s3://my-bucket/eap-agents/agent.zip"
    fake_boto3.client.assert_called_once_with("s3")
    fake_client.upload_file.assert_called_once_with(
        str(zip_path), "my-bucket", "eap-agents/agent.zip"
    )


def test_deploy_gcp_runs_gcloud_subprocess(tmp_path: Path) -> None:
    """Covers deploy.py:622-626 — ``deploy_gcp`` shells out to
    ``gcloud run deploy`` with the right service + source args.
    """
    target = tmp_path / "dist" / "agent"
    target.mkdir(parents=True)

    with patch("subprocess.run") as run_mock:
        run_mock.return_value = MagicMock(returncode=0)
        result = deploy_gcp(target, "my-service")

    assert result == "projects/$PROJECT/services/my-service"
    run_mock.assert_called_once()
    cmd = run_mock.call_args.args[0]
    assert cmd[0] == "gcloud"
    assert cmd[1:4] == ["run", "deploy", "my-service"]
    assert cmd[-1] == str(target)


def test_deploy_agentcore_runs_docker_buildx(tmp_path: Path) -> None:
    """Covers deploy.py:703-720 — ``deploy_agentcore`` shells out to
    ``docker buildx build --platform linux/arm64 --load -t <name>:latest <target>``
    and returns the image tag.
    """
    target = tmp_path / "dist" / "agentcore"
    target.mkdir(parents=True)

    with patch("subprocess.run") as run_mock:
        run_mock.return_value = MagicMock(returncode=0)
        image = deploy_agentcore(target, name="my-agent", region="us-east-1")

    assert image == "my-agent:latest"
    run_mock.assert_called_once()
    cmd = run_mock.call_args.args[0]
    assert cmd[:5] == ["docker", "buildx", "build", "--platform", "linux/arm64"]
    assert "--load" in cmd
    assert "my-agent:latest" in cmd
    assert cmd[-1] == str(target)


def test_deploy_vertex_agent_engine_runs_docker_buildx_amd64(tmp_path: Path) -> None:
    """Covers deploy.py:1023-1040 — Vertex builds an amd64 image (unlike
    AgentCore's arm64) and returns the image tag.
    """
    target = tmp_path / "dist" / "vertex-agent-engine"
    target.mkdir(parents=True)

    with patch("subprocess.run") as run_mock:
        run_mock.return_value = MagicMock(returncode=0)
        image = deploy_vertex_agent_engine(
            target, name="my-vertex-agent", project_id="proj", region="us-central1"
        )

    assert image == "my-vertex-agent:latest"
    run_mock.assert_called_once()
    cmd = run_mock.call_args.args[0]
    assert cmd[:5] == ["docker", "buildx", "build", "--platform", "linux/amd64"]
    assert "my-vertex-agent:latest" in cmd
    assert cmd[-1] == str(target)
