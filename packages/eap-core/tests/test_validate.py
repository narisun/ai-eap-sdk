import json

import pytest
from pydantic import BaseModel

from eap_core.exceptions import OutputValidationError
from eap_core.middleware.validate import OutputValidationMiddleware
from eap_core.types import Context, Message, Request, Response


class Person(BaseModel):
    name: str
    age: int


async def test_passes_through_when_no_schema():
    mw = OutputValidationMiddleware()
    req = Request(model="m", messages=[Message(role="user", content="hi")])
    ctx = Context()
    await mw.on_request(req, ctx)
    out = await mw.on_response(Response(text="anything"), ctx)
    assert out.text == "anything"


async def test_parses_json_into_pydantic_payload():
    mw = OutputValidationMiddleware()
    req = Request(model="m", messages=[Message(role="user", content="hi")])
    req.metadata["output_schema"] = Person
    ctx = Context()
    await mw.on_request(req, ctx)
    out = await mw.on_response(Response(text=json.dumps({"name": "Ada", "age": 36})), ctx)
    assert isinstance(out.payload, Person)
    assert out.payload.name == "Ada"


async def test_raises_on_invalid_json():
    mw = OutputValidationMiddleware()
    req = Request(model="m", messages=[Message(role="user", content="hi")])
    req.metadata["output_schema"] = Person
    ctx = Context()
    await mw.on_request(req, ctx)
    with pytest.raises(OutputValidationError):
        await mw.on_response(Response(text="not even json"), ctx)


async def test_raises_on_schema_mismatch():
    mw = OutputValidationMiddleware()
    req = Request(model="m", messages=[Message(role="user", content="hi")])
    req.metadata["output_schema"] = Person
    ctx = Context()
    await mw.on_request(req, ctx)
    with pytest.raises(OutputValidationError):
        await mw.on_response(Response(text=json.dumps({"name": "Ada"})), ctx)
