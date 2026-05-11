"""Prompt-injection detection middleware.

Default detector is a small regex set covering common patterns,
imported from :mod:`eap_core.security` so that ``RegexThreatDetector``
and ``PromptInjectionMiddleware`` share one source of truth (H13).
Callers can plug in a more sophisticated classifier (LLM- or
model-based) via the ``extra_classifier`` argument.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable

from eap_core.exceptions import PromptInjectionError
from eap_core.middleware.base import PassthroughMiddleware
from eap_core.security import INJECTION_PATTERNS
from eap_core.types import Context, Message, Request


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
        patterns: tuple[tuple[str, re.Pattern[str]], ...] | None = None,
        extra_classifier: Callable[[str], Awaitable[bool]] | None = None,
    ) -> None:
        self._patterns: tuple[tuple[str, re.Pattern[str]], ...] = patterns or INJECTION_PATTERNS
        self._classifier = extra_classifier

    async def on_request(self, req: Request, ctx: Context) -> Request:
        for msg in req.messages:
            text = _content_text(msg)
            for _label, pat in self._patterns:
                if pat.search(text):
                    raise PromptInjectionError(matched=text, pattern=pat.pattern)
            if self._classifier is not None and await self._classifier(text):
                raise PromptInjectionError(matched=text, pattern="<classifier>")
        return req
