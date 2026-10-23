# Exception Catalog

Every typed exception class exported by the framework.

## Base classes

### `LaurenError`

```python
class LaurenError(message: str = '', detail: dict[str, Any] | None = None)
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
class HTTPError
```

Base class for errors mapped to HTTP responses.

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

### `OpenAPISchemaError`

```python
class OpenAPISchemaError
```

Reserved for strict OpenAPI generation — not raised yet.

A schema that cannot be generated currently degrades to a generic
``{"type": "object"}`` entry instead of failing startup.

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

## HTTP errors

### `RouteNotFoundError`

```python
class RouteNotFoundError
```

No route matches the request path.

### `MethodNotAllowedError`

```python
class MethodNotAllowedError(message: str = '', allow: list[str] | None = None)
```

The path matches, but not for this HTTP method (the response sets ``Allow``).

### `RequestBodyTooLarge`

```python
class RequestBodyTooLarge
```

The request body exceeds ``max_body_size``.

### `UnauthorizedError`

```python
class UnauthorizedError
```

Authentication is missing or invalid.

### `ForbiddenError`

```python
class ForbiddenError
```

The caller is not allowed — also what a guard returning ``False`` raises.

## Lifecycle errors

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
