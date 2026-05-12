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

# Upper bound on bracket-scan retries per start_char in extract_json
# (v1.8.1 M1). When a candidate fails json.loads, the scanner advances
# past it and tries again — this cap prevents pathological inputs from
# DoS'ing the scanner. 32 is comfortably above any realistic LLM output
# (which rarely contains more than a handful of brace tokens before the
# actual JSON), but tight enough that a malicious input can't burn CPU.
_MAX_CANDIDATE_ATTEMPTS = 32


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
        boundaries. When a candidate fails ``json.loads``, the scanner
        advances past the failed candidate's closing bracket and retries
        — so a failed fenced block (containing garbage) or earlier
        prose-embedded ``{...}`` token doesn't shadow valid JSON
        further along (v1.8.1 M1). The retry loop is bounded at
        ``_MAX_CANDIDATE_ATTEMPTS`` per bracket pair to prevent
        pathological-input DoS.
        """
        # Try fenced ```json (or ```) block first
        fenced_match = _FENCED_BLOCK_RE.search(text)
        if fenced_match:
            candidate = fenced_match.group(1).strip()
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                pass  # fall through to brace scan

        # Bracket-counter scan that retries past failed candidates.
        for start_char, end_char in [("{", "}"), ("[", "]")]:
            search_from = 0
            for _ in range(_MAX_CANDIDATE_ATTEMPTS):
                start = text.find(start_char, search_from)
                if start == -1:
                    break
                depth = 0
                in_string = False
                escape = False
                candidate_end = -1
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
                            candidate_end = i
                            break
                if candidate_end == -1:
                    # Unmatched bracket from this start — no further
                    # candidate for this start_char will parse either
                    # (they'd be nested inside this unmatched run).
                    break
                try:
                    return json.loads(text[start : candidate_end + 1])
                except json.JSONDecodeError:
                    # Advance past this failed candidate and retry.
                    search_from = candidate_end + 1
                    continue

        raise OutputValidationError(
            errors=[{"type": "json_extract", "msg": "no parseable JSON found in response text"}]
        )
