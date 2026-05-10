"""`eap deploy` packaging."""
from __future__ import annotations

import os
import zipfile
from pathlib import Path
from typing import Literal

Runtime = Literal["aws", "gcp"]

_DOCKERFILE_TEMPLATE = """\
FROM python:3.11-slim
WORKDIR /app
COPY . /app
RUN pip install --no-cache-dir eap-core && pip install --no-cache-dir .
ENV PYTHONUNBUFFERED=1
CMD ["python", "agent.py"]
"""

_CLOUDBUILD_TEMPLATE = """\
steps:
  - name: gcr.io/cloud-builders/docker
    args: ["build", "-t", "gcr.io/$PROJECT_ID/{service}:latest", "."]
  - name: gcr.io/cloud-builders/docker
    args: ["push", "gcr.io/$PROJECT_ID/{service}:latest"]
images:
  - "gcr.io/$PROJECT_ID/{service}:latest"
"""


def _real_deploy_enabled() -> bool:
    return os.environ.get("EAP_ENABLE_REAL_DEPLOY") == "1"


def package_aws(project: Path, *, dry_run: bool = False) -> Path:
    out = project / "dist"
    target = out / "agent.zip"
    if dry_run:
        return target
    out.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
        for src in project.rglob("*"):
            if not src.is_file():
                continue
            rel = src.relative_to(project)
            if rel.parts and rel.parts[0] in {"dist", ".venv", "__pycache__", ".eap"}:
                continue
            zf.write(src, str(rel))
    return target


def package_gcp(project: Path, *, service: str = "eap-agent", dry_run: bool = False) -> Path:
    out = project / "dist" / "agent"
    if dry_run:
        return out
    out.mkdir(parents=True, exist_ok=True)
    (out / "Dockerfile").write_text(_DOCKERFILE_TEMPLATE)
    (out / "cloudbuild.yaml").write_text(_CLOUDBUILD_TEMPLATE.format(service=service))
    # Stage source alongside Dockerfile for image build.
    for src in project.rglob("*"):
        if not src.is_file():
            continue
        rel = src.relative_to(project)
        if rel.parts and rel.parts[0] in {"dist", ".venv", "__pycache__", ".eap"}:
            continue
        target = out / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(src.read_bytes())
    return out


def upload_aws(zip_path: Path, bucket: str) -> str:
    """Real upload via boto3. Gated by EAP_ENABLE_REAL_DEPLOY=1."""
    import boto3  # type: ignore[import-not-found]  # lazy
    s3 = boto3.client("s3")
    key = f"eap-agents/{zip_path.name}"
    s3.upload_file(str(zip_path), bucket, key)
    return f"s3://{bucket}/{key}"


def deploy_gcp(target_dir: Path, service: str) -> str:
    """Real deploy via gcloud subprocess. Gated by EAP_ENABLE_REAL_DEPLOY=1."""
    import subprocess  # noqa: S404
    cmd = ["gcloud", "run", "deploy", service, "--source", str(target_dir)]
    subprocess.run(cmd, check=True)  # noqa: S603
    return f"projects/$PROJECT/services/{service}"
