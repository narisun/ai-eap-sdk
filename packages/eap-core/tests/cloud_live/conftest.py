"""Fixtures for cloud-live integration tests.

Two-level gating per cloud (AWS / GCP):

1. **Env flag** — ``EAP_LIVE_AWS=1`` / ``EAP_LIVE_GCP=1``. Opt-in: without
   the flag, every test in this directory skips cleanly.
2. **Cred probe** — a small no-op call against the provider's identity
   service (``sts.get_caller_identity`` for AWS, ``google.auth.default``
   for GCP). On failure, skips with a message naming the env vars / cred
   chain the user needs to fix.

When both gates pass, the fixture flips ``EAP_ENABLE_REAL_RUNTIMES=1`` for
the duration of the test session so the SDK's real-runtime gate opens.

The default gauntlet (``pytest -m "not extras and not cloud and not
cloud_live"``) deselects this directory entirely. The cloud-live gauntlet
(``pytest packages/eap-core/tests/cloud_live``) collects them but reports
``X skipped`` unless creds are wired — that skip-clean behavior IS the
framework contract.
"""

from __future__ import annotations

import os
import uuid

import pytest


@pytest.fixture(scope="session")
def live_aws_enabled() -> None:
    """Gate AWS live tests on env flag + STS cred probe.

    Skips with a clear setup message if ``EAP_LIVE_AWS=1`` is unset, or
    if boto3's default cred chain can't reach STS. On success, sets
    ``EAP_ENABLE_REAL_RUNTIMES=1`` so the SDK's real-runtime guard opens.
    """
    if os.environ.get("EAP_LIVE_AWS") != "1":
        pytest.skip(
            "AWS live tests are opt-in. Set EAP_LIVE_AWS=1 and provide AWS "
            "credentials via the standard boto3 chain "
            "(AWS_PROFILE / AWS_ACCESS_KEY_ID+AWS_SECRET_ACCESS_KEY / IAM role)."
        )
    try:
        import boto3
    except ImportError as e:
        pytest.skip(f"boto3 not installed (required for AWS live tests): {e}")
    try:
        sts = boto3.client("sts", region_name=os.environ.get("AWS_REGION", "us-east-1"))
        sts.get_caller_identity()
    except Exception as e:
        # Any failure (missing creds, expired session, network error) means
        # we should skip cleanly rather than fail the run.
        pytest.skip(
            f"AWS credentials invalid or unreachable ({type(e).__name__}: {e}). "
            "Verify AWS_PROFILE / AWS_ACCESS_KEY_ID / IAM role can call sts:GetCallerIdentity."
        )
    # Open the SDK's real-runtime gate for the test session duration.
    os.environ["EAP_ENABLE_REAL_RUNTIMES"] = "1"


@pytest.fixture(scope="session")
def live_gcp_enabled() -> None:
    """Gate GCP live tests on env flag + ADC cred probe.

    Skips with a clear setup message if ``EAP_LIVE_GCP=1`` is unset, or
    if ``google.auth.default`` can't resolve a credential. On success,
    sets ``EAP_ENABLE_REAL_RUNTIMES=1`` so the SDK's real-runtime guard
    opens.
    """
    if os.environ.get("EAP_LIVE_GCP") != "1":
        pytest.skip(
            "GCP live tests are opt-in. Set EAP_LIVE_GCP=1 and provide GCP "
            "credentials via Application Default Credentials "
            "(GOOGLE_APPLICATION_CREDENTIALS / gcloud auth application-default login)."
        )
    try:
        import google.auth
    except ImportError as e:
        pytest.skip(f"google-auth not installed (required for GCP live tests): {e}")
    try:
        google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    except Exception as e:
        pytest.skip(
            f"GCP credentials invalid or unreachable ({type(e).__name__}: {e}). "
            "Verify GOOGLE_APPLICATION_CREDENTIALS or run "
            "`gcloud auth application-default login`."
        )
    os.environ["EAP_ENABLE_REAL_RUNTIMES"] = "1"


@pytest.fixture(scope="session")
def aws_region() -> str:
    """The AWS region to use for live tests. Defaults to ``us-east-1``."""
    return os.environ.get("AWS_REGION", "us-east-1")


@pytest.fixture(scope="session")
def gcp_project_id() -> str:
    """The GCP project id to use for live tests.

    Reads ``GCP_PROJECT_ID`` (preferred) or ``GOOGLE_CLOUD_PROJECT``.
    Skips if neither is set so we never accidentally hit a default
    project.
    """
    val = os.environ.get("GCP_PROJECT_ID") or os.environ.get("GOOGLE_CLOUD_PROJECT")
    if not val:
        pytest.skip(
            "Set GCP_PROJECT_ID (or GOOGLE_CLOUD_PROJECT) to the project id "
            "you want to run GCP live tests against."
        )
    return val


@pytest.fixture(scope="session")
def unique_test_id() -> str:
    """Per-session unique id for tagging cloud artifacts.

    Prevents concurrent test runs (e.g. two devs hitting the same
    registry / memory bank) from colliding on resource names. The id
    is short (12 hex chars) but collision-resistant enough for
    interactive test usage.
    """
    return f"eap-live-{uuid.uuid4().hex[:12]}"
