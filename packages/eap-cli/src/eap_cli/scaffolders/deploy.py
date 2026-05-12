"""`eap deploy` packaging."""

from __future__ import annotations

import fnmatch
import os
import shutil
import zipfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Literal

Runtime = Literal["aws", "gcp", "agentcore", "vertex-agent-engine"]

# ---------------------------------------------------------------------------
# Deny-list + .eapignore + manifest (C9)
# ---------------------------------------------------------------------------
#
# Every packager runs project files through ``_stage_project`` (or
# ``_iter_included_files`` for the zip case) so that secrets, version
# control state, and other host artifacts never reach the deploy image.
# Project authors can add patterns through a top-level ``.eapignore``
# file. Each packaged target also writes ``.eap-manifest.txt`` listing
# every staged file for a pre-push audit.

_DEFAULT_DENY: tuple[str, ...] = (
    ".env",
    ".env.*",
    ".env.local",
    ".env.production",
    ".envrc",
    "credentials*.json",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "*.tfstate",
    "*.tfstate.*",
    ".git",
    ".git/*",
    ".aws",
    ".aws/*",
    "id_rsa",
    "id_rsa.*",
    "id_ed25519",
    "id_ed25519.*",
    ".ssh",
    ".ssh/*",
)
_DEFAULT_SKIP_DIRS: frozenset[str] = frozenset(
    {
        "dist",
        ".venv",
        "venv",
        "__pycache__",
        ".eap",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        "node_modules",
        # v0.6.0 additions (L-N5): common build/cache dirs that bloat
        # the deploy package and never want to be staged by default.
        ".terraform",
        ".next",
        ".nuxt",
        ".cache",
        "build",
        "target",
        ".tox",
        ".coverage",
        "htmlcov",
    }
)


def _load_eapignore(project: Path) -> tuple[list[str], list[str]]:
    """Parse ``.eapignore`` into ``(deny_patterns, allow_patterns)``.

    Lines starting with ``!`` are gitignore-style negations — they
    re-include paths that the default deny-list or ``_DEFAULT_SKIP_DIRS``
    would otherwise exclude (L-N1). All other non-comment, non-blank
    lines are deny patterns matched case-sensitively against the path
    and basename (the user controls exact spelling).

    The strip happens BEFORE the comment check so a line like
    ``"   # comment"`` (with leading whitespace) is not treated as a
    literal pattern. Encoding is passed explicitly for cross-platform
    stability.
    """
    f = project / ".eapignore"
    if not f.is_file():
        return [], []
    deny: list[str] = []
    allow: list[str] = []
    for line in f.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("!"):
            allow.append(stripped[1:])
        else:
            deny.append(stripped)
    return deny, allow


def _allow_matches(rel: Path, user_allow: tuple[str, ...]) -> bool:
    """Return True iff ``rel`` matches at least one ``user_allow`` pattern.

    Allow patterns mirror the deny-pattern shape (case-sensitive
    fnmatch on the basename and full relative path, plus directory
    prefix matching) so a user can re-include a previously-excluded
    path with the same syntax they use to exclude one. Segment-anywhere
    matching also runs here so a nested ``foo/dist/`` can be opted back
    in via ``!dist``.
    """
    if not user_allow:
        return False
    s = str(rel)
    name = rel.name
    for pattern in user_allow:
        if fnmatch.fnmatch(name, pattern) or fnmatch.fnmatch(s, pattern):
            return True
        if pattern.endswith("/*"):
            prefix = pattern[:-2]
            if s == prefix or s.startswith(prefix + "/"):
                return True
        if s == pattern or s.startswith(pattern + "/"):
            return True
        # Segment-anywhere allow — mirrors the deny-side fix (L-N2).
        if "/" + pattern + "/" in "/" + s + "/":
            return True
    return False


