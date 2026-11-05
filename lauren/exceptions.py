"""lauren exception hierarchy — the closed error catalog.

The canonical, generated table of every class (base, HTTP status, ``code``)
lives in ``docs/reference/errors.md``; :func:`lauren.reflect.error_catalog`
returns the same catalog as data.

All framework errors inherit from :class:`LaurenError`. Runtime errors that
reach the ASGI boundary are converted to structured JSON responses of the
form::

    {"error": {"code": "...", "message": "...", "detail": {...}}}

Any error may carry response ``headers``; ``UnauthorizedError(challenge=...)``,
``MethodNotAllowedError(allow=...)`` and the ``retry_after=`` argument of
``TooManyRequestsError`` / ``ServiceUnavailableError`` / ``RequestTimeoutError``
build the headers HTTP requires on those statuses.

This module imports only the standard library: ``scripts/sync_doc_facts.py``
loads it without importing the ``lauren`` package.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from typing import Any, Self

#: Header names the framework owns on every error envelope.
_RESERVED_ERROR_HEADERS: frozenset[str] = frozenset({"content-type", "content-length"})
#: Characters that may never appear in a header name or value (response splitting).
_FORBIDDEN_HEADER_CHARS: tuple[str, ...] = ("\r", "\n", "\x00")


def _checked_header(name: object, value: object) -> tuple[str, str]:
    """Validate one ``(name, value)`` pair and return it with a lower-cased name."""
    if not isinstance(name, str) or not isinstance(value, str):
        raise TypeError(f"error header names and values must be str, got {name!r}: {value!r}")
    if not name or any(ch in name for ch in (*_FORBIDDEN_HEADER_CHARS, " ", "\t", ":")):
        raise ValueError(f"invalid header name {name!r}")
    if any(ch in value for ch in _FORBIDDEN_HEADER_CHARS):
        raise ValueError(f"header {name!r} contains a CR, LF or NUL character (response splitting)")
    try:
        value.encode("latin-1")
    except UnicodeEncodeError:
        raise ValueError(f"header {name!r} value is not latin-1 encodable: {value!r}") from None
    lowered = name.lower()
    if lowered in _RESERVED_ERROR_HEADERS:
        raise ValueError(f"{lowered!r} is set by the framework on error responses and cannot be overridden")
    return lowered, value


def _normalize_headers(
    headers: Mapping[str, str] | Iterable[tuple[str, str]] | None,
) -> tuple[tuple[str, str], ...]:
    """Validate ``headers`` and return them as ``(lower-cased name, value)`` pairs.

    A mapping gives one header per key; an iterable of pairs may repeat a name
    (two ``WWW-Authenticate`` challenges, a ``Set-Cookie`` per cookie).
    """
    if headers is None:
        return ()
    items: Iterable[Any] = headers.items() if isinstance(headers, Mapping) else headers
    if isinstance(items, (str, bytes)):
        raise TypeError("headers must be a mapping or an iterable of (name, value) pairs")
    out: list[tuple[str, str]] = []
    for item in items:
        if not isinstance(item, tuple) or len(item) != 2:
            raise TypeError(f"headers must be a mapping or an iterable of (name, value) pairs, got {item!r}")
        out.append(_checked_header(item[0], item[1]))
    return tuple(out)


def _format_retry_after(value: int | float | timedelta | datetime) -> str:
    """Render a ``Retry-After`` value (RFC 9110 §10.2.3).

    A number of seconds or a :class:`~datetime.timedelta` becomes
    ``delay-seconds`` (rounded up to a whole second); a timezone-aware
    :class:`~datetime.datetime` becomes an IMF-fixdate such as
    ``Fri, 23 Oct 2026 18:00:00 GMT``. Naive datetimes and negative delays
    raise :class:`ValueError`.
    """
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("retry_after datetime must be timezone-aware (e.g. tzinfo=timezone.utc)")
        return format_datetime(value.astimezone(timezone.utc), usegmt=True)
    if isinstance(value, timedelta):
        seconds: float = value.total_seconds()
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        seconds = value
    else:
        raise TypeError(
            f"retry_after must be seconds (int/float), a timedelta or an aware datetime, got {type(value).__name__}"
        )
    if not math.isfinite(seconds) or seconds < 0:
        raise ValueError(f"retry_after must be a finite, non-negative delay, got {value!r}")
    return str(math.ceil(seconds))


class LaurenError(Exception):
    """Base class for all lauren framework errors."""

    #: Stable machine-readable error code.
    code: str = "lauren_error"
    #: Suggested HTTP status for runtime errors. ``None`` for startup errors.
    status_code: int | None = None
    #: Response headers sent with the error envelope, as ``(name, value)``
    #: pairs with lower-cased names. Validated when the error is constructed.
    headers: tuple[tuple[str, str], ...] = ()

    def __init__(
        self,
        message: str = "",
        *,
        detail: dict[str, Any] | None = None,
        headers: Mapping[str, str] | Iterable[tuple[str, str]] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.detail: dict[str, Any] = detail or {}
        self.headers = _normalize_headers(headers)

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
    """A lifecycle-bound declaration is misused (a hook on a non-method, a hosted service or job on a non-singleton, …)."""

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


class ConfigValidationError(StartupError):
    """One or more settings classes failed to load or validate."""

    code = "config_validation"


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
    """Base class for errors mapped to HTTP responses.

    Subclass it (or one of the built-in leaves such as :class:`NotFoundError`)
    to mint a stable domain ``code``. ``status_code=`` and ``code=`` passed to
    the constructor override the class attributes on that one instance — an
    escape hatch for one-offs and for library code that maps foreign errors.
    ``problem_type`` / ``problem_title`` set the RFC 9457 ``type`` and
    ``title`` members when the app uses a problem-details ``error_format``.
    """

    status_code: int | None = 500
    code = "http_error"
    #: RFC 9457 ``type`` URI for this error class (problem-details formats only).
    problem_type: str | None = None
    #: RFC 9457 ``title`` for this error class; defaults to the status phrase.
    problem_title: str | None = None

    def __init__(
        self,
        message: str = "",
        *,
        detail: dict[str, Any] | None = None,
        headers: Mapping[str, str] | Iterable[tuple[str, str]] | None = None,
        status_code: int | None = None,
        code: str | None = None,
    ) -> None:
        super().__init__(message, detail=detail, headers=headers)
        if status_code is not None:
            if (
                isinstance(status_code, bool)
                or not isinstance(status_code, int)
                or not 400 <= status_code <= 599
            ):
                raise ValueError(f"status_code must be an int between 400 and 599, got {status_code!r}")
            self.status_code = status_code
        if code is not None:
            if not isinstance(code, str) or not code:
                raise ValueError(f"code must be a non-empty string, got {code!r}")
            self.code = code

    def _add_headers(self, extra: Iterable[tuple[str, str]]) -> None:
        """Prepend status-specific headers built from typed keyword arguments."""
        self.headers = _normalize_headers(tuple(extra)) + self.headers


def _issue_dict(issue: Any, include_input: bool) -> dict[str, Any]:
    """One validation issue as its JSON dict (``ValidationIssue`` or a mapping)."""
    to_dict = getattr(issue, "to_dict", None)
    if callable(to_dict):
        return dict(to_dict(include_input=include_input))
    out = {str(k): v for k, v in dict(issue).items() if include_input or k != "input"}
    if "loc" in out:
        out["loc"] = list(out["loc"]) if isinstance(out["loc"], (list, tuple)) else [out["loc"]]
    return out


def _issue_text(entry: Mapping[str, Any]) -> str:
    """``"query.page: Input should be ..."`` — the legacy ``detail.errors`` string."""
    loc = entry.get("loc") or ()
    where = ".".join(str(part) for part in loc) if loc else "<root>"
    return f"{where}: {entry.get('msg', '')}"


#: ``loc`` roots followed by a parameter (wire) name.
_PARAMETER_LOC_ROOTS = frozenset({"path", "query", "header", "cookie", "form"})


def _issues_detail(
    issues: Iterable[Any], include_input: bool
) -> tuple[list[Any], list[dict[str, Any]], list[str]]:
    items = list(issues)
    dicts = [_issue_dict(issue, include_input) for issue in items]
    return items, dicts, [_issue_text(d) for d in dicts]


class ExtractorError(HTTPError):
    """A request value could not be extracted or validated."""

    status_code = 422
    code = "extractor_error"
    #: The structured validation issues behind this error (``ValidationIssue``
    #: objects), mirrored as JSON dicts in ``detail["issues"]``.
    issues: tuple[Any, ...] = ()

    @classmethod
    def from_issues(
        cls,
        issues: Iterable[Any],
        *,
        field: str | None = None,
        message: str | None = None,
        include_input: bool = False,
        detail: Mapping[str, Any] | None = None,
    ) -> Self:
        """Build a 422 from validation issues (``ValidationIssue`` objects or ``{loc, type, msg}`` dicts).

        ``detail["issues"]`` holds the issues as ``{"loc", "type", "msg", "ctx"?}``
        dicts (``"input"`` only with ``include_input``). The 1.x keys stay
        alongside: ``detail["field"]`` names the first failing parameter and
        ``detail["errors"]`` lists ``"loc: msg"`` strings. The message is the
        single issue's text, or ``"N validation errors"``.
        """
        items, dicts, texts = _issues_detail(issues, include_input)
        if message is None:
            message = texts[0] if len(texts) == 1 else f"{len(texts)} validation errors"
        if field is None and dicts:
            loc = dicts[0].get("loc") or []
            if len(loc) > 1 and loc[0] in _PARAMETER_LOC_ROOTS:
                field = str(loc[1])
            elif loc:
                field = str(loc[0])
        full: dict[str, Any] = {"field": field, "errors": texts, "issues": dicts}
        if detail:
            full.update(detail)
        err = cls(message, detail=full)
        err.issues = tuple(items)
        return err


class ExtractorFieldError(ExtractorError):
    """One parameter is missing or violates a field constraint."""

    code = "extractor_field_error"


class BadRequestError(HTTPError):
    """The request is malformed in a way that is not schema validation.

    RFC 9110 §15.5.1. Use it for unparsable input (broken JSON or UTF-8, a
    tampered cursor); field-level validation failures are 422
    :class:`ExtractorError` responses.
    """

    status_code = 400
    code = "bad_request"


class NotFoundError(HTTPError):
    """The requested resource does not exist — the domain 404.

    RFC 9110 §15.5.5. Subclass it for domain codes
    (``class UserNotFound(NotFoundError): code = "user_not_found"``). Routing
    misses raise its subclass :class:`RouteNotFoundError`.
    """

    status_code = 404
    code = "not_found"


class RouteNotFoundError(NotFoundError):
    """No route matches the request path."""

    status_code = 404
    code = "route_not_found"


class MethodNotAllowedError(HTTPError):
    """The path matches, but not for this HTTP method; ``allow=`` sets the ``Allow`` header.

    RFC 9110 §15.5.6 requires ``Allow`` on every 405. The header is emitted
    wherever the error is raised — by the router or by a handler.
    """

    status_code = 405
    code = "method_not_allowed"

    def __init__(
        self,
        message: str = "",
        *,
        allow: Iterable[str] | None = None,
        detail: dict[str, Any] | None = None,
        headers: Mapping[str, str] | Iterable[tuple[str, str]] | None = None,
        status_code: int | None = None,
        code: str | None = None,
    ) -> None:
        allowed = [] if allow is None else list(allow)
        merged: dict[str, Any] = {"allow": allowed}
        merged.update(detail or {})
        super().__init__(message, detail=merged, headers=headers, status_code=status_code, code=code)
        self.allow = allowed
        if allowed:
            self._add_headers([("allow", ", ".join(allowed))])


class NotAcceptableError(HTTPError):
    """No available representation matches the request's ``Accept`` headers.

    RFC 9110 §15.5.7. Raised by content negotiation that is configured to be
    strict.
    """

    status_code = 406
    code = "not_acceptable"


class ConflictError(HTTPError):
    """The request conflicts with the current state of the resource.

    RFC 9110 §15.5.10 — uniqueness violations, state-machine conflicts,
    idempotency keys replayed with a different body.
    """

    status_code = 409
    code = "conflict"


class GoneError(HTTPError):
    """The resource existed but has been removed permanently.

    RFC 9110 §15.5.11 — deleted resources, API versions past their sunset date.
    """

    status_code = 410
    code = "gone"


class PreconditionFailedError(HTTPError):
    """A conditional request header (``If-Match``, ``If-Unmodified-Since``) evaluated to false.

    RFC 9110 §15.5.13 — the optimistic-concurrency answer to a stale update.
    """

    status_code = 412
    code = "precondition_failed"


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


class TooManyRequestsError(HTTPError):
    """The client sent too many requests; ``retry_after=`` sets ``Retry-After``.

    RFC 6585 §4. ``retry_after`` takes seconds (``int`` / ``float``), a
    ``timedelta`` or a timezone-aware ``datetime`` (rendered as an HTTP-date).
    """

    status_code = 429
    code = "too_many_requests"
    #: The ``retry_after`` value the error was raised with (``None`` when absent).
    retry_after: int | float | timedelta | datetime | None = None

    def __init__(
        self,
        message: str = "",
        *,
        retry_after: int | float | timedelta | datetime | None = None,
        detail: dict[str, Any] | None = None,
        headers: Mapping[str, str] | Iterable[tuple[str, str]] | None = None,
        status_code: int | None = None,
        code: str | None = None,
    ) -> None:
        super().__init__(message, detail=detail, headers=headers, status_code=status_code, code=code)
        self.retry_after = retry_after
        if retry_after is not None:
            self._add_headers([("retry-after", _format_retry_after(retry_after))])


class ServiceUnavailableError(HTTPError):
    """The server cannot handle the request right now; ``retry_after=`` sets ``Retry-After``.

    RFC 9110 §15.6.4 — draining, maintenance windows, load shedding, a failing
    readiness check.
    """

    status_code = 503
    code = "service_unavailable"
    #: The ``retry_after`` value the error was raised with (``None`` when absent).
    retry_after: int | float | timedelta | datetime | None = None

    def __init__(
        self,
        message: str = "",
        *,
        retry_after: int | float | timedelta | datetime | None = None,
        detail: dict[str, Any] | None = None,
        headers: Mapping[str, str] | Iterable[tuple[str, str]] | None = None,
        status_code: int | None = None,
        code: str | None = None,
    ) -> None:
        super().__init__(message, detail=detail, headers=headers, status_code=status_code, code=code)
        self.retry_after = retry_after
        if retry_after is not None:
            self._add_headers([("retry-after", _format_retry_after(retry_after))])


class RequestTimeoutError(HTTPError):
    """A server-side deadline was exceeded while handling the request (504).

    Not HTTP 408 — 408 means the *client* was too slow to send its request,
    which the ASGI server handles. This error means the handler (or an
    upstream call it made) blew its deadline; the ``request_timeout`` code
    matches the envelope lauren-middlewares' ``timeout`` middleware emits.
    ``retry_after=`` optionally sets ``Retry-After``.
    """

    status_code = 504
    code = "request_timeout"
    #: The ``retry_after`` value the error was raised with (``None`` when absent).
    retry_after: int | float | timedelta | datetime | None = None

    def __init__(
        self,
        message: str = "",
        *,
        retry_after: int | float | timedelta | datetime | None = None,
        detail: dict[str, Any] | None = None,
        headers: Mapping[str, str] | Iterable[tuple[str, str]] | None = None,
        status_code: int | None = None,
        code: str | None = None,
    ) -> None:
        super().__init__(message, detail=detail, headers=headers, status_code=status_code, code=code)
        self.retry_after = retry_after
        if retry_after is not None:
            self._add_headers([("retry-after", _format_retry_after(retry_after))])


class UnauthorizedError(HTTPError):
    """Authentication is missing or invalid; ``challenge=`` sets ``WWW-Authenticate``.

    RFC 9110 §15.5.2: a 401 must carry at least one challenge. Pass one
    string (``'Bearer realm="api"'``) or several (each becomes its own
    ``WWW-Authenticate`` header).
    """

    status_code = 401
    code = "unauthorized"

    def __init__(
        self,
        message: str = "",
        *,
        challenge: str | Iterable[str] | None = None,
        detail: dict[str, Any] | None = None,
        headers: Mapping[str, str] | Iterable[tuple[str, str]] | None = None,
        status_code: int | None = None,
        code: str | None = None,
    ) -> None:
        super().__init__(message, detail=detail, headers=headers, status_code=status_code, code=code)
        if challenge is not None:
            challenges = [challenge] if isinstance(challenge, str) else list(challenge)
            if not challenges:
                raise ValueError("challenge must be a non-empty string or a non-empty list of strings")
            self._add_headers(("www-authenticate", c) for c in challenges)


class ForbiddenError(HTTPError):
    """The caller is not allowed — also what a guard returning ``False`` raises by default.

    A guard class can declare ``deny_error = UnauthorizedError`` (or another
    :class:`HTTPError` subclass) to answer its denials with that error
    instead.
    """

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
    "ConfigValidationError",
    "OpenAPISchemaError",
    "DecoratorUsageError",
    "ExtractorError",
    "ExtractorFieldError",
    "BadRequestError",
    "NotFoundError",
    "RouteNotFoundError",
    "MethodNotAllowedError",
    "NotAcceptableError",
    "ConflictError",
    "GoneError",
    "PreconditionFailedError",
    "RequestBodyTooLarge",
    "UnsupportedMediaTypeError",
    "TooManyRequestsError",
    "ServiceUnavailableError",
    "RequestTimeoutError",
    "UnauthorizedError",
    "ForbiddenError",
    "MissingStateError",
    "StateTypeError",
    "LifecycleViolationError",
    "DestructError",
    "DestructTimeoutError",
    "DrainTimeoutError",
]
