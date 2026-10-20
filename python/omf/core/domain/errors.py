"""Domain exceptions.

Every error raised across a port boundary is an :class:`OMFError`. Adapters translate
provider-specific failures (HTTP status codes, SDK exceptions, socket errors) into these
types so that application services and the API layer never see provider details.

``code`` is a stable, machine-readable identifier that is part of the public API
contract. ``retryable`` tells the routing engine whether another attempt *may* succeed;
it never authorises a retry on its own (see :mod:`omf.core.services.routing`).
"""

from __future__ import annotations

from typing import Any


class OMFError(Exception):
    code: str = "internal_error"
    retryable: bool = False

    def __init__(
        self,
        message: str,
        *,
        details: dict[str, Any] | None = None,
        retryable: bool | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}
        if retryable is not None:
            self.retryable = retryable

    def __str__(self) -> str:
        return self.message


# --- request / configuration problems -------------------------------------------------


class ValidationFailed(OMFError):
    code = "validation_failed"


class ConfigurationError(OMFError):
    code = "configuration_error"


class Conflict(OMFError):
    code = "conflict"


# --- lookups --------------------------------------------------------------------------


class NotFound(OMFError):
    code = "not_found"


class ProviderNotFound(NotFound):
    code = "provider_not_found"


class ModelNotFound(NotFound):
    code = "model_not_found"


class ConversationNotFound(NotFound):
    code = "conversation_not_found"


class ArtifactNotFound(NotFound):
    code = "artifact_not_found"


class BenchmarkNotFound(NotFound):
    code = "benchmark_not_found"


class ComputeEnvironmentNotFound(NotFound):
    code = "compute_environment_not_found"


class StorageProviderNotFound(NotFound):
    code = "storage_provider_not_found"


# --- capabilities ---------------------------------------------------------------------


class CapabilityNotSupported(OMFError):
    """A *model* does not declare a capability the request needs (e.g. vision input)."""

    code = "capability_not_supported"


class OperationNotSupported(OMFError):
    """An *adapter* does not implement an optional port operation (e.g. ``cancel``)."""

    code = "operation_not_supported"


# --- provider runtime failures --------------------------------------------------------


class ProviderError(OMFError):
    """The provider answered, but with an error that another attempt will not fix."""

    code = "provider_error"


class ProviderUnavailable(OMFError):
    """The provider could not be reached or reported itself as overloaded."""

    code = "provider_unavailable"
    retryable = True


class ProviderTimeout(OMFError):
    code = "provider_timeout"
    retryable = True


class ProviderAuthenticationFailed(OMFError):
    code = "provider_authentication_failed"


class ProviderDisabled(OMFError):
    code = "provider_disabled"


class RequestCancelled(OMFError):
    code = "request_cancelled"


# --- routing / policy -----------------------------------------------------------------


class NoRouteAvailable(OMFError):
    code = "no_route_available"


class PolicyViolation(OMFError):
    """The request would break a data-handling policy (e.g. sensitive prompt -> external)."""

    code = "policy_violation"


class EgressDenied(OMFError):
    """A configured endpoint points to a destination the egress policy forbids (SSRF)."""

    code = "egress_denied"


# --- storage --------------------------------------------------------------------------


class ChecksumMismatch(OMFError):
    code = "checksum_mismatch"


class StorageError(OMFError):
    code = "storage_error"


# --- security -------------------------------------------------------------------------


class AuthenticationRequired(OMFError):
    code = "authentication_required"


class PermissionDenied(OMFError):
    code = "permission_denied"


class RateLimited(OMFError):
    code = "rate_limited"
    retryable = True


class LifecycleDisabled(PermissionDenied):
    """The adapter could change the environment, but the operator has not allowed it."""

    code = "lifecycle_disabled"


class SecretNotFound(OMFError):
    code = "secret_not_found"


class ContextLengthExceeded(ValidationFailed):
    code = "context_length_exceeded"


class ModelUnavailable(OMFError):
    """The model is declared for a provider but the provider does not currently serve it."""

    code = "model_unavailable"


class ComputeError(OMFError):
    """A compute backend (Docker, Kubernetes, cloud API) rejected or failed an operation."""

    code = "compute_error"