def _should_include(
    rel: Path,
    default_deny: tuple[str, ...],
    user_deny: tuple[str, ...],
    user_allow: tuple[str, ...] = (),
) -> bool:
    """Return True iff ``rel`` survives the deny-lists.

    ``_DEFAULT_DENY`` is applied case-insensitively (POSIX filesystems
    are case-sensitive but ``.ENV`` / ``Credentials.json`` / ``PROD.PEM``
    / ``ID_RSA`` must still be denied — secret naming is not always
    lowercase). The user's ``.eapignore`` is treated as case-sensitive
    so users can target exact filenames.

    L-N1: ``user_allow`` patterns (gitignore-style ``!pattern`` lines)
    re-include otherwise-excluded paths. Allow runs AFTER deny so a user
    can opt a single file back in even when its parent dir is denied.

    L-N2: deny-pattern matching is segment-anywhere — a directory name
    like ``.env`` excludes both top-level ``.env/`` AND nested
    ``src/.env/``. Without this, deny only matched ``rel.startswith(pat)``
    and a nested ``.env/`` subdir leaked secrets.
    """
    s = str(rel)
    name = rel.name
    sl = s.lower()
    namel = name.lower()
    # _DEFAULT_DENY — case-insensitive
    excluded = False
    for pattern in default_deny:
        pl = pattern.lower()
        if fnmatch.fnmatchcase(namel, pl) or fnmatch.fnmatchcase(sl, pl):
            excluded = True
            break
        # Directory prefix match: ".git/*" excludes any file under .git/.
        if pl.endswith("/*"):
            prefix = pl[:-2]
            if sl == prefix or sl.startswith(prefix + "/"):
                excluded = True
                break
        # Top-level path match: ".git" excludes any file at or under .git/.
        if sl == pl or sl.startswith(pl + "/"):
            excluded = True
            break
        # Nested-dir match (L-N2): any path segment equals the pattern.
        # The leading + trailing "/" handles top-of-path and nested cases
        # uniformly without a separate branch.
        if "/" + pl + "/" in "/" + sl + "/":
            excluded = True
            break
    if not excluded:
        # user .eapignore deny — case-sensitive (user-controlled exact patterns)
        for pattern in user_deny:
            if fnmatch.fnmatch(name, pattern) or fnmatch.fnmatch(s, pattern):
                excluded = True
                break
            if pattern.endswith("/*"):
                prefix = pattern[:-2]
                if s == prefix or s.startswith(prefix + "/"):
                    excluded = True
                    break
            if s == pattern or s.startswith(pattern + "/"):
                excluded = True
                break
            if "/" + pattern + "/" in "/" + s + "/":
                excluded = True
                break
    if excluded:
        # Allow patterns (L-N1) can opt the path back in.
        return _allow_matches(rel, user_allow)
    return True


def _iter_included_files(
    project: Path,
    default_deny: tuple[str, ...],
    user_deny: tuple[str, ...],
    user_allow: tuple[str, ...] = (),
    exclude_subtree: Path | None = None,
) -> Iterator[Path]:
    """Yield project-relative file paths that survive deny-list + .eapignore.

    Uses ``os.walk`` with in-place ``dirnames`` pruning so we never
    descend into ``_DEFAULT_SKIP_DIRS`` entries (``.venv``,
    ``node_modules``, ...). Symlinks are skipped outright — a symlink
    pointing at ``~/.aws/credentials`` would otherwise dereference via
    ``read_bytes`` and stage the target's secret content without the
    deny-list ever seeing the target path.

    L-N1: ``user_allow`` patterns can re-include directories that the
    default skip-dir set would otherwise prune. We don't prune a
    directory if any allow pattern matches its relative path — the
    walk has to descend so ``_should_include`` can decide each file.

    ``exclude_subtree`` short-circuits the walk for one specific
    subtree even if allow patterns would otherwise rescue it — this
    is how the packager refuses to stage its own ``dist/<runtime>/``
    output back into itself when a user adds ``!dist`` to opt their
    own ``dist/`` source bundle in.
    """
    excluded_resolved = exclude_subtree.resolve() if exclude_subtree is not None else None

    def _allow_matches_dir(dir_rel: Path) -> bool:
        # Match an allow pattern against a directory's relative path —
        # we want to KEEP descending into a dir if any allow pattern
        # might let any file inside it through.
        if not user_allow:
            return False
        s = str(dir_rel)
        name = dir_rel.name
        for pattern in user_allow:
            # Strip a trailing "/*" — "dist/*" should still match dir "dist".
            base = pattern[:-2] if pattern.endswith("/*") else pattern
            if fnmatch.fnmatch(name, base) or fnmatch.fnmatch(s, base):
                return True
            if s == base or s.startswith(base + "/") or base.startswith(s + "/"):
                return True
            if "/" + base + "/" in "/" + s + "/":
                return True
        return False

    for dirpath, dirnames, filenames in os.walk(project, followlinks=False):
        # Prune skip-dirs in place — stops descent before we read them.
        # An allow pattern may rescue a skip-dir; in that case, keep walking.
        # ``exclude_subtree`` is unconditional (packager-self-output guard).
        kept: list[str] = []
        for d in dirnames:
            full = Path(dirpath) / d
            if excluded_resolved is not None and full.resolve() == excluded_resolved:
                continue
            if d not in _DEFAULT_SKIP_DIRS:
                kept.append(d)
                continue
            dir_rel = full.relative_to(project)
            if _allow_matches_dir(dir_rel):
                kept.append(d)
        dirnames[:] = kept
        for fname in filenames:
            src = Path(dirpath) / fname
            # Reject symlinks (file-pointing symlinks survive ``.is_file()``
            # and ``read_bytes`` silently dereferences; that's a C9 leak).
            if src.is_symlink():
                continue
            rel = src.relative_to(project)
            if not _should_include(rel, default_deny, user_deny, user_allow):
                continue
            yield src


