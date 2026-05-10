"""EAP-Core exception hierarchy."""


class EapError(Exception):
    """Base for all eap-core exceptions."""


class PromptInjectionError(EapError):
    def __init__(self, reason: str, matched: str | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.matched = matched


class PolicyDeniedError(EapError):
    def __init__(self, rule_id: str, reason: str) -> None:
        super().__init__(f"{rule_id}: {reason}")
        self.rule_id = rule_id
        self.reason = reason


class OutputValidationError(EapError):
    def __init__(self, errors: list[dict]) -> None:
        super().__init__(f"Output failed schema validation: {errors}")
        self.errors = errors


class RuntimeAdapterError(EapError):
    """Adapter could not satisfy the request."""


class IdentityError(EapError):
    """Token exchange or identity verification failed."""
