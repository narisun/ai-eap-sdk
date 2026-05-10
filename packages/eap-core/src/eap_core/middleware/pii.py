"""PII masking middleware.

Default behavior uses regex patterns and an in-context vault for
re-identification. The Presidio path is enabled when `pii` extra is
installed and `engine="presidio"` is passed.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterator
from typing import Any, Literal

from eap_core.middleware.base import PassthroughMiddleware
from eap_core.types import Chunk, Context, Message, Request, Response

_DEFAULT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("EMAIL", re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")),
    ("SSN", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    ("PHONE", re.compile(r"\b\+?\d{1,3}[\s-]?\(?\d{3}\)?[\s-]?\d{3}[\s-]?\d{4}\b")),
    ("CREDIT_CARD", re.compile(r"\b\d{4}[\s-]?\d{4}[\s-]?\d{4}[\s-]?\d{4}\b")),
)


def _replace_in_text(
    text: str, vault: dict[str, str], patterns: tuple[tuple[str, re.Pattern[str]], ...]
) -> str:
    out = text
    for label, pat in patterns:

        def _sub(m: re.Match[str], _label: str = label) -> str:
            value = m.group(0)
            token = f"<{_label}_{uuid.uuid4().hex[:8]}>"
            vault[token] = value
            return token

        out = pat.sub(_sub, out)
    return out


def _content_iter(content: str | list[dict[str, object]]) -> Iterator[str]:  # pragma: no cover
    if isinstance(content, str):
        yield content
    else:
        for part in content:
            if isinstance(part, dict) and "text" in part:
                yield str(part["text"])


class PiiMaskingMiddleware(PassthroughMiddleware):
    name = "pii_masking"

    def __init__(
        self,
        engine: Literal["regex", "presidio"] = "regex",
        patterns: tuple[tuple[str, re.Pattern[str]], ...] | None = None,
    ) -> None:
        self._engine = engine
        self._patterns = patterns or _DEFAULT_PATTERNS
        self._presidio: Any = None
        if engine == "presidio":
            self._init_presidio()

    def _init_presidio(self) -> None:  # pragma: no cover  # exercised in extras tests
        try:
            from presidio_analyzer import AnalyzerEngine
            from presidio_anonymizer import AnonymizerEngine
        except ImportError as e:
            raise ImportError(
                "engine='presidio' requires the [pii] extra: pip install eap-core[pii]"
            ) from e
        self._presidio = (AnalyzerEngine(), AnonymizerEngine())  # type: ignore[no-untyped-call,unused-ignore]

    def _mask_text(self, text: str, vault: dict[str, str]) -> str:
        if self._engine == "regex":
            return _replace_in_text(text, vault, self._patterns)
        # Presidio path — exercised in tests/extras/test_pii_presidio.py
        analyzer, anonymizer = self._presidio  # pragma: no cover
        results = analyzer.analyze(text=text, language="en")  # pragma: no cover
        if not results:  # pragma: no cover
            return text
        from presidio_anonymizer.entities import OperatorConfig  # pragma: no cover

        resolved = anonymizer.anonymize(  # pragma: no cover
            text=text,
            analyzer_results=results,
            operators={"DEFAULT": OperatorConfig("replace", {"new_value": "<<PII>>"})},
        )
        out: str = resolved.text  # pragma: no cover
        for item in sorted(resolved.items, key=lambda i: i.start):  # pragma: no cover
            token = f"<{item.entity_type}_{uuid.uuid4().hex[:8]}>"
            original = text[item.start : item.end]
            vault[token] = original
            out = out.replace("<<PII>>", token, 1)
        return out  # pragma: no cover

    def _mask_message(self, msg: Message, vault: dict[str, str]) -> Message:
        if isinstance(msg.content, str):
            return msg.model_copy(update={"content": self._mask_text(msg.content, vault)})
        new_parts: list[dict[str, object]] = []
        for part in msg.content:
            if isinstance(part, dict) and "text" in part:
                new_parts.append({**part, "text": self._mask_text(part["text"], vault)})
            else:
                new_parts.append(part)
        return msg.model_copy(update={"content": new_parts})

    async def on_request(self, req: Request, ctx: Context) -> Request:
        new_msgs = [self._mask_message(m, ctx.vault) for m in req.messages]
        return req.model_copy(update={"messages": new_msgs})

    async def on_response(self, resp: Response, ctx: Context) -> Response:
        if not ctx.vault:
            return resp
        text = resp.text
        for token, original in ctx.vault.items():
            text = text.replace(token, original)
        return resp.model_copy(update={"text": text})

    async def on_stream_chunk(self, chunk: Chunk, ctx: Context) -> Chunk:
        if not ctx.vault:
            return chunk
        text = chunk.text
        for token, original in ctx.vault.items():
            text = text.replace(token, original)
        return chunk.model_copy(update={"text": text})
