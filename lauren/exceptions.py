"""lauren exception hierarchy — the 28-type Error Catalog.

All framework errors inherit from :class:`LaurenError`. Runtime errors that
reach the ASGI boundary are converted to structured JSON responses of the
form::

    {"error": {"code": "...", "message": "...", "detail": {...}}}
"""

from __future__ import annotations

from typing import Any


class LaurenError(Exception):
    """Base class for all lauren framework errors."""

    #: Stable machine-readable error code.
    code: str = "lauren_error"
    #: Suggested HTTP status for runtime errors. ``None`` for startup errors.
    status_code: int | None = None

    def __init__(
        self, message: str = "", *, detail: dict[str, Any] | None = None
    ) -> None:
        super().__init__(message)
        self.message = message
        self.detail: dict[str, Any] = detail or {}

    def to_payload(self) -> dict[str, Any]:
        """Return the canonical JSON payload for this error."""
        return {
            "error": {
                "code": self.code,
                "message": self.message,
                "detail": self.detail,
            }
        }


# ---------------------------------------------------------------------------
# Startup-time errors (not HTTP-mapped).
# ---------------------------------------------------------------------------


class StartupError(LaurenError):
    code = "startup_error"


class RouterConflictError(StartupError):
    """Two routes share the same (method, path) signature."""

    code = "router_conflict"


class CircularDependencyError(StartupError):
    """A cycle was detected in the DI graph."""

    code = "circular_dependency"


class CircularModuleError(StartupError):
    """A cycle was detected in the module import graph."""

    code = "circular_module"


class MissingProviderError(StartupError):
    """No provider is registered for a requested token."""

    code = "missing_provider"


class ProtocolAmbiguityError(StartupError):
    """A protocol has multiple providers but ``multi=False`` was requested."""

    code = "protocol_ambiguity"


class ModuleExportViolation(StartupError):
    """A module exports a provider it neither declares nor imports."""

    code = "module_export_violation"


class LifecycleConfigError(StartupError):
    code = "lifecycle_config"


class MetadataInheritanceError(StartupError):
    """Injectable metadata was inherited from a base class (forbidden)."""

    code = "metadata_inheritance"


class DuplicateBindingError(StartupError):
    code = "duplicate_binding"


class UnresolvableParameterError(StartupError):
    """A handler or provider parameter cannot be resolved to an extractor or dep."""

    code = "unresolvable_parameter"


class DIScopeViolationError(StartupError):
    """A singleton depends on a request-scoped provider (or similar)."""

    code = "di_scope_violation"


class MiddlewareConfigError(StartupError):
    code = "middleware_config"


class GuardConfigError(StartupError):
    code = "guard_config"


class InterceptorConfigError(StartupError):
    code = "interceptor_config"


class ExceptionHandlerConfigError(StartupError):
    """An ``@exception_handler`` / ``@use_exception_handlers`` was misused.

    Raised when:

    * ``@exception_handler`` is invoked with no exception types or with
      something that is not a ``BaseException`` subclass;
    * ``@exception_handler`` decorates a class without a ``catch`` method;
    * ``@use_exception_handlers`` references a class / function that was
      never decorated with ``@exception_handler``.
    """

    code = "exception_handler_config"


class OpenAPISchemaError(StartupError):
    code = "openapi_schema"


class DecoratorUsageError(StartupError):
    """A configurable decorator was used without parentheses.

    ``@controller``, ``@injectable``, ``@module``, and the HTTP verb
    decorators (``@get``, ``@post``, ...) must be invoked with arguments
    (or empty parentheses) so their configuration is always explicit::

        @controller("/pets")       # correct
        @controller()             # correct — defaults are fine
        @controller               # rejected — ambiguous
    """

    code = "decorator_usage"


# ---------------------------------------------------------------------------
# Runtime / HTTP-mapped errors.
# ---------------------------------------------------------------------------


class HTTPError(LaurenError):
    """Base class for errors mapped to HTTP responses."""

    status_code: int | None = 500
    code = "http_error"


class ExtractorError(HTTPError):
    status_code = 422
    code = "extractor_error"


class ExtractorFieldError(ExtractorError):
    code = "extractor_field_error"


class RouteNotFoundError(HTTPError):
    status_code = 404
    code = "route_not_found"


class MethodNotAllowedError(HTTPError):
    status_code = 405
    code = "method_not_allowed"

    def __init__(self, message: str = "", *, allow: list[str] | None = None) -> None:
        super().__init__(message, detail={"allow": allow or []})
        self.allow = allow or []


class RequestBodyTooLarge(HTTPError):
    status_code = 413
    code = "request_body_too_large"


class UnauthorizedError(HTTPError):
    status_code = 401
    code = "unauthorized"


class ForbiddenError(HTTPError):
    status_code = 403
    code = "forbidden"


class MissingStateError(HTTPError):
    status_code = 500
    code = "missing_state"


class StateTypeError(HTTPError):
    status_code = 500
    code = "state_type_error"


# ---------------------------------------------------------------------------
# Lifecycle runtime errors.
# ---------------------------------------------------------------------------


class LifecycleError(LaurenError):
    code = "lifecycle_error"


class LifecycleViolationError(LifecycleError):
    code = "lifecycle_violation"


class DestructError(LifecycleError):
    code = "destruct_error"


class DestructTimeoutError(LifecycleError):
    code = "destruct_timeout"


class DrainTimeoutError(LifecycleError):
    code = "drain_timeout"


__all__ = [
    "LaurenError",
    "StartupError",
    "HTTPError",
    "LifecycleError",
    "RouterConflictError",
    "CircularDependencyError",
    "CircularModuleError",
    "MissingProviderError",
    "ProtocolAmbiguityError",
    "ModuleExportViolation",
    "LifecycleConfigError",
    "MetadataInheritanceError",
    "DuplicateBindingError",
    "UnresolvableParameterError",
    "DIScopeViolationError",
    "MiddlewareConfigError",
    "GuardConfigError",
    "ExceptionHandlerConfigError",
    "OpenAPISchemaError",
    "ExtractorError",
    "ExtractorFieldError",
    "RouteNotFoundError",
    "MethodNotAllowedError",
    "RequestBodyTooLarge",
    "UnauthorizedError",
    "ForbiddenError",
    "MissingStateError",
    "StateTypeError",
    "LifecycleViolationError",
    "DestructError",
    "DestructTimeoutError",
    "DrainTimeoutError",
]
