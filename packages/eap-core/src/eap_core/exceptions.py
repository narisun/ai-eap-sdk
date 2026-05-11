"""EAP-Core exception hierarchy."""

from __future__ import annotations

import hashlib


class EapError(Exception):
    """Base for all eap-core exceptions."""


class PromptInjectionError(EapError):
    """Raised when prompt-injection patterns match in user input.

    Carries a short SHA-256 prefix of the matched text (``matched_hash``)
    plus the regex ``pattern`` that fired. The raw matched text is **not**
    stored on the exception — that landed in spans / trajectories / logs
    that may not be PII-scrubbed downstream (H7). The hash is enough for
    audit correlation without leaking user content.
    """

    def __init__(self, *, matched: str, pattern: str) -> None:
        self.matched_hash = hashlib.sha256(matched.encode("utf-8")).hexdigest()[:16]
        self.pattern = pattern
        super().__init__(
            f"prompt-injection: pattern {pattern!r} matched (hash {self.matched_hash})"
        )


class PolicyDeniedError(EapError):
    def __init__(self, rule_id: str, reason: str) -> None:
        super().__init__(f"{rule_id}: {reason}")
        self.rule_id = rule_id
        self.reason = reason


class PolicyConfigurationError(EapError):
    """PolicyMiddleware invoked without the required trusted-metadata
    slots (``ctx.metadata['policy.action']`` / ``['policy.resource']``).

    Indicates a programming error in pipeline wiring — not a policy
    decision against a caller. Distinct from ``PolicyDeniedError``
    (which signals a policy outcome) and from generic Python
    ``RuntimeError`` (so operators using ``except EapError:`` as the
    SDK boundary catch this consistently).
    """


class OutputValidationError(EapError):
    def __init__(self, errors: list[dict[str, object]]) -> None:
        super().__init__(f"Output failed schema validation: {errors}")
        self.errors = errors


class RuntimeAdapterError(EapError):
    """Adapter could not satisfy the request."""


class IdentityError(EapError):
    """Token exchange or identity verification failed."""


class RealRuntimeDisabledError(EapError):
    """Raised by cloud-runtime adapters and integrations when called
    without ``EAP_ENABLE_REAL_RUNTIMES=1``.

    Distinguishes "configuration not enabled" from
    ``NotImplementedError`` (which Python uses for "subclass forgot to
    implement"). Tools, debuggers, and policy decisions can ``except
    RealRuntimeDisabledError`` selectively without intercepting genuine
    implementation gaps.
    """
