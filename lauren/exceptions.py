"""lauren exception hierarchy — the closed error catalog.

The canonical, generated table of every class (base, HTTP status, ``code``)
lives in ``docs/reference/errors.md``.

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

    def __init__(self, message: str = "", *, detail: dict[str, Any] | None = None) -> None:
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
    """Base class for configuration errors raised by ``LaurenFactory.create`` (never HTTP-mapped)."""

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
    """``@post_construct`` / ``@pre_destruct`` decorates something other than a method."""

    code = "lifecycle_config"


class MetadataInheritanceError(StartupError):
    """Injectable metadata was inherited from a base class (forbidden)."""

    code = "metadata_inheritance"


class DuplicateBindingError(StartupError):
    """The same class or token is registered twice in one container."""

    code = "duplicate_binding"


class UnresolvableParameterError(StartupError):
    """A handler or provider parameter cannot be resolved to an extractor or dep."""

    code = "unresolvable_parameter"


class DIScopeViolationError(StartupError):
    """A singleton depends on a request-scoped provider (or similar)."""

    code = "di_scope_violation"


class MiddlewareConfigError(StartupError):
    """A middleware lacks ``dispatch(request, call_next)``, or ``@use_middlewares`` cannot run where placed."""

    code = "middleware_config"


class GuardConfigError(StartupError):
    """A guard lacks ``can_activate(context)``, or ``@use_guards`` cannot run where placed."""

    code = "guard_config"


class InterceptorConfigError(StartupError):
    """An interceptor lacks ``intercept(ctx, call_handler)``, or ``@use_interceptors`` cannot run where placed."""

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


class SessionConfigError(StartupError):
    """A :class:`~lauren.SessionConfig` was invalid, or a ``Session`` was
    injected into a handler in an app that did not enable sessions.

    Raised at startup (inside ``LaurenFactory.create``) when:

    * ``same_site="none"`` is combined with ``secure=False`` (browsers
      reject such cookies);
    * a ``__Host-`` / ``__Secure-`` cookie-name prefix is used without the
      matching ``secure`` / ``path`` / ``domain`` constraints;
    * the chosen store requires a signing secret but none was supplied;
    * ``max_age`` / ``idle_timeout`` is non-positive;
    * a handler declares a ``session: Session`` parameter but
      ``sessions=SessionConfig(...)`` was never passed to the factory.

    Joins the existing ``*ConfigError`` family (``MiddlewareConfigError``,
    ``GuardConfigError``, ``InterceptorConfigError``,
    ``ExceptionHandlerConfigError``).
    """

    code = "session_config"


class OpenAPISchemaError(StartupError):
    """Reserved for strict OpenAPI generation — not raised yet.

    A schema that cannot be generated currently degrades to a generic
    ``{"type": "object"}`` entry instead of failing startup.
    """

    code = "openapi_schema"


class DecoratorUsageError(StartupError):
    """A decorator or provider helper was used incorrectly — most often without parentheses.

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
    """A request value could not be extracted or validated."""

    status_code = 422
    code = "extractor_error"


class ExtractorFieldError(ExtractorError):
    """One parameter is missing or violates a field constraint."""

    code = "extractor_field_error"


class RouteNotFoundError(HTTPError):
    """No route matches the request path."""

    status_code = 404
    code = "route_not_found"


class MethodNotAllowedError(HTTPError):
    """The path matches, but not for this HTTP method (the response sets ``Allow``)."""

    status_code = 405
    code = "method_not_allowed"

    def __init__(self, message: str = "", *, allow: list[str] | None = None) -> None:
        super().__init__(message, detail={"allow": allow or []})
        self.allow = allow or []


class RequestBodyTooLarge(HTTPError):
    """The request body exceeds ``max_body_size``."""

    status_code = 413
    code = "request_body_too_large"


class UnsupportedMediaTypeError(HTTPError):
    """The request body's ``Content-Type`` is not one the route accepts.

    Raised before the body is read when
    ``ContentConfig(strict_content_type=True)`` is in effect and a JSON body
    parameter receives a non-JSON (or missing) ``Content-Type``.
    """

    status_code = 415
    code = "unsupported_media_type"


class UnauthorizedError(HTTPError):
    """Authentication is missing or invalid."""

    status_code = 401
    code = "unauthorized"


class ForbiddenError(HTTPError):
    """The caller is not allowed — also what a guard returning ``False`` raises."""

    status_code = 403
    code = "forbidden"


class MissingStateError(HTTPError):
    """``request.state.require(key, T)`` found no value for ``key``."""

    status_code = 500
    code = "missing_state"


class StateTypeError(HTTPError):
    """A ``request.state`` value has the wrong type for ``get_typed`` / ``require``."""

    status_code = 500
    code = "state_type_error"


# ---------------------------------------------------------------------------
# Lifecycle runtime errors.
# ---------------------------------------------------------------------------


class LifecycleError(LaurenError):
    """Base class for errors around startup, shutdown and lifecycle hooks."""

    code = "lifecycle_error"


class LifecycleViolationError(LifecycleError):
    """A lifecycle operation was used in the wrong state (``startup()`` twice, a mutator after compile)."""

    code = "lifecycle_violation"


class DestructError(LifecycleError):
    """A ``@pre_destruct`` hook raised during shutdown (collected; shutdown continues)."""

    code = "destruct_error"


class DestructTimeoutError(LifecycleError):
    """A ``@pre_destruct`` hook exceeded its timeout (collected; shutdown continues)."""

    code = "destruct_timeout"


class DrainTimeoutError(LifecycleError):
    """Reserved: not raised — a drain timeout at shutdown is logged as a warning."""

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
    "InterceptorConfigError",
    "ExceptionHandlerConfigError",
    "SessionConfigError",
    "OpenAPISchemaError",
    "DecoratorUsageError",
    "ExtractorError",
    "ExtractorFieldError",
    "RouteNotFoundError",
    "MethodNotAllowedError",
    "RequestBodyTooLarge",
    "UnsupportedMediaTypeError",
    "UnauthorizedError",
    "ForbiddenError",
    "MissingStateError",
    "StateTypeError",
    "LifecycleViolationError",
    "DestructError",
    "DestructTimeoutError",
    "DrainTimeoutError",
]