def _write_manifest(target: Path, included: list[str]) -> None:
    (target / ".eap-manifest.txt").write_text(
        "# Files staged for deployment (review before push).\n" + "\n".join(sorted(included)) + "\n"
    )


def _stage_project(project: Path, target: Path) -> list[str]:
    """Copy project files into ``target``, honoring deny-list + .eapignore.

    Returns the sorted list of relative paths included (for tests /
    callers). Also writes ``target/.eap-manifest.txt`` listing every
    staged file for pre-push audit. Skipped: deny-list matches,
    ``.eapignore`` matches, ``_DEFAULT_SKIP_DIRS`` entries.

    The target directory is removed and recreated to avoid leaving a
    corrupt partial-write state on disk if a copy fails mid-way through.
    An empty staged set raises — typically caused by an overly broad
    ``.eapignore`` pattern like ``*``.
    """
    # Clean target first so a previous partial-write doesn't bleed into
    # this run. Callers create ``target`` *before* invoking us, but the
    # contract is "the staged dist after _stage_project belongs to this
    # run, full stop".
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True, exist_ok=True)

    user_deny, user_allow = _load_eapignore(project)
    # Refuse to stage the packager's own output back into itself. This
    # only matters when an ``!dist`` allow-pattern rescues the user's
    # source ``dist/`` from skip-dir pruning, since ``target`` lives at
    # ``project/dist/<runtime>/``. Without this guard, ``os.walk`` would
    # discover each freshly-written file as it appears and recurse
    # infinitely deeper into ``dist/<runtime>/dist/<runtime>/...``.
    exclude_subtree = target if target.is_relative_to(project) else None
    included: list[str] = []
    for src in _iter_included_files(
        project,
        _DEFAULT_DENY,
        tuple(user_deny),
        tuple(user_allow),
        exclude_subtree=exclude_subtree,
    ):
        rel = src.relative_to(project)
        dst = target / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            dst.write_bytes(src.read_bytes())
        except OSError as e:
            # Surface enough context that the user understands the failure
            # happened while staging a deploy package, not in their code.
            raise RuntimeError(f"failed to stage {rel} (under deploy package step): {e}") from e
        included.append(str(rel))
    if not included:
        raise RuntimeError(
            "deploy package contains no project files — check your .eapignore "
            "(an empty package usually means a too-broad pattern like '*')"
        )
    _write_manifest(target, included)
    return included


_DOCKERFILE_TEMPLATE = """\
FROM python:3.11-slim
WORKDIR /app
COPY . /app
ARG EAP_CORE_VERSION=1.6.2
ARG EAP_CORE_SOURCE="eap-core @ git+https://github.com/narisun/ai-eap-sdk.git@v${EAP_CORE_VERSION}#subdirectory=packages/eap-core"
RUN pip install --no-cache-dir "${EAP_CORE_SOURCE}" && pip install --no-cache-dir .
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

# AgentCore Runtime expects an ARM64 container on 0.0.0.0:8080 implementing
# the HTTP protocol contract: POST /invocations, GET /ping, optional WS /ws.
# See: https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-http-protocol-contract.html
_AGENTCORE_DOCKERFILE = """\
# syntax=docker/dockerfile:1
# AgentCore Runtime requires ARM64 containers; build with --platform linux/arm64.
FROM --platform=linux/arm64 python:3.11-slim
WORKDIR /app
COPY . /app
ARG EAP_CORE_VERSION=1.6.2
ARG EAP_CORE_SOURCE="eap-core @ git+https://github.com/narisun/ai-eap-sdk.git@v${EAP_CORE_VERSION}#subdirectory=packages/eap-core"
RUN pip install --no-cache-dir "${EAP_CORE_SOURCE}" fastapi 'uvicorn[standard]' \\
    && pip install --no-cache-dir .
