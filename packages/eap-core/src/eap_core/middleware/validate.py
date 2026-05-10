"""Pydantic v2 output validation middleware.

Schema is read from `req.metadata['output_schema']` (set by EnterpriseLLM
when caller passes `schema=`). Attempts to parse `resp.text` as JSON and
validates against the schema. Result placed in `resp.payload`.
"""

from __future__ import annotations

import json

from pydantic import BaseModel, ValidationError

from eap_core.exceptions import OutputValidationError
from eap_core.middleware.base import PassthroughMiddleware
from eap_core.types import Context, Request, Response


class OutputValidationMiddleware(PassthroughMiddleware):
    name = "output_validation"

    async def on_request(self, req: Request, ctx: Context) -> Request:
        schema = req.metadata.get("output_schema")
        if schema is not None:
            ctx.metadata["output_schema"] = schema
        return req

    async def on_response(self, resp: Response, ctx: Context) -> Response:
        schema: type[BaseModel] | None = ctx.metadata.get("output_schema")
        if schema is None:
            return resp
        try:
            data = json.loads(resp.text)
        except json.JSONDecodeError as e:
            raise OutputValidationError(errors=[{"type": "json_decode", "msg": str(e)}]) from e
        try:
            payload = schema.model_validate(data)
        except ValidationError as e:
            raise OutputValidationError(errors=e.errors()) from e  # type: ignore[arg-type]
        return resp.model_copy(update={"payload": payload})
