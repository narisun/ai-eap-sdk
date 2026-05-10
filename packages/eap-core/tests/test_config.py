import pytest
from pydantic import ValidationError

from eap_core.config import EvalConfig, IdentityConfig, RuntimeConfig


def test_runtime_config_local_minimal():
    c = RuntimeConfig(provider="local", model="echo-1")
    assert c.provider == "local"
    assert c.options == {}


def test_runtime_config_bedrock_with_options():
    c = RuntimeConfig(
        provider="bedrock",
        model="anthropic.claude-3-5-sonnet",
        options={"region": "us-east-1"},
    )
    assert c.options["region"] == "us-east-1"


def test_runtime_config_rejects_empty_model():
    with pytest.raises(ValidationError):
        RuntimeConfig(provider="local", model="")


def test_eval_config_defaults():
    c = EvalConfig()
    assert c.judge_runtime.provider == "local"
    assert c.threshold == 0.7


def test_identity_config_local_default():
    c = IdentityConfig()
    assert c.idp_url is None
    assert c.client_id == "local-agent"