ENV PYTHONUNBUFFERED=1
EXPOSE 8080
CMD ["python", "handler.py"]
"""

_AGENTCORE_HANDLER = '''\
"""AgentCore Runtime HTTP protocol handler.

Generated by `eap deploy --runtime agentcore`. Wraps the user's agent
entry point to satisfy the AgentCore HTTP protocol contract:
- POST /invocations  → call the entry function with the prompt
- GET  /ping         → healthcheck
- (Optional) WS /ws  → not implemented by default; add if needed

Entry point: {entry}
"""
{header_warning}
from __future__ import annotations

import importlib.util
import inspect
import sys
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
{auth_imports}

def _load_entry(spec: str):
    """Load an entry of the form `path.py:func` or `module:func`."""
    if ":" not in spec:
        raise RuntimeError(f"entry must be 'path:function', got: {{spec!r}}")
    target, func = spec.split(":", 1)
    p = Path(target)
    if p.suffix == ".py":
        mod_spec = importlib.util.spec_from_file_location(p.stem, p)
        if mod_spec is None or mod_spec.loader is None:
            raise ImportError(f"could not load {{target}}")
        # Ensure the project's directory is on sys.path so local imports work.
        sys.path.insert(0, str(p.resolve().parent))
        module = importlib.util.module_from_spec(mod_spec)
        sys.modules[mod_spec.name] = module
        mod_spec.loader.exec_module(module)
    else:
        module = importlib.import_module(target)
    if not hasattr(module, func):
        raise AttributeError(f"{{target}} has no attribute {{func!r}}")
    return getattr(module, func)


_entry_callable = _load_entry({entry!r})

app = FastAPI(title="eap-core agent on agentcore")
{auth_wiring}

class InvocationRequest(BaseModel):
    prompt: str


class InvocationResponse(BaseModel):
    response: str
    status: str = "success"


class PingResponse(BaseModel):
    status: str
    time_of_last_update: int


@app.post("/invocations", response_model=InvocationResponse)
async def invocations(
    req: InvocationRequest,{claims_param}
) -> InvocationResponse:
    try:
        result: Any = _entry_callable(req.prompt)
        if inspect.isawaitable(result):
            result = await result
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(e)) from e
    return InvocationResponse(response=str(result))


@app.get("/ping", response_model=PingResponse)
async def ping() -> PingResponse:
    return PingResponse(status="Healthy", time_of_last_update=int(time.time()))


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8080)  # noqa: S104
'''

_AGENTCORE_HANDLER_AUTH_IMPORTS = (
    "from fastapi import Depends\n"
    "from eap_core.integrations.agentcore import InboundJwtVerifier, jwt_dependency\n"
)

_AGENTCORE_HANDLER_AUTH_WIRING = """\

_verifier = InboundJwtVerifier(
    discovery_url={discovery_url!r},
    issuer={issuer!r},
    allowed_audiences={audiences!r},
)
_auth_dep = jwt_dependency(_verifier)
"""

_AGENTCORE_HANDLER_UNAUTH_WARNING = """\
# WARNING: This handler runs WITHOUT authentication.
# Pass --auth-discovery-url + --auth-issuer + --auth-audience to wire jwt_dependency.
"""

_AGENTCORE_README = """\
# Agent packaged for AWS Bedrock AgentCore Runtime

This directory was generated by `eap deploy --runtime agentcore`. It
contains an ARM64 Docker container that implements the AgentCore
HTTP protocol contract.

## Authentication

`eap deploy --runtime agentcore` refuses to scaffold an unauthenticated
handler. Pass the OIDC details so `handler.py` wires
`InboundJwtVerifier` + `jwt_dependency` into `POST /invocations`:

```bash
eap deploy --runtime agentcore \\
    --auth-discovery-url https://<your-idp>/.well-known/openid-configuration \\
    --auth-issuer        https://<your-idp> \\
    --auth-audience      my-agent
```

To skip auth wiring for local smoke testing **only**, pass
`--allow-unauthenticated`. The generated handler will then ship without
auth and a `WARNING` comment is emitted at the top of `handler.py`. Do
not use this in production.

## Build the image

