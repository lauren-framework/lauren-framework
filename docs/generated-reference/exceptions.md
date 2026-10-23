# Exception Catalog

Every typed exception class exported by the framework.

## Base classes

### `LaurenError`

```python
class LaurenError(message: str = '', detail: dict[str, Any] | None = None, headers: Mapping[str, str] | Iterable[tuple[str, str]] | None = None)
```

Base class for all lauren framework errors.

#### `LaurenError.to_payload`

```python
def to_payload(self) -> dict[str, Any]
```

Return the canonical JSON payload for this error.

### `StartupError`

```python
class StartupError
```

Base class for configuration errors raised by ``LaurenFactory.create`` (never HTTP-mapped).

### `HTTPError`

```python
class HTTPError(message: str = '', detail: dict[str, Any] | None = None, headers: Mapping[str, str] | Iterable[tuple[str, str]] | None = None, status_code: int | None = None, code: str | None = None)
```

Base class for errors mapped to HTTP responses.

Subclass it (or one of the built-in leaves such as :class:`NotFoundError`)
to mint a stable domain ``code``. ``status_code=`` and ``code=`` passed to
the constructor override the class attributes on that one instance — an
escape hatch for one-offs and for library code that maps foreign errors.
``problem_type`` / ``problem_title`` set the RFC 9457 ``type`` and
``title`` members when the app uses a problem-details ``error_format``.

### `LifecycleError`

```python
class LifecycleError
```

Base class for errors around startup, shutdown and lifecycle hooks.

## Startup errors

### `RouterConflictError`

```python
class RouterConflictError
```

Two routes share the same (method, path) signature.

### `CircularDependencyError`

```python
class CircularDependencyError
```

A cycle was detected in the DI graph.

### `CircularModuleError`

```python
class CircularModuleError
```

A cycle was detected in the module import graph.

### `DecoratorUsageError`

```python
class DecoratorUsageError
```

A decorator or provider helper was used incorrectly — most often without parentheses.

``@controller``, ``@injectable``, ``@module``, and the HTTP verb
decorators (``@get``, ``@post``, ...) must be invoked with arguments
(or empty parentheses) so their configuration is always explicit::

    @controller("/pets")       # correct
    @controller()             # correct — defaults are fine
    @controller               # rejected — ambiguous

### `MissingProviderError`

```python
class MissingProviderError
```

No provider is registered for a requested token.

### `ProtocolAmbiguityError`

```python
class ProtocolAmbiguityError
```

A protocol has multiple providers but ``multi=False`` was requested.

### `ModuleExportViolation`

```python
class ModuleExportViolation
```

A module exports a provider it neither declares nor imports.

### `LifecycleConfigError`

```python
class LifecycleConfigError
```

A lifecycle-bound declaration is misused (a hook on a non-method, a hosted service or job on a non-singleton, …).

### `MetadataInheritanceError`

```python
class MetadataInheritanceError
```

Injectable metadata was inherited from a base class (forbidden).

### `DuplicateBindingError`

```python
class DuplicateBindingError
```

The same class or token is registered twice in one container.

### `UnresolvableParameterError`

```python
class UnresolvableParameterError
```

A handler or provider parameter cannot be resolved to an extractor or dep.

### `DIScopeViolationError`

```python
class DIScopeViolationError
```

A singleton depends on a request-scoped provider (or similar).

### `MiddlewareConfigError`

```python
class MiddlewareConfigError
```

A middleware lacks ``dispatch(request, call_next)``, or ``@use_middlewares`` cannot run where placed.

### `GuardConfigError`

```python
class GuardConfigError
```

A guard lacks ``can_activate(context)``, or ``@use_guards`` cannot run where placed.

### `InterceptorConfigError`

```python
class InterceptorConfigError
```

An interceptor lacks ``intercept(ctx, call_handler)``, or ``@use_interceptors`` cannot run where placed.

### `ExceptionHandlerConfigError`

```python
class ExceptionHandlerConfigError
```

An ``@exception_handler`` / ``@use_exception_handlers`` was misused.

Raised when:

* ``@exception_handler`` is invoked with no exception types or with
  something that is not a ``BaseException`` subclass;
* ``@exception_handler`` decorates a class without a ``catch`` method;
* ``@use_exception_handlers`` references a class / function that was
  never decorated with ``@exception_handler``.

### `SessionConfigError`

```python
class SessionConfigError
```

A :class:`~lauren.SessionConfig` was invalid, or a ``Session`` was
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

### `OpenAPISchemaError`

```python
class OpenAPISchemaError
```

Reserved for strict OpenAPI generation — not raised yet.

A schema that cannot be generated currently degrades to a generic
``{"type": "object"}`` entry instead of failing startup.

## HTTP errors

### `ExtractorError`

```python
class ExtractorError
```

A request value could not be extracted or validated.

### `ExtractorFieldError`

```python
class ExtractorFieldError
```

One parameter is missing or violates a field constraint.

### `BadRequestError`

```python
class BadRequestError
```

The request is malformed in a way that is not schema validation.

RFC 9110 §15.5.1. Use it for unparsable input (broken JSON or UTF-8, a
tampered cursor); field-level validation failures are 422
:class:`ExtractorError` responses.

### `UnauthorizedError`

```python
class UnauthorizedError(message: str = '', challenge: str | Iterable[str] | None = None, detail: dict[str, Any] | None = None, headers: Mapping[str, str] | Iterable[tuple[str, str]] | None = None, status_code: int | None = None, code: str | None = None)
```

