"""Prompt-injection detection middleware.

Default detector is a small regex set covering common patterns. Callers
can plug in a more sophisticated classifier (LLM- or model-based) via
the `extra_classifier` argument.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable

from eap_core.exceptions import PromptInjectionError
from eap_core.middleware.base import PassthroughMiddleware
from eap_core.types import Context, Message, Request

_DEFAULT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"ignore\s+(all\s+)?(previous|prior)\s+(instructions|directives)", re.I),
    re.compile(r"disregard\s+(all\s+)?(previous|prior)", re.I),
    re.compile(r"<<\s*sys\s*>>", re.I),
    re.compile(r"\byou\s+are\s+now\s+(dan|developer\s+mode)\b", re.I),
    re.compile(r"reveal\s+(your\s+)?system\s+prompt", re.I),
)


def _content_text(msg: Message) -> str:
    return (
        msg.content
        if isinstance(msg.content, str)
        else " ".join(p.get("text", "") for p in msg.content if isinstance(p, dict))
    )


class PromptInjectionMiddleware(PassthroughMiddleware):
    name = "prompt_injection"

    def __init__(
        self,
        patterns: tuple[re.Pattern[str], ...] | None = None,
        extra_classifier: Callable[[str], Awaitable[bool]] | None = None,
    ) -> None:
        self._patterns = patterns or _DEFAULT_PATTERNS
        self._classifier = extra_classifier

    async def on_request(self, req: Request, ctx: Context) -> Request:
        for msg in req.messages:
            text = _content_text(msg)
            for pat in self._patterns:
                if pat.search(text):
                    raise PromptInjectionError(
                        reason=f"matched pattern {pat.pattern!r}", matched=text[:200]
                    )
            if self._classifier is not None and await self._classifier(text):
                raise PromptInjectionError(reason="classifier flagged input", matched=text[:200])
        return req
