"""Pydantic v2 output validation middleware.

Schema is read from `req.metadata['output_schema']` (set by EnterpriseLLM
when caller passes `schema=`). Attempts to parse `resp.text` as JSON and
validates against the schema. Result placed in `resp.payload`.

Modes (P2-10):

- ``strict_json`` (default): ``json.loads(resp.text)``. Backward-compat
  with v1.7.
- ``extract_json``: pulls the first JSON object/array from ``resp.text``.
  Handles fenced triple-backtick json blocks (with or without lang tag)
  via regex, then falls back to a bracket-counter scan that respects
  string and escape boundaries so braces inside strings don't break
  parsing.
- ``provider_native``: trusts ``resp.payload`` (set by adapters that
  support native structured output). Falls back to ``strict_json`` when
  ``resp.payload`` is ``None`` (``{}`` and ``[]`` are valid payloads and
  do NOT trigger the fallback).
"""

from __future__ import annotations

import json
import re
from typing import Any, Literal

from pydantic import BaseModel, ValidationError

from eap_core.exceptions import OutputValidationError
from eap_core.middleware.base import PassthroughMiddleware
from eap_core.types import Context, Request, Response

_FENCED_BLOCK_RE = re.compile(r"```(?:json)?\s*(.+?)```", re.DOTALL | re.IGNORECASE)


class OutputValidationMiddleware(PassthroughMiddleware):
    name = "output_validation"

    def __init__(
        self,
        mode: Literal["strict_json", "extract_json", "provider_native"] = "strict_json",
    ) -> None:
        self._mode = mode

    async def on_request(self, req: Request, ctx: Context) -> Request:
        schema = req.metadata.get("output_schema")
        if schema is not None:
            ctx.metadata["output_schema"] = schema
        return req

    async def on_response(self, resp: Response, ctx: Context) -> Response:
        schema: type[BaseModel] | None = ctx.metadata.get("output_schema")
        if schema is None:
            return resp
        data = self._extract_data(resp)
        try:
            payload = schema.model_validate(data)
        except ValidationError as e:
            raise OutputValidationError(errors=e.errors()) from e  # type: ignore[arg-type]
        return resp.model_copy(update={"payload": payload})

    def _extract_data(self, resp: Response) -> Any:
        if self._mode == "provider_native" and resp.payload is not None:
            return resp.payload
        if self._mode == "extract_json":
            return self._extract_first_json(resp.text)
        # strict_json (default) or provider_native fallback
        try:
            return json.loads(resp.text)
        except json.JSONDecodeError as e:
            raise OutputValidationError(errors=[{"type": "json_decode", "msg": str(e)}]) from e

    def _extract_first_json(self, text: str) -> Any:
        """Pull the first JSON object/array from ``text``.

        Tries fenced triple-backtick json blocks first, then falls back
        to a bracket-counter scan that respects string + escape
        boundaries.
        """
        # Try fenced ```json (or ```) block first
        fenced_match = _FENCED_BLOCK_RE.search(text)
        if fenced_match:
            candidate = fenced_match.group(1).strip()
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                pass  # fall through to brace scan

        # Bracket-counter scan respecting strings + escapes
        for start_char, end_char in [("{", "}"), ("[", "]")]:
            start = text.find(start_char)
            if start == -1:
                continue
            depth = 0
            in_string = False
            escape = False
            for i in range(start, len(text)):
                c = text[i]
                if escape:
                    escape = False
                    continue
                if c == "\\":
                    escape = True
                    continue
                if c == '"':
                    in_string = not in_string
                    continue
                if in_string:
                    continue
                if c == start_char:
                    depth += 1
                elif c == end_char:
                    depth -= 1
                    if depth == 0:
                        candidate = text[start : i + 1]
                        try:
                            return json.loads(candidate)
                        except json.JSONDecodeError:
                            break

        raise OutputValidationError(
            errors=[{"type": "json_extract", "msg": "no parseable JSON found in response text"}]
        )
