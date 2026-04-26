# Error Catalog

Lauren ships a closed catalog of **28 error classes**, all rooted at `LaurenError`. Every HTTP-mapped error renders with the same envelope:

```json
{
  "error": {
    "code": "<stable-string-code>",
    "message": "<human-readable>",
    "detail": { /* optional structured detail */ }
  }
}
```

This stability — same shape, same code field, every time — is what enables downstream consumers (other services, audit pipelines, partner integrations) to programmatically distinguish errors.

## Hierarchy

```
LaurenError
├── StartupError                 # raised in LaurenFactory.create — never HTTP-mapped
│   ├── RouterConflictError
│   ├── CircularDependencyError
│   ├── CircularModuleError
│   ├── MissingProviderError
│   ├── ProtocolAmbiguityError
│   ├── ModuleExportViolation
│   ├── MetadataInheritanceError
│   ├── DuplicateBindingError
│   ├── UnresolvableParameterError
│   ├── DIScopeViolationError
│   ├── MiddlewareConfigError
│   ├── GuardConfigError
│   ├── ExceptionHandlerConfigError
│   ├── DecoratorUsageError
│   ├── OpenAPISchemaError
│   └── LifecycleConfigError
├── HTTPError                    # raised at request time — HTTP-mapped
│   ├── ExtractorError           → 422  code=extractor_error
│   ├── ExtractorFieldError      → 422  code=extractor_error
│   ├── RouteNotFoundError       → 404  code=route_not_found
│   ├── MethodNotAllowedError    → 405  code=method_not_allowed (sets Allow header)
│   ├── RequestBodyTooLarge      → 413  code=request_body_too_large
│   ├── UnauthorizedError        → 401  code=unauthorized
│   ├── ForbiddenError           → 403  code=forbidden
│   ├── MissingStateError        → 500  code=missing_state
│   └── StateTypeError           → 500  code=state_type_error
└── LifecycleError
    ├── LifecycleViolationError
    ├── DestructError
    ├── DestructTimeoutError
    └── DrainTimeoutError
```

## Startup errors (not HTTP-mapped)

These are caught in `LaurenFactory.create(...)` and never reach the request path. Each is fixed by changing your declarations.

| Class | Meaning |
|---|---|
| `RouterConflictError` | Two routes share the same `(method, path)`. |
| `CircularDependencyError` | The DI graph contains a cycle. |
| `CircularModuleError` | The module-import graph contains a cycle. |
| `MissingProviderError` | A constructor parameter has no visible provider in this module's scope. |
| `ProtocolAmbiguityError` | Two providers fight over the same scalar token without `multi=True`. |
| `ModuleExportViolation` | A module exports a token it neither declares nor imports. |
| `MetadataInheritanceError` | A subclass uses a parent's decoration without re-declaring (controllers, injectables, modules, middleware, exception handlers). |
| `DuplicateBindingError` | The same token is registered twice. |
| `UnresolvableParameterError` | A handler/provider parameter has no annotation and no default. |
| `DIScopeViolationError` | A `SINGLETON` depends on a `REQUEST`-scoped class (or other narrowing violation). |
| `MiddlewareConfigError` | A middleware class is missing `dispatch(request, call_next)`. |
| `GuardConfigError` | A guard class is missing `can_activate(ctx)`. |
| `ExceptionHandlerConfigError` | `@exception_handler` was used with no arguments / non-exception arguments / a class form missing `catch`. |
| `DecoratorUsageError` | A custom-provider helper was called incorrectly (`use_value`/`use_class`/`use_factory`/`use_existing`). |
| `OpenAPISchemaError` | OpenAPI generation failed to construct the schema. |
| `LifecycleConfigError` | Misuse of `@post_construct` / `@pre_destruct`. |

## HTTP-mapped errors

These can be raised from handlers, extractors, guards, or downstream middleware. Lauren maps them to HTTP responses with the standard envelope.

| Class | Status | `code` | Notes |
|---|---|---|---|
| `ExtractorError` | 422 | `extractor_error` | Thrown by Pydantic validation in `Json[Model]`, missing extractors, etc. |
| `ExtractorFieldError` | 422 | `extractor_error` | A specific field failed (constraints from `QueryField(...)`, etc.). |
| `RouteNotFoundError` | 404 | `route_not_found` | No route matched the request path. |
| `MethodNotAllowedError` | 405 | `method_not_allowed` | The path matched, but not for the request's HTTP method. The `Allow` response header is populated. |
| `RequestBodyTooLarge` | 413 | `request_body_too_large` | Request exceeds `LaurenFactory.create(..., max_body_size=N)`. |
| `UnauthorizedError` | 401 | `unauthorized` | Missing or invalid authentication. Idiomatic for guards. |
| `ForbiddenError` | 403 | `forbidden` | Authenticated but not authorized. |
| `MissingStateError` | 500 | `missing_state` | `request.state.require(key)` called for an absent key. |
| `StateTypeError` | 500 | `state_type_error` | `request.state.get_typed(key, T)` found a value of the wrong type. |

## Lifecycle errors

Reported through the structured logger. Teardown is best-effort — these errors are collected but never abort the rest of shutdown.

| Class | Meaning |
|---|---|
| `LifecycleViolationError` | A `@post_construct` is declared on an unsupported scope (e.g. transient). |
| `DestructError` | A `@pre_destruct` hook raised an exception. |
| `DestructTimeoutError` | A `@pre_destruct` hook exceeded its per-hook timeout. |
| `DrainTimeoutError` | The shutdown drain phase exceeded `drain_timeout`. |

## Subclassing `HTTPError` for domain errors

Most domain errors should subclass `HTTPError` directly:

```python
from lauren.exceptions import HTTPError

class UserNotFound(HTTPError):
    status_code = 404
    code = "user_not_found"

# In a handler:
raise UserNotFound("user does not exist", detail={"id": user_id})
```

Renders as:

```json
{"error": {"code": "user_not_found", "message": "user does not exist", "detail": {"id": 7}}}
```

For more sophisticated logging, auditing, or response shaping per error class, see [Custom Exception Handlers](../guides/custom-exception-handlers.md).

## Programmatic discovery

```python
import lauren.exceptions as exc

for cls in exc.LaurenError.__subclasses__():
    print(cls.__name__)
```

Or:

```python
from lauren.exceptions import HTTPError
for cls in HTTPError.__subclasses__():
    print(cls.__name__, cls.status_code, cls.code)
```

This catalog is **closed and stable** — adding a new error class is a public-API change and would be noted in the release notes.
