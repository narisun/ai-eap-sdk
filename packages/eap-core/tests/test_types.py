import pytest
from pydantic import ValidationError

from eap_core.types import Chunk, Context, Message, Request, Response


def test_message_accepts_str_or_parts():
    m = Message(role="user", content="hello")
    assert m.content == "hello"


def test_request_required_fields():
    r = Request(model="m", messages=[Message(role="user", content="hi")])
    assert r.model == "m"
    assert r.metadata == {}


def test_request_rejects_unknown_role():
    with pytest.raises(ValidationError):
        Message(role="bogus", content="x")


def test_response_carries_payload_and_usage():
    r = Response(text="ok", payload=None, usage={"input_tokens": 3, "output_tokens": 1})
    assert r.text == "ok"
    assert r.usage["input_tokens"] == 3


def test_context_is_mutable_dict_with_vault():
    ctx = Context()
    ctx.vault["TOKEN_1"] = "secret"
    ctx.metadata["foo"] = 42
    assert ctx.vault["TOKEN_1"] == "secret"
    assert ctx.metadata["foo"] == 42
    assert ctx.span is None


def test_chunk_carries_text_and_index():
    c = Chunk(index=0, text="hi", finish_reason=None)
    assert c.index == 0
    assert c.text == "hi"