```bash
cd dist/agentcore
docker buildx build --platform linux/arm64 -t my-agent:latest .
```

## Push to ECR

```bash
ECR_REGISTRY=<account>.dkr.ecr.us-east-1.amazonaws.com
aws ecr get-login-password --region us-east-1 \\
    | docker login --username AWS --password-stdin "$ECR_REGISTRY"
docker tag my-agent:latest "$ECR_REGISTRY/my-agent:latest"
docker push "$ECR_REGISTRY/my-agent:latest"
```

## Register with AgentCore Runtime

Use the AWS console or:

```bash
aws bedrock-agentcore create-runtime \\
    --name my-agent \\
    --container-uri <account>.dkr.ecr.us-east-1.amazonaws.com/my-agent:latest
```

## Local test

```bash
docker buildx build --platform linux/arm64 -t my-agent:latest --load .
docker run --rm -p 8080:8080 my-agent:latest
# In another terminal:
curl http://localhost:8080/ping
curl -X POST http://localhost:8080/invocations \\
     -H 'Authorization: Bearer <token>' \\
     -H 'Content-Type: application/json' \\
     -d '{"prompt": "hello"}'
```
"""


def _real_deploy_enabled() -> bool:
    return os.environ.get("EAP_ENABLE_REAL_DEPLOY") == "1"


def package_aws(project: Path, *, dry_run: bool = False) -> Path:
    out = project / "dist"
    target = out / "agent.zip"
    if dry_run:
        return target
    out.mkdir(parents=True, exist_ok=True)
    user_deny, user_allow = _load_eapignore(project)
    included = [
        src.relative_to(project)
        for src in _iter_included_files(project, _DEFAULT_DENY, tuple(user_deny), tuple(user_allow))
    ]
    if not included:
        raise RuntimeError(
            "deploy package contains no project files — check your .eapignore "
            "(an empty package usually means a too-broad pattern like '*')"
        )
    rel_strs = sorted(str(rel) for rel in included)
    manifest = "# Files staged for deployment (review before push).\n" + "\n".join(rel_strs) + "\n"
    # Stage the zip atomically: on any failure, unlink the partial file
    # so the next run starts clean instead of inheriting half a zip.
    try:
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
            for rel in included:
                zf.write(project / rel, str(rel))
            zf.writestr(".eap-manifest.txt", manifest)
    except OSError as e:
        if target.exists():
            target.unlink()
        raise RuntimeError(
            f"failed to build deploy zip {target.name} (under deploy package step): {e}"
        ) from e
    return target


def package_gcp(project: Path, *, service: str = "eap-agent", dry_run: bool = False) -> Path:
    out = project / "dist" / "agent"
    if dry_run:
        return out
    out.mkdir(parents=True, exist_ok=True)
    # Stage source FIRST so generated artifacts overwrite (and are not
    # rejected by) any user files of the same name. Manifest reflects
    # the staged project files only — generated artifacts are obvious.
    _stage_project(project, out)
    (out / "Dockerfile").write_text(_DOCKERFILE_TEMPLATE)
    (out / "cloudbuild.yaml").write_text(_CLOUDBUILD_TEMPLATE.format(service=service))
    return out


def upload_aws(zip_path: Path, bucket: str) -> str:
    """Real upload via boto3. Gated by EAP_ENABLE_REAL_DEPLOY=1."""
    import boto3  # lazy

    s3 = boto3.client("s3")
    key = f"eap-agents/{zip_path.name}"
    s3.upload_file(str(zip_path), bucket, key)
    return f"s3://{bucket}/{key}"


def deploy_gcp(target_dir: Path, service: str) -> str:
    """Real deploy via gcloud subprocess. Gated by EAP_ENABLE_REAL_DEPLOY=1."""
    import subprocess

    cmd = ["gcloud", "run", "deploy", service, "--source", str(target_dir)]
    subprocess.run(cmd, check=True)  # noqa: S603
    return f"projects/$PROJECT/services/{service}"


def _render_agentcore_handler(entry: str, auth: dict[str, Any] | None) -> str:
    """Render the AgentCore handler template with optional auth wiring.

    ``auth`` is either ``None`` (allow-unauthenticated path) or a dict
    of the form::

        {"discovery_url": str, "issuer": str, "audiences": list[str]}
    """
    if auth is not None:
        header_warning = ""
        auth_imports = _AGENTCORE_HANDLER_AUTH_IMPORTS
        auth_wiring = _AGENTCORE_HANDLER_AUTH_WIRING.format(
            discovery_url=auth["discovery_url"],
            issuer=auth["issuer"],
            audiences=list(auth["audiences"]),
        )
        claims_param = "\n    claims: dict = Depends(_auth_dep),"
    else:
        header_warning = "\n" + _AGENTCORE_HANDLER_UNAUTH_WARNING
        auth_imports = ""
        auth_wiring = ""
        claims_param = ""
    return _AGENTCORE_HANDLER.format(
        entry=entry,
        header_warning=header_warning,
        auth_imports=auth_imports,
        auth_wiring=auth_wiring,
        claims_param=claims_param,
    )


def package_agentcore(
    project: Path,
    *,
    entry: str = "agent.py:answer",
    auth: dict[str, Any] | None = None,
    dry_run: bool = False,
) -> Path:
    """Package the project for AWS Bedrock AgentCore Runtime.

    Produces an ARM64 Docker context at ``dist/agentcore/`` with a
    ``Dockerfile``, a generated ``handler.py`` that satisfies the
    AgentCore HTTP protocol contract, a deploy ``README.md``, and a
    copy of the user's source files.

    When ``auth`` is provided (``{"discovery_url": str, "issuer": str,
    "audiences": list[str]}``), the generated handler wires
    :class:`~eap_core.integrations.agentcore.InboundJwtVerifier` +
    :func:`~eap_core.integrations.agentcore.jwt_dependency` into
    ``POST /invocations``. When ``auth`` is ``None`` the handler is
    emitted without auth and prefixed with a loud warning comment;
    this mode is only safe for local smoke testing.
    """
    out = project / "dist" / "agentcore"
    if dry_run:
        return out
    out.mkdir(parents=True, exist_ok=True)
    # Stage source FIRST so generated artifacts (Dockerfile, handler.py,
    # README.md) overwrite any user files of the same name and aren't
    # treated as project content.
    _stage_project(project, out)
    (out / "Dockerfile").write_text(_AGENTCORE_DOCKERFILE)
    (out / "handler.py").write_text(_render_agentcore_handler(entry, auth))
    (out / "README.md").write_text(_AGENTCORE_README)
    return out


def deploy_agentcore(target_dir: Path, *, name: str, region: str) -> str:
    """Real deploy: build, push to ECR, register with AgentCore.

    Gated by EAP_ENABLE_REAL_DEPLOY=1. The path is intentionally minimal
    (subprocess to docker + aws CLI) — full automation lives in a future
    phase.
    """
    import subprocess

    image = f"{name}:latest"
    subprocess.run(  # noqa: S603
        [  # noqa: S607  # docker is on PATH for users who opt in
            "docker",
            "buildx",
            "build",
            "--platform",
            "linux/arm64",
            "--load",
            "-t",
            image,
            str(target_dir),
        ],
        check=True,
    )
    return image  # caller still has to ECR-push + create-runtime; doc covers it


# ---------------------------------------------------------------------------
# Vertex Agent Engine packaging
# ---------------------------------------------------------------------------
#
# Vertex Agent Engine accepts several artifact formats: pickled Python,
# source tarball, Dockerfile, pre-built container image URI, or git
# repository. Container image is the most universal — same approach
# we take for AgentCore — but Vertex doesn't require ARM64 like AgentCore
# Runtime does, so we default to linux/amd64. Users can override via
# --platform if their Vertex region prefers ARM64.

_VERTEX_DOCKERFILE = """\
# syntax=docker/dockerfile:1
# Vertex Agent Engine accepts standard linux/amd64 containers.
# Override with --platform on `docker buildx build` if ARM64 is preferred.
FROM --platform=linux/amd64 python:3.11-slim
WORKDIR /app
COPY . /app
ARG EAP_CORE_VERSION=1.6.2
ARG EAP_CORE_SOURCE="eap-core @ git+https://github.com/narisun/ai-eap-sdk.git@v${EAP_CORE_VERSION}#subdirectory=packages/eap-core"
RUN pip install --no-cache-dir "${EAP_CORE_SOURCE}" fastapi 'uvicorn[standard]' \\
    && pip install --no-cache-dir .
