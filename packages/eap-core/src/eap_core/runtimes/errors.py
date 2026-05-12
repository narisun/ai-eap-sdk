"""Canonical runtime exception hierarchy.

Adapters MUST translate vendor exceptions into one of these types so
middleware ``on_error`` handlers can react uniformly regardless of
provider (Bedrock, Vertex, OpenAI, Anthropic, local).

Translators (e.g., ``BedrockRuntimeAdapter._map_botocore_error``,
``VertexRuntimeAdapter._map_google_error``) MUST preserve the original
vendor exception via ``__cause__`` (use ``raise NewError(...) from exc``)
so audit logs can inspect the underlying payload.
"""

from __future__ import annotations


class RuntimeAdapterError(Exception):
    """Base for all runtime adapter failures.

    Middleware can ``except RuntimeAdapterError`` to catch any
    provider-level failure without coupling to a specific vendor SDK.
    """


class RuntimeAuthError(RuntimeAdapterError):
    """Authentication or authorization failure with the provider.

    Vendor mappings include: botocore ``AccessDeniedException`` /
    ``UnauthorizedException``; google.api_core ``PermissionDenied``;
    HTTP 401 / 403.
    """


class RuntimeRateLimitError(RuntimeAdapterError):
    """Provider returned a rate-limit or quota error.

    Caller may retry with backoff. Vendor mappings include:
    botocore ``ThrottlingException`` / ``TooManyRequestsException``;
    google.api_core ``ResourceExhausted``; HTTP 429.
    """


class RuntimeTimeoutError(RuntimeAdapterError):
    """Request exceeded the provider's timeout window."""


class RuntimeServerError(RuntimeAdapterError):
    """Provider returned a 5xx / unavailable / transient server error.

    Caller may retry with backoff.
    """


class RuntimeContextLengthError(RuntimeAdapterError):
    """Request exceeded the model's context window.

    Caller should reduce prompt size; retry will not help.
    """


__all__ = [
    "RuntimeAdapterError",
    "RuntimeAuthError",
    "RuntimeContextLengthError",
    "RuntimeRateLimitError",
    "RuntimeServerError",
    "RuntimeTimeoutError",
]
