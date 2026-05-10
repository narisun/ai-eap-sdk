"""Threat detection abstraction.

EAP-Core's default ``PromptInjectionMiddleware`` is a regex
classifier — sufficient for many cases, weak for sophisticated
attacks. Cloud providers offer managed threat-detection services
(AWS Bedrock Guardrails, GCP Model Armor) that catch what regex
can't.

This module defines the vendor-neutral ``ThreatDetector`` Protocol.
Implementations:

- ``RegexThreatDetector`` (here) — same patterns as the default
  middleware, now exposed as a structured assessment.
- ``eap_core.integrations.agentcore.BedrockGuardrailsDetector`` —
  AWS Bedrock Guardrails (TBD).
- ``eap_core.integrations.vertex.ModelArmorDetector`` —
  GCP Model Armor.

The Protocol returns a ``ThreatAssessment`` with a categorized
verdict so middleware and audit consumers can branch on it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@dataclass
class ThreatAssessment:
    """Structured result of assessing a piece of text for threats.

    Categories are open-set strings; common values:

    - ``"prompt_injection"``
    - ``"jailbreak"``
    - ``"pii_exfiltration"``
    - ``"harmful_content"``
    - ``"unsafe_code"``
    """

    is_threat: bool
    confidence: float = 0.0
    """0.0 to 1.0 confidence that this is a real threat."""

    categories: list[str] = field(default_factory=list)
    """Categories matched. Empty if ``is_threat`` is False."""

    explanation: str = ""
    """Human-readable rationale (or empty)."""


@runtime_checkable
class ThreatDetector(Protocol):
    """Vendor-neutral threat detector.

    All detectors return a ``ThreatAssessment``. Implementations may
    be pure-Python (regex), call a local model, or hit a cloud-managed
    service (Model Armor, Bedrock Guardrails).
    """

    name: str

    async def assess(self, text: str) -> ThreatAssessment: ...


_DEFAULT_INJECTION_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"ignore\s+(all\s+)?(previous|prior)\s+(instructions|directives)", re.I),
    re.compile(r"disregard\s+(all\s+)?(previous|prior)", re.I),
    re.compile(r"<<\s*sys\s*>>", re.I),
    re.compile(r"\byou\s+are\s+now\s+(dan|developer\s+mode)\b", re.I),
    re.compile(r"reveal\s+(your\s+)?system\s+prompt", re.I),
)


class RegexThreatDetector:
    """Default ``ThreatDetector`` using a small set of regex patterns.

    Mirrors the patterns in ``PromptInjectionMiddleware`` but exposes
    a structured ``ThreatAssessment`` so callers (audit logs, custom
    middleware, dashboards) can branch on category and confidence.
    """

    name: str = "regex"

    def __init__(self, patterns: tuple[re.Pattern[str], ...] | None = None) -> None:
        self._patterns = patterns or _DEFAULT_INJECTION_PATTERNS

    async def assess(self, text: str) -> ThreatAssessment:
        for pat in self._patterns:
            if pat.search(text):
                return ThreatAssessment(
                    is_threat=True,
                    confidence=0.9,  # regex matches are high-confidence
                    categories=["prompt_injection"],
                    explanation=f"matched pattern {pat.pattern!r}",
                )
        return ThreatAssessment(is_threat=False, confidence=0.0)


__all__ = [
    "RegexThreatDetector",
    "ThreatAssessment",
    "ThreatDetector",
]