ENV PYTHONUNBUFFERED=1
ENV PORT=8080
EXPOSE 8080
CMD ["python", "handler.py"]
"""

_VERTEX_HANDLER = '''\
"""Vertex Agent Engine HTTP handler.

Generated by `eap deploy --runtime vertex-agent-engine`. Wraps the
user's agent entry point to satisfy the Cloud Run convention that
Vertex Agent Runtime uses: PORT environment variable, /invocations
for the agent call, /health for the readiness check.

Entry point: {entry}
"""
{header_warning}
from __future__ import annotations

import importlib.util
import inspect
import os
import sys
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
{auth_imports}

def _load_entry(spec: str):
    """Load an entry of the form ``path.py:func`` or ``module:func``."""
    if ":" not in spec:
        raise RuntimeError(f"entry must be 'path:function', got: {{spec!r}}")
    target, func = spec.split(":", 1)
    p = Path(target)
    if p.suffix == ".py":
        mod_spec = importlib.util.spec_from_file_location(p.stem, p)
        if mod_spec is None or mod_spec.loader is None:
            raise ImportError(f"could not load {{target}}")
        sys.path.insert(0, str(p.resolve().parent))
        module = importlib.util.module_from_spec(mod_spec)
        sys.modules[mod_spec.name] = module
        mod_spec.loader.exec_module(module)
    else:
        module = importlib.import_module(target)
    if not hasattr(module, func):
        raise AttributeError(f"{{target}} has no attribute {{func!r}}")
    return getattr(module, func)


_entry_callable = _load_entry({entry!r})

app = FastAPI(title="eap-core agent on vertex-agent-engine")
{auth_wiring}

class InvocationRequest(BaseModel):
    prompt: str


class InvocationResponse(BaseModel):
    response: str
    status: str = "success"


class HealthResponse(BaseModel):
    status: str
    time_of_last_update: int


@app.post("/invocations", response_model=InvocationResponse)
async def invocations(
    req: InvocationRequest,{claims_param}
) -> InvocationResponse:
    try:
        result: Any = _entry_callable(req.prompt)
        if inspect.isawaitable(result):
            result = await result
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(e)) from e
    return InvocationResponse(response=str(result))


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse(status="OK", time_of_last_update=int(time.time()))


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", "8080"))
    uvicorn.run(app, host="0.0.0.0", port=port)  # noqa: S104
'''

_VERTEX_HANDLER_AUTH_IMPORTS = (
    "from fastapi import Depends\n"
    "from eap_core.integrations.vertex import InboundJwtVerifier, jwt_dependency\n"
)

_VERTEX_HANDLER_AUTH_WIRING = """\