Authentication is missing or invalid; ``challenge=`` sets ``WWW-Authenticate``.

RFC 9110 §15.5.2: a 401 must carry at least one challenge. Pass one
string (``'Bearer realm="api"'``) or several (each becomes its own
``WWW-Authenticate`` header).

### `ForbiddenError`

```python
class ForbiddenError
```

The caller is not allowed — also what a guard returning ``False`` raises by default.

A guard class can declare ``deny_error = UnauthorizedError`` (or another
:class:`HTTPError` subclass) to answer its denials with that error
instead.

### `NotFoundError`

```python
class NotFoundError
```

The requested resource does not exist — the domain 404.

RFC 9110 §15.5.5. Subclass it for domain codes
(``class UserNotFound(NotFoundError): code = "user_not_found"``). Routing
misses raise its subclass :class:`RouteNotFoundError`.

### `RouteNotFoundError`

```python
class RouteNotFoundError
```

No route matches the request path.

### `MethodNotAllowedError`

```python
class MethodNotAllowedError(message: str = '', allow: Iterable[str] | None = None, detail: dict[str, Any] | None = None, headers: Mapping[str, str] | Iterable[tuple[str, str]] | None = None, status_code: int | None = None, code: str | None = None)
```

The path matches, but not for this HTTP method; ``allow=`` sets the ``Allow`` header.

RFC 9110 §15.5.6 requires ``Allow`` on every 405. The header is emitted
wherever the error is raised — by the router or by a handler.

### `NotAcceptableError`

```python
class NotAcceptableError
```

No available representation matches the request's ``Accept`` headers.

RFC 9110 §15.5.7. Raised by content negotiation that is configured to be
strict.

### `ConflictError`

```python
class ConflictError
```

The request conflicts with the current state of the resource.

RFC 9110 §15.5.10 — uniqueness violations, state-machine conflicts,
idempotency keys replayed with a different body.

### `GoneError`

```python
class GoneError
```

The resource existed but has been removed permanently.

RFC 9110 §15.5.11 — deleted resources, API versions past their sunset date.

### `PreconditionFailedError`

```python
class PreconditionFailedError
```

A conditional request header (``If-Match``, ``If-Unmodified-Since``) evaluated to false.

RFC 9110 §15.5.13 — the optimistic-concurrency answer to a stale update.

### `RequestBodyTooLarge`

```python
class RequestBodyTooLarge
```

The request body exceeds ``max_body_size``.

### `UnsupportedMediaTypeError`

```python
class UnsupportedMediaTypeError
```

The request body's ``Content-Type`` is not one the route accepts.

Raised before the body is read when
``ContentConfig(strict_content_type=True)`` is in effect and a JSON body
parameter receives a non-JSON (or missing) ``Content-Type``.

### `TooManyRequestsError`

```python
class TooManyRequestsError(message: str = '', retry_after: int | float | timedelta | datetime | None = None, detail: dict[str, Any] | None = None, headers: Mapping[str, str] | Iterable[tuple[str, str]] | None = None, status_code: int | None = None, code: str | None = None)
```

The client sent too many requests; ``retry_after=`` sets ``Retry-After``.

RFC 6585 §4. ``retry_after`` takes seconds (``int`` / ``float``), a
``timedelta`` or a timezone-aware ``datetime`` (rendered as an HTTP-date).

### `ServiceUnavailableError`

```python
class ServiceUnavailableError(message: str = '', retry_after: int | float | timedelta | datetime | None = None, detail: dict[str, Any] | None = None, headers: Mapping[str, str] | Iterable[tuple[str, str]] | None = None, status_code: int | None = None, code: str | None = None)
```

The server cannot handle the request right now; ``retry_after=`` sets ``Retry-After``.

RFC 9110 §15.6.4 — draining, maintenance windows, load shedding, a failing
readiness check.

### `RequestTimeoutError`

```python
class RequestTimeoutError(message: str = '', retry_after: int | float | timedelta | datetime | None = None, detail: dict[str, Any] | None = None, headers: Mapping[str, str] | Iterable[tuple[str, str]] | None = None, status_code: int | None = None, code: str | None = None)
```

A server-side deadline was exceeded while handling the request (504).

Not HTTP 408 — 408 means the *client* was too slow to send its request,
which the ASGI server handles. This error means the handler (or an
upstream call it made) blew its deadline; the ``request_timeout`` code
matches the envelope lauren-middlewares' ``timeout`` middleware emits.
``retry_after=`` optionally sets ``Retry-After``.

### `MissingStateError`

```python
class MissingStateError
```

``request.state.require(key, T)`` found no value for ``key``.

### `StateTypeError`

```python
class StateTypeError
```

A ``request.state`` value has the wrong type for ``get_typed`` / ``require``.

## Lifecycle errors

### `LifecycleViolationError`

```python
class LifecycleViolationError
```

A lifecycle operation was used in the wrong state (``startup()`` twice, a mutator after compile).

### `DestructError`

```python
class DestructError
```

A ``@pre_destruct`` hook raised during shutdown (collected; shutdown continues).

### `DestructTimeoutError`

```python
class DestructTimeoutError
```

A ``@pre_destruct`` hook exceeded its timeout (collected; shutdown continues).

### `DrainTimeoutError`

```python
class DrainTimeoutError
```

Reserved: not raised — a drain timeout at shutdown is logged as a warning.
