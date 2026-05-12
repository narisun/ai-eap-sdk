"""Threat-detection middleware (formerly prompt-injection middleware).

The canonical middleware is :class:`ThreatDetectionMiddleware`, which
accepts any :class:`~eap_core.security.ThreatDetector` (regex,
classifier, managed cloud service). It defaults to
:class:`~eap_core.security.RegexThreatDetector` for parity with the
legacy :class:`PromptInjectionMiddleware`, kept as a backward-compat
alias through the v1.x line.
"""

from __future__ import annotations

import re
import warnings
from collections.abc import Awaitable, Callable
from typing import Any

from eap_core.exceptions import PromptInjectionError
from eap_core.middleware.base import PassthroughMiddleware
from eap_core.security import INJECTION_PATTERNS, RegexThreatDetector, ThreatDetector
from eap_core.types import Context, Message, Request


def _content_text(msg: Message) -> str:
    return (
        msg.content
        if isinstance(msg.content, str)
        else " ".join(p.get("text", "") for p in msg.content if isinstance(p, dict))
    )


class _ClassifierThreatDetector:
    """Adapter wrapping a regex pattern tuple + optional async classifier
    into the :class:`ThreatDetector` Protocol.

    Used internally by :class:`PromptInjectionMiddleware` to preserve
    the legacy ``patterns`` / ``extra_classifier`` constructor contract
    while routing all detection through the new Protocol-based path.
    """

    name: str = "regex+classifier"

    def __init__(
        self,
        patterns: tuple[tuple[str, re.Pattern[str], str], ...] | None = None,
        extra_classifier: Callable[[str], Awaitable[bool]] | None = None,
    ) -> None:
        # Build a RegexThreatDetector from the canonical-shaped tuple.
        self._patterns = patterns or INJECTION_PATTERNS
        self._classifier = extra_classifier

    async def assess(self, text: str) -> ThreatAssessmentLike:
        from eap_core.security import ThreatAssessment, _max_severity  # local: avoid cycles

        matched_categories: list[str] = []
        matched_severities: list[str] = []
        for category, pattern, severity in self._patterns:
            if pattern.search(text):
                matched_categories.append(category)
                matched_severities.append(severity)
        if matched_categories:
            return ThreatAssessment(
                is_threat=True,
                confidence=0.9,
                severity=_max_severity(matched_severities),  # type: ignore[arg-type]
                categories=["prompt_injection"],
                explanation=f"matched patterns: {', '.join(matched_categories)}",
            )
        if self._classifier is not None and await self._classifier(text):
            return ThreatAssessment(
                is_threat=True,
                confidence=0.9,
                severity="medium",
                categories=["prompt_injection"],
                explanation="classifier flagged",
            )
        return ThreatAssessment(is_threat=False, confidence=0.0, severity="low")


# Type-only forward reference for the adapter's return annotation.
ThreatAssessmentLike = Any


class ThreatDetectionMiddleware(PassthroughMiddleware):
    """Pluggable threat-detector middleware.

    Accepts any :class:`~eap_core.security.ThreatDetector` (regex,
    classifier, managed cloud service such as AWS Bedrock Guardrails
    or GCP Model Armor). Default detector is
    :class:`~eap_core.security.RegexThreatDetector` for parity with
    the deprecated :class:`PromptInjectionMiddleware`.

    Parameters
    ----------
    detector:
        Detector implementation. ``None`` (default) installs a
        :class:`RegexThreatDetector` over the canonical
        :data:`~eap_core.security.INJECTION_PATTERNS` set.
    min_confidence:
        Minimum detector confidence (0.0-1.0) required to count as a
        block-worthy threat. Default ``0.7``. Tune lower for higher
        recall (more false positives), higher for higher precision.
    block:
        If ``True`` (default), raise :class:`PromptInjectionError` on a
        confident threat. If ``False``, only stash the assessment on
        ``ctx.metadata['threat.assessment']`` for downstream consumption.
    """

    name: str = "threat_detection"

    def __init__(
        self,
        detector: ThreatDetector | None = None,
        min_confidence: float = 0.7,
        block: bool = True,
    ) -> None:
        self._detector: ThreatDetector = detector or RegexThreatDetector()
        self._min_confidence = min_confidence
        self._block = block

    async def on_request(self, req: Request, ctx: Context) -> Request:
        for msg in req.messages:
            text = _content_text(msg)
            assessment = await self._detector.assess(text)
            if assessment.is_threat and assessment.confidence >= self._min_confidence:
                # Stash on ctx.metadata so audit / downstream middleware can
                # inspect the verdict even when ``block=False``.
                ctx.metadata["threat.assessment"] = {
                    "is_threat": assessment.is_threat,
                    "confidence": assessment.confidence,
                    "severity": assessment.severity,
                    "categories": list(assessment.categories),
                    "explanation": assessment.explanation,
                }
                if self._block:
                    raise PromptInjectionError(
                        matched=text,
                        pattern=(
                            f"threat:{','.join(assessment.categories) or 'unknown'}"
                            f":severity={assessment.severity}"
                        ),
                    )
        return req


class PromptInjectionMiddleware(ThreatDetectionMiddleware):
    """Deprecated alias for :class:`ThreatDetectionMiddleware`.

    .. deprecated:: 1.7
        Will be removed in v2.0. Use :class:`ThreatDetectionMiddleware`
        directly for the same behavior, or pass a custom
        :class:`~eap_core.security.ThreatDetector` to override the
        regex defaults.

    Preserves the legacy ``patterns`` and ``extra_classifier`` keyword
    arguments so existing call sites keep compiling and behaving
    identically. Emits a :class:`DeprecationWarning` on instantiation.
    """

    name: str = "prompt_injection"  # preserve old name for audit logs

    def __init__(
        self,
        patterns: tuple[tuple[str, re.Pattern[str], str], ...] | None = None,
        extra_classifier: Callable[[str], Awaitable[bool]] | None = None,
        *,
        detector: ThreatDetector | None = None,
        min_confidence: float = 0.7,
        block: bool = True,
    ) -> None:
        warnings.warn(
            "PromptInjectionMiddleware is deprecated and will be removed in v2.0. "
            "Use ThreatDetectionMiddleware instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        # Preserve legacy semantics: if patterns or extra_classifier are
        # passed (or no detector is supplied), build a classifier-aware
        # adapter that satisfies the ThreatDetector Protocol.
        if detector is None:
            detector = _ClassifierThreatDetector(
                patterns=patterns,
                extra_classifier=extra_classifier,
            )
        super().__init__(
            detector=detector,
            min_confidence=min_confidence,
            block=block,
        )
        # Surface ``_patterns`` for callers (and tests) that introspected
        # the previous attribute. Resolve to the canonical 3-tuple shape.
        self._patterns: tuple[tuple[str, re.Pattern[str], str], ...] = (
            patterns or INJECTION_PATTERNS
        )