_verifier = InboundJwtVerifier(
    discovery_url={discovery_url!r},
    issuer={issuer!r},
    allowed_audiences={audiences!r},
)
_auth_dep = jwt_dependency(_verifier)
"""

_VERTEX_HANDLER_UNAUTH_WARNING = """\
# WARNING: This handler runs WITHOUT authentication.
# Pass --auth-discovery-url + --auth-issuer + --auth-audience to wire jwt_dependency.
"""

_VERTEX_README = """\
# Agent packaged for GCP Vertex AI Agent Engine

This directory was generated by `eap deploy --runtime vertex-agent-engine`.
It contains an x86_64 Docker container that follows the Cloud Run
convention (PORT env var, `/health` readiness check, `/invocations`
agent endpoint).

## Authentication

`eap deploy --runtime vertex-agent-engine` refuses to scaffold an
unauthenticated handler. Pass the OIDC details so `handler.py` wires
`InboundJwtVerifier` + `jwt_dependency` into `POST /invocations`:

```bash
eap deploy --runtime vertex-agent-engine \\
    --auth-discovery-url https://<your-idp>/.well-known/openid-configuration \\
    --auth-issuer        https://<your-idp> \\
    --auth-audience      my-agent
```

To skip auth wiring for local smoke testing **only**, pass
`--allow-unauthenticated`. The generated handler will then ship without
auth and a `WARNING` comment is emitted at the top of `handler.py`. Do
not use this in production.

## Build the image

```bash
cd dist/vertex-agent-engine
docker buildx build --platform linux/amd64 -t my-agent:latest .
```

## Push to Artifact Registry

```bash
REGION=us-central1
PROJECT=my-gcp-project
REPO=agents

gcloud artifacts repositories create $REPO \\
    --repository-format=docker --location=$REGION || true

docker tag my-agent:latest \\
    $REGION-docker.pkg.dev/$PROJECT/$REPO/my-agent:latest

gcloud auth configure-docker $REGION-docker.pkg.dev
docker push $REGION-docker.pkg.dev/$PROJECT/$REPO/my-agent:latest
```

