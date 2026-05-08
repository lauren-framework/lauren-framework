# Exception Catalog

All 28 typed exception classes exported by the framework.

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

### `HTTPError`

```python
class HTTPError
```

Base class for errors mapped to HTTP responses.

### `LifecycleError`

```python
class LifecycleError
```

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

A configurable decorator was used without parentheses.

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

### `MetadataInheritanceError`

```python
class MetadataInheritanceError
```

Injectable metadata was inherited from a base class (forbidden).

### `DuplicateBindingError`

```python
class DuplicateBindingError
```

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

### `GuardConfigError`

```python
class GuardConfigError
```

### `InterceptorConfigError`

```python
class InterceptorConfigError
```

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

### `ExtractorError`

```python
class ExtractorError
```

### `ExtractorFieldError`

```python
class ExtractorFieldError
```

## HTTP errors

### `RouteNotFoundError`

```python
class RouteNotFoundError
```

### `MethodNotAllowedError`

```python
class MethodNotAllowedError(message: str = '', allow: list[str] | None = None)
```

### `RequestBodyTooLarge`

```python
class RequestBodyTooLarge
```

### `UnauthorizedError`

```python
class UnauthorizedError
```

### `ForbiddenError`

```python
class ForbiddenError
```

## Lifecycle errors

### `MissingStateError`

```python
class MissingStateError
```

### `StateTypeError`

```python
class StateTypeError
```

### `LifecycleViolationError`

```python
class LifecycleViolationError
```

### `DestructError`

```python
class DestructError
```

### `DestructTimeoutError`

```python
class DestructTimeoutError
```

### `DrainTimeoutError`

```python
class DrainTimeoutError
```

