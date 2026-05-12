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
from typing import Literal, Protocol, runtime_checkable

#: Allowed severity values for :class:`ThreatAssessment.severity`.
Severity = Literal["low", "medium", "high", "critical"]


@dataclass
class ThreatAssessment:
    """Structured result of assessing a piece of text for threats.

    Two independent axes describe the verdict:

    - **confidence** — how sure the detector is that a threat EXISTS.
      Calibration metric. A regex match on ``"reveal system prompt"``
      is high-confidence (the literal text was found). A model
      classifier flagging code-execution intent may be lower-confidence
      because soft-classification has irreducible uncertainty.

    - **severity** — how dangerous the threat WOULD BE if it is real.
      Impact metric. A jailbreak attempt to ``"reveal system prompt"``
      is high-severity (system-prompt extraction). A vague
      "ignore previous instructions" prefix is medium-severity (could
      be a probe, could be benign mis-phrasing).

    The two axes are not redundant — a regex match for "reveal system
    prompt" is high-confidence + high-severity; a model-output
    classifier flagging code-execution intent may be lower-confidence
    + critical-severity. Middleware decides on confidence (the
    ``min_confidence`` threshold); audit/alerting branches on
    severity (page on critical, log on medium).

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

    severity: Severity = "low"
    """How dangerous the threat would be IF real. Independent of
    ``confidence``. Defaults to ``"low"`` so a no-threat assessment
    is descriptively the lowest severity."""

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


#: Canonical prompt-injection regex set tagged with per-pattern severity.
#:
#: Single source of truth for both ``RegexThreatDetector`` (this module) and
#: ``eap_core.middleware.sanitize.ThreatDetectionMiddleware``. Closes H13 —
#: previously the tuple was duplicated in two places and could drift.
#:
#: Severities are assigned by impact-if-real:
#:
#: - ``"medium"`` for generic ignore-/disregard-previous probes (often
#:   benign-mis-phrasing, but possible injection prefixes).
#: - ``"high"`` for explicit jailbreak/system-prompt-extraction phrasing.
INJECTION_PATTERNS: tuple[tuple[str, re.Pattern[str], Severity], ...] = (
    (
        "ignore_previous",
        re.compile(r"ignore\s+(all\s+)?(previous|prior)\s+(instructions|directives)", re.I),
        "medium",
    ),
    (
        "disregard_previous",
        re.compile(r"disregard\s+(all\s+)?(previous|prior)", re.I),
        "medium",
    ),
    (
        "sys_override",
        re.compile(r"<<\s*sys\s*>>", re.I),
        "high",
    ),
    (
        "dan_jailbreak",
        re.compile(r"\byou\s+are\s+now\s+(dan|developer\s+mode)\b", re.I),
        "high",
    ),
    (
        "reveal_system_prompt",
        re.compile(r"reveal\s+(your\s+)?system\s+prompt", re.I),
        "high",
    ),
)


#: Ordering for max-severity selection. Index = severity rank.
_SEVERITY_ORDER: tuple[Severity, ...] = ("low", "medium", "high", "critical")


def _max_severity(severities: list[Severity]) -> Severity:
    """Return the highest severity from a list (by ``_SEVERITY_ORDER``)."""
    return max(severities, key=_SEVERITY_ORDER.index)


class RegexThreatDetector:
    """Default ``ThreatDetector`` using a small set of regex patterns.

    Mirrors the patterns in ``ThreatDetectionMiddleware`` but exposes
    a structured ``ThreatAssessment`` so callers (audit logs, custom
    middleware, dashboards) can branch on category and confidence.

    Each pattern in :data:`INJECTION_PATTERNS` carries a per-pattern
    severity (Finding 7 from the 2026-05-12 external review). When
    multiple patterns match, the assessment surfaces the *highest*
    severity across matches — operators see the worst-case impact
    rather than whatever happened to match first.
    """

    name: str = "regex"

    def __init__(self, patterns: tuple[re.Pattern[str], ...] | None = None) -> None:
        # ``patterns is None`` selects the canonical-table path in ``assess``
        # (full category + severity metadata). A non-None tuple selects the
        # bare-pattern fallback path (default severity, single category).
        self._patterns: tuple[re.Pattern[str], ...] | None = patterns

    async def assess(self, text: str) -> ThreatAssessment:
        # Walk the canonical pattern table so we get category + severity.
        # If the caller passed a custom ``patterns`` tuple at construction
        # time, fall back to bare-pattern matching with default severity.
        if self._patterns is None:
            matched_categories: list[str] = []
            matched_severities: list[Severity] = []
            for category, pattern, severity in INJECTION_PATTERNS:
                if pattern.search(text):
                    matched_categories.append(category)
                    matched_severities.append(severity)
            if not matched_categories:
                return ThreatAssessment(is_threat=False, confidence=0.0, severity="low")
            return ThreatAssessment(
                is_threat=True,
                confidence=0.9,  # regex matches are high-confidence
                severity=_max_severity(matched_severities),
                categories=["prompt_injection"],
                explanation=f"matched patterns: {', '.join(matched_categories)}",
            )

        # Custom pattern tuple — no severity metadata, so default to medium.
        for pat in self._patterns:
            if pat.search(text):
                return ThreatAssessment(
                    is_threat=True,
                    confidence=0.9,
                    severity="medium",
                    categories=["prompt_injection"],
                    explanation=f"matched pattern {pat.pattern!r}",
                )
        return ThreatAssessment(is_threat=False, confidence=0.0, severity="low")


__all__ = [
    "INJECTION_PATTERNS",
    "RegexThreatDetector",
    "Severity",
    "ThreatAssessment",
    "ThreatDetector",
]