## Register with Vertex Agent Engine

Use the Python SDK:

```python
from google.cloud import aiplatform

aiplatform.init(project="my-gcp-project", location="us-central1")

remote_agent = aiplatform.AgentEngine.create(
    display_name="my-agent",
    container_uri="us-central1-docker.pkg.dev/my-gcp-project/agents/my-agent:latest",
)
print("Agent deployed:", remote_agent.resource_name)
```

Or `gcloud beta agent-engines create ...` if you prefer the CLI.

## Local test

```bash
docker buildx build --platform linux/amd64 -t my-agent:latest --load .
docker run --rm -p 8080:8080 -e PORT=8080 my-agent:latest
# In another terminal:
curl http://localhost:8080/health
curl -X POST http://localhost:8080/invocations \\
     -H 'Content-Type: application/json' \\
     -d '{"prompt": "hello"}'
```
"""


def _render_vertex_handler(entry: str, auth: dict[str, Any] | None) -> str:
    """Render the Vertex handler template with optional auth wiring.

    ``auth`` is either ``None`` (allow-unauthenticated path) or a dict
    of the form::

        {"discovery_url": str, "issuer": str, "audiences": list[str]}
    """
    if auth is not None:
        header_warning = ""
        auth_imports = _VERTEX_HANDLER_AUTH_IMPORTS
        auth_wiring = _VERTEX_HANDLER_AUTH_WIRING.format(
            discovery_url=auth["discovery_url"],
            issuer=auth["issuer"],
            audiences=list(auth["audiences"]),
        )
        claims_param = "\n    claims: dict = Depends(_auth_dep),"
    else:
        header_warning = "\n" + _VERTEX_HANDLER_UNAUTH_WARNING
        auth_imports = ""
        auth_wiring = ""
        claims_param = ""
    return _VERTEX_HANDLER.format(
        entry=entry,
        header_warning=header_warning,
        auth_imports=auth_imports,
        auth_wiring=auth_wiring,
        claims_param=claims_param,
    )


def package_vertex_agent_engine(
    project: Path,
    *,
    entry: str = "agent.py:answer",
    auth: dict[str, Any] | None = None,
    dry_run: bool = False,
) -> Path:
    """Package the project for GCP Vertex AI Agent Engine.

    Produces a Docker context at ``dist/vertex-agent-engine/`` with a
    ``Dockerfile``, generated ``handler.py``, deploy ``README.md``,
    and a staged copy of the user's source files. The image targets
    linux/amd64 (Cloud Run-compatible) and exposes a Cloud Run-style
    HTTP contract: ``POST /invocations`` for the agent call,
    ``GET /health`` for readiness checks, ``PORT`` env var honored.

    When ``auth`` is provided (``{"discovery_url": str, "issuer": str,
    "audiences": list[str]}``), the generated handler wires
    :class:`~eap_core.integrations.vertex.InboundJwtVerifier` +
    :func:`~eap_core.integrations.vertex.jwt_dependency` into
    ``POST /invocations``. When ``auth`` is ``None`` the handler is
    emitted without auth and prefixed with a loud warning comment;
    this mode is only safe for local smoke testing.
    """
    out = project / "dist" / "vertex-agent-engine"
    if dry_run:
        return out
    out.mkdir(parents=True, exist_ok=True)
    # Stage source FIRST so generated artifacts overwrite any user files
    # of the same name and aren't treated as project content.
    _stage_project(project, out)
    (out / "Dockerfile").write_text(_VERTEX_DOCKERFILE)
    (out / "handler.py").write_text(_render_vertex_handler(entry, auth))
    (out / "README.md").write_text(_VERTEX_README)
    return out


def deploy_vertex_agent_engine(
    target_dir: Path,
    *,
    name: str,
    project_id: str,
    region: str,
) -> str:
    """Real deploy: build x86_64 image locally.

    Gated by ``EAP_ENABLE_REAL_DEPLOY=1``. Push to Artifact Registry
    and registration with Vertex Agent Engine are documented in the
    generated README — keeping this helper minimal matches the
    Phase A pattern for AgentCore.
    """
    import subprocess

    image = f"{name}:latest"
    subprocess.run(  # noqa: S603
        [  # noqa: S607
            "docker",
            "buildx",
            "build",
            "--platform",
            "linux/amd64",
            "--load",
            "-t",
            image,
            str(target_dir),
        ],
        check=True,
    )
    return image
