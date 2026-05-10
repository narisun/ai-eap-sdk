from pydantic import BaseModel

from eap_core.config import RuntimeConfig
from eap_core.runtimes.local import LocalRuntimeAdapter
from eap_core.types import Message, Request


async def test_returns_canned_response_when_yaml_matches(tmp_path, monkeypatch):
    yaml_file = tmp_path / "responses.yaml"
    yaml_file.write_text("responses:\n  - match: 'capital of France'\n    text: 'Paris.'\n")
    monkeypatch.chdir(tmp_path)
    a = LocalRuntimeAdapter(RuntimeConfig(provider="local", model="echo-1"))
    resp = await a.generate(
        Request(
            model="echo-1",
            messages=[Message(role="user", content="What is the capital of France?")],
        )
    )
    assert resp.text == "Paris."


async def test_falls_back_to_templated_echo():
    a = LocalRuntimeAdapter(RuntimeConfig(provider="local", model="echo-1"))
    resp = await a.generate(
        Request(model="echo-1", messages=[Message(role="user", content="hello world")])
    )
    assert "[local-runtime]" in resp.text
    assert resp.usage["input_tokens"] >= 1


async def test_synthesizes_payload_when_schema_set():
    class Out(BaseModel):
        name: str
        score: int = 0

    a = LocalRuntimeAdapter(RuntimeConfig(provider="local", model="echo-1"))
    req = Request(model="echo-1", messages=[Message(role="user", content="hi")])
    req.metadata["output_schema"] = Out
    resp = await a.generate(req)
    import json

    obj = Out.model_validate(json.loads(resp.text))
    assert obj.score == 0


async def test_streaming_yields_word_chunks():
    a = LocalRuntimeAdapter(RuntimeConfig(provider="local", model="echo-1"))
    chunks = []
    async for c in a.stream(
        Request(model="echo-1", messages=[Message(role="user", content="one two three")])
    ):
        chunks.append(c.text)
    assert len(chunks) >= 2
    assert "".join(chunks).strip().startswith("[local-runtime]")


async def test_list_models_returns_at_least_default():
    a = LocalRuntimeAdapter(RuntimeConfig(provider="local", model="echo-1"))
    models = await a.list_models()
    assert any(m.name == "echo-1" for m in models)


async def test_flatten_prompt_handles_multipart_content():
    from eap_core.runtimes.local import _flatten_prompt

    msgs = [
        Message(role="user", content=[{"type": "text", "text": "hello"}, {"type": "image"}]),
    ]
    result = _flatten_prompt(msgs)
    assert "hello" in result


async def test_synthesize_handles_all_field_types():

    from eap_core.runtimes.local import _synthesize_default

    class Schema(BaseModel):
        name: str
        score: int
        weight: float
        active: bool
        tags: list
        props: dict
        other: str | None = None

    obj = _synthesize_default(Schema)
    assert obj["name"] == ""
    assert obj["score"] == 0
    assert obj["weight"] == 0.0
    assert obj["active"] is False
    assert obj["tags"] == []
    assert obj["props"] == {}
    assert obj["other"] is None


async def test_synthesize_handles_default_factory():
    from pydantic import Field

    from eap_core.runtimes.local import _synthesize_default

    class Schema(BaseModel):
        items: list = Field(default_factory=list)

    obj = _synthesize_default(Schema)
    assert obj["items"] == []
