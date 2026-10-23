# Error Catalog

Lauren ships a **closed catalog** of error classes, all rooted at `LaurenError` — the [table below](#the-catalog) is generated from `lauren.exceptions`, so it is always complete. Every HTTP-mapped error renders with the same envelope:

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

<!-- lauren:facts:error-hierarchy -->
```text
LaurenError
├── StartupError
│   ├── RouterConflictError
│   ├── CircularDependencyError
│   ├── CircularModuleError
│   ├── MissingProviderError
│   ├── ProtocolAmbiguityError
│   ├── ModuleExportViolation
│   ├── LifecycleConfigError
│   ├── MetadataInheritanceError
│   ├── DuplicateBindingError
│   ├── UnresolvableParameterError
│   ├── DIScopeViolationError
│   ├── MiddlewareConfigError
│   ├── GuardConfigError
│   ├── InterceptorConfigError
│   ├── ExceptionHandlerConfigError
│   ├── SessionConfigError
│   ├── ConfigValidationError
│   ├── OpenAPISchemaError
│   └── DecoratorUsageError
├── HTTPError  → 500  code=http_error
│   ├── ExtractorError  → 422  code=extractor_error
│   │   └── ExtractorFieldError  → 422  code=extractor_field_error
│   ├── RouteNotFoundError  → 404  code=route_not_found
│   ├── MethodNotAllowedError  → 405  code=method_not_allowed
│   ├── RequestBodyTooLarge  → 413  code=request_body_too_large
│   ├── UnsupportedMediaTypeError  → 415  code=unsupported_media_type
│   ├── UnauthorizedError  → 401  code=unauthorized
│   ├── ForbiddenError  → 403  code=forbidden
│   ├── MissingStateError  → 500  code=missing_state
│   └── StateTypeError  → 500  code=state_type_error
└── LifecycleError
    ├── LifecycleViolationError
    ├── DestructError
    ├── DestructTimeoutError
    └── DrainTimeoutError
```
<!-- /lauren:facts -->

## The catalog

Generated from the class docstrings in `lauren/exceptions.py` by
`python scripts/sync_doc_facts.py` (`nox -s docs_contract` fails when it is
stale). **Startup errors** (`StartupError` subclasses) are raised by
`LaurenFactory.create(...)` — or at decoration time — and never reach the
request path; fix them by changing your declarations. **HTTP errors**
(`HTTPError` subclasses) can be raised from handlers, extractors, guards,
interceptors or middleware and render with the envelope above.
**Lifecycle errors** are reported through the structured logger; teardown is
best-effort, so they are collected rather than aborting shutdown.

<!-- lauren:facts:error-catalog -->
| Class | Base | HTTP status | `code` | Meaning |
|---|---|---|---|---|
| `LaurenError` | `Exception` | — | `lauren_error` | Base class for all lauren framework errors. |
| `StartupError` | `LaurenError` | — | `startup_error` | Base class for configuration errors raised by `LaurenFactory.create` (never HTTP-mapped). |
| `RouterConflictError` | `StartupError` | — | `router_conflict` | Two routes share the same (method, path) signature. |
| `CircularDependencyError` | `StartupError` | — | `circular_dependency` | A cycle was detected in the DI graph. |
| `CircularModuleError` | `StartupError` | — | `circular_module` | A cycle was detected in the module import graph. |
| `MissingProviderError` | `StartupError` | — | `missing_provider` | No provider is registered for a requested token. |
| `ProtocolAmbiguityError` | `StartupError` | — | `protocol_ambiguity` | A protocol has multiple providers but `multi=False` was requested. |
| `ModuleExportViolation` | `StartupError` | — | `module_export_violation` | A module exports a provider it neither declares nor imports. |
| `LifecycleConfigError` | `StartupError` | — | `lifecycle_config` | A lifecycle-bound declaration is misused (a hook on a non-method, a hosted service or job on a non-singleton, …). |
| `MetadataInheritanceError` | `StartupError` | — | `metadata_inheritance` | Injectable metadata was inherited from a base class (forbidden). |
| `DuplicateBindingError` | `StartupError` | — | `duplicate_binding` | The same class or token is registered twice in one container. |
| `UnresolvableParameterError` | `StartupError` | — | `unresolvable_parameter` | A handler or provider parameter cannot be resolved to an extractor or dep. |
| `DIScopeViolationError` | `StartupError` | — | `di_scope_violation` | A singleton depends on a request-scoped provider (or similar). |
| `MiddlewareConfigError` | `StartupError` | — | `middleware_config` | A middleware lacks `dispatch(request, call_next)`, or `@use_middlewares` cannot run where placed. |
| `GuardConfigError` | `StartupError` | — | `guard_config` | A guard lacks `can_activate(context)`, or `@use_guards` cannot run where placed. |
| `InterceptorConfigError` | `StartupError` | — | `interceptor_config` | An interceptor lacks `intercept(ctx, call_handler)`, or `@use_interceptors` cannot run where placed. |
| `ExceptionHandlerConfigError` | `StartupError` | — | `exception_handler_config` | An `@exception_handler` / `@use_exception_handlers` was misused. |
| `SessionConfigError` | `StartupError` | — | `session_config` | A `SessionConfig` was invalid, or a `Session` was injected into a handler in an app that did not enable sessions. |
| `ConfigValidationError` | `StartupError` | — | `config_validation` | One or more settings classes failed to load or validate. |
| `OpenAPISchemaError` | `StartupError` | — | `openapi_schema` | Reserved for strict OpenAPI generation — not raised yet. |
| `DecoratorUsageError` | `StartupError` | — | `decorator_usage` | A decorator or provider helper was used incorrectly — most often without parentheses. |
| `HTTPError` | `LaurenError` | 500 | `http_error` | Base class for errors mapped to HTTP responses. |
| `ExtractorError` | `HTTPError` | 422 | `extractor_error` | A request value could not be extracted or validated. |
| `ExtractorFieldError` | `ExtractorError` | 422 | `extractor_field_error` | One parameter is missing or violates a field constraint. |
| `RouteNotFoundError` | `HTTPError` | 404 | `route_not_found` | No route matches the request path. |
| `MethodNotAllowedError` | `HTTPError` | 405 | `method_not_allowed` | The path matches, but not for this HTTP method (the response sets `Allow`). |
| `RequestBodyTooLarge` | `HTTPError` | 413 | `request_body_too_large` | The request body exceeds `max_body_size`. |
| `UnsupportedMediaTypeError` | `HTTPError` | 415 | `unsupported_media_type` | The request body's `Content-Type` is not one the route accepts. |
| `UnauthorizedError` | `HTTPError` | 401 | `unauthorized` | Authentication is missing or invalid. |
| `ForbiddenError` | `HTTPError` | 403 | `forbidden` | The caller is not allowed — also what a guard returning `False` raises. |
| `MissingStateError` | `HTTPError` | 500 | `missing_state` | `request.state.require(key, T)` found no value for `key`. |
| `StateTypeError` | `HTTPError` | 500 | `state_type_error` | A `request.state` value has the wrong type for `get_typed` / `require`. |
| `LifecycleError` | `LaurenError` | — | `lifecycle_error` | Base class for errors around startup, shutdown and lifecycle hooks. |
| `LifecycleViolationError` | `LifecycleError` | — | `lifecycle_violation` | A lifecycle operation was used in the wrong state (`startup()` twice, a mutator after compile). |
| `DestructError` | `LifecycleError` | — | `destruct_error` | A `@pre_destruct` hook raised during shutdown (collected; shutdown continues). |
| `DestructTimeoutError` | `LifecycleError` | — | `destruct_timeout` | A `@pre_destruct` hook exceeded its timeout (collected; shutdown continues). |
| `DrainTimeoutError` | `LifecycleError` | — | `drain_timeout` | Reserved: not raised — a drain timeout at shutdown is logged as a warning. |
<!-- /lauren:facts -->

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
