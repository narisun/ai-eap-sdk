# packages/eap-cli/tests/test_deploy.py
from pathlib import Path

from click.testing import CliRunner
from eap_cli.main import cli


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "demo"
    project.mkdir()
    (project / "agent.py").write_text("# agent\n")
    (project / "pyproject.toml").write_text('[project]\nname = "demo"\nversion = "0.1.0"\n')
    return project


def test_deploy_aws_packaging_writes_zip(tmp_path: Path, monkeypatch):
    project = _project(tmp_path)
    monkeypatch.chdir(project)
    runner = CliRunner()
    result = runner.invoke(cli, ["deploy", "--runtime", "aws"])
    assert result.exit_code == 0, result.output
    zip_path = project / "dist" / "agent.zip"
    assert zip_path.is_file()
    assert "aws s3 cp" in result.output


def test_deploy_gcp_packaging_writes_dockerfile_and_cloudbuild(tmp_path: Path, monkeypatch):
    project = _project(tmp_path)
    monkeypatch.chdir(project)
    runner = CliRunner()
    result = runner.invoke(cli, ["deploy", "--runtime", "gcp"])
    assert result.exit_code == 0, result.output
    assert (project / "dist" / "agent" / "Dockerfile").is_file()
    assert (project / "dist" / "agent" / "cloudbuild.yaml").is_file()
    assert "gcloud run deploy" in result.output


def test_deploy_dry_run_writes_nothing(tmp_path: Path, monkeypatch):
    project = _project(tmp_path)
    monkeypatch.chdir(project)
    runner = CliRunner()
    result = runner.invoke(cli, ["deploy", "--runtime", "aws", "--dry-run"])
    assert result.exit_code == 0
    assert not (project / "dist").exists()


def test_deploy_live_blocked_without_env_flag(tmp_path: Path, monkeypatch):
    project = _project(tmp_path)
    monkeypatch.chdir(project)
    monkeypatch.delenv("EAP_ENABLE_REAL_DEPLOY", raising=False)
    runner = CliRunner()
    result = runner.invoke(cli, ["deploy", "--runtime", "aws", "--bucket", "my-bucket"])
    assert result.exit_code == 0
    assert "EAP_ENABLE_REAL_DEPLOY" in result.output
    # Package was created but no upload attempted.
    assert (project / "dist" / "agent.zip").is_file()
