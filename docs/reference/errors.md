# Error Catalog

Lauren ships a **closed catalog** of error classes, all rooted at `LaurenError` — the [table below](#the-catalog) is generated from `lauren.exceptions`, so it is always complete, and [`error_catalog()`](#programmatic-discovery) returns the same list as data. Every HTTP-mapped error renders with the same envelope:

```json
{
  "error": {
    "code": "<stable-string-code>",
    "message": "<human-readable>",
    "detail": { /* optional structured detail */ }
  }
}
```

This stability — same shape, same code field, every time — is what enables downstream consumers (other services, audit pipelines, partner integrations) to programmatically distinguish errors. Apps that prefer the standard [RFC 9457 problem-details](#problem-details-rfc-9457) shape opt in with `error_format="rfc9457"`.

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
│   ├── BadRequestError  → 400  code=bad_request
│   ├── NotFoundError  → 404  code=not_found
│   │   └── RouteNotFoundError  → 404  code=route_not_found
│   ├── MethodNotAllowedError  → 405  code=method_not_allowed
│   ├── NotAcceptableError  → 406  code=not_acceptable
│   ├── ConflictError  → 409  code=conflict
│   ├── GoneError  → 410  code=gone
│   ├── PreconditionFailedError  → 412  code=precondition_failed
│   ├── RequestBodyTooLarge  → 413  code=request_body_too_large
│   ├── UnsupportedMediaTypeError  → 415  code=unsupported_media_type
│   ├── TooManyRequestsError  → 429  code=too_many_requests
│   ├── ServiceUnavailableError  → 503  code=service_unavailable
│   ├── RequestTimeoutError  → 504  code=request_timeout
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
| `BadRequestError` | `HTTPError` | 400 | `bad_request` | The request is malformed in a way that is not schema validation. |
| `NotFoundError` | `HTTPError` | 404 | `not_found` | The requested resource does not exist — the domain 404. |
| `RouteNotFoundError` | `NotFoundError` | 404 | `route_not_found` | No route matches the request path. |
| `MethodNotAllowedError` | `HTTPError` | 405 | `method_not_allowed` | The path matches, but not for this HTTP method; `allow=` sets the `Allow` header. |
| `NotAcceptableError` | `HTTPError` | 406 | `not_acceptable` | No available representation matches the request's `Accept` headers. |
| `ConflictError` | `HTTPError` | 409 | `conflict` | The request conflicts with the current state of the resource. |
| `GoneError` | `HTTPError` | 410 | `gone` | The resource existed but has been removed permanently. |
| `PreconditionFailedError` | `HTTPError` | 412 | `precondition_failed` | A conditional request header (`If-Match`, `If-Unmodified-Since`) evaluated to false. |
| `RequestBodyTooLarge` | `HTTPError` | 413 | `request_body_too_large` | The request body exceeds `max_body_size`. |
| `UnsupportedMediaTypeError` | `HTTPError` | 415 | `unsupported_media_type` | The request body's `Content-Type` is not one the route accepts. |
| `TooManyRequestsError` | `HTTPError` | 429 | `too_many_requests` | The client sent too many requests; `retry_after=` sets `Retry-After`. |
| `ServiceUnavailableError` | `HTTPError` | 503 | `service_unavailable` | The server cannot handle the request right now; `retry_after=` sets `Retry-After`. |
| `RequestTimeoutError` | `HTTPError` | 504 | `request_timeout` | A server-side deadline was exceeded while handling the request (504). |
| `UnauthorizedError` | `HTTPError` | 401 | `unauthorized` | Authentication is missing or invalid; `challenge=` sets `WWW-Authenticate`. |
| `ForbiddenError` | `HTTPError` | 403 | `forbidden` | The caller is not allowed — also what a guard returning `False` raises by default. |
| `MissingStateError` | `HTTPError` | 500 | `missing_state` | `request.state.require(key, T)` found no value for `key`. |
| `StateTypeError` | `HTTPError` | 500 | `state_type_error` | A `request.state` value has the wrong type for `get_typed` / `require`. |
| `LifecycleError` | `LaurenError` | — | `lifecycle_error` | Base class for errors around startup, shutdown and lifecycle hooks. |
| `LifecycleViolationError` | `LifecycleError` | — | `lifecycle_violation` | A lifecycle operation was used in the wrong state (`startup()` twice, a mutator after compile). |
| `DestructError` | `LifecycleError` | — | `destruct_error` | A `@pre_destruct` hook raised during shutdown (collected; shutdown continues). |
| `DestructTimeoutError` | `LifecycleError` | — | `destruct_timeout` | A `@pre_destruct` hook exceeded its timeout (collected; shutdown continues). |
| `DrainTimeoutError` | `LifecycleError` | — | `drain_timeout` | Reserved: not raised — a drain timeout at shutdown is logged as a warning. |
<!-- /lauren:facts -->

## Raising the built-in HTTP errors

The everyday statuses ship as leaves of `HTTPError` — import them from `lauren`
instead of declaring your own:

| Status | Class | `code` | Typical use |
|---|---|---|---|
| 400 | `BadRequestError` | `bad_request` | Malformed input that is not schema validation (broken JSON, a tampered cursor) |
| 401 | `UnauthorizedError` | `unauthorized` | Missing / invalid credentials — pass `challenge=` |
| 403 | `ForbiddenError` | `forbidden` | Authenticated but not allowed |
| 404 | `NotFoundError` | `not_found` | A resource that does not exist (routing misses raise its subclass `RouteNotFoundError`) |
| 405 | `MethodNotAllowedError` | `method_not_allowed` | Pass `allow=` — the `Allow` header is sent wherever it is raised |
| 406 | `NotAcceptableError` | `not_acceptable` | No representation matches `Accept` |
| 409 | `ConflictError` | `conflict` | Uniqueness / state conflicts |
| 410 | `GoneError` | `gone` | Permanently removed resources |
| 412 | `PreconditionFailedError` | `precondition_failed` | A failed `If-Match` / `If-Unmodified-Since` |
| 413 | `RequestBodyTooLarge` | `request_body_too_large` | Raised by the framework above `max_body_size` |
| 415 | `UnsupportedMediaTypeError` | `unsupported_media_type` | A body `Content-Type` the route does not accept |
| 422 | `ExtractorError` | `extractor_error` | Raised by extractors when validation fails |
| 429 | `TooManyRequestsError` | `too_many_requests` | Rate limits and quotas — pass `retry_after=` |
| 503 | `ServiceUnavailableError` | `service_unavailable` | Draining, maintenance, load shedding — pass `retry_after=` |
| 504 | `RequestTimeoutError` | `request_timeout` | A **server-side** deadline was exceeded (not HTTP 408) |

<!-- docs-contract: run -->
```python
from lauren import ConflictError, NotFoundError


class UserNotFound(NotFoundError):     # keeps 404, mints a stable domain code
    code = "user_not_found"


class EmailTaken(ConflictError):
    code = "email_taken"


error = UserNotFound("user does not exist", detail={"id": 7})
assert (error.status_code, error.code) == (404, "user_not_found")
```

Renders as:

```json
{"error": {"code": "user_not_found", "message": "user does not exist", "detail": {"id": 7}}}
```

Subclassing a built-in keeps the shared status (and lets one
`@exception_handler(NotFoundError)` claim every 404 of the app) while the
`code` stays domain-specific. Subclass `HTTPError` directly only for a status
the family does not cover. For logging, auditing, or response shaping per error
class, see [Custom Exception Handlers](../guides/custom-exception-handlers.md).

## Headers on errors

Every error accepts `headers=` — a mapping, or an iterable of `(name, value)`
pairs when a name repeats. Names are lower-cased; a CR, LF or NUL character, a
value that is not latin-1 encodable, or `content-type` / `content-length`
(framework-owned) raises `ValueError` **when the error is constructed**, so a
mistake surfaces at the raise site rather than as a malformed response.
Status-specific keywords build the headers HTTP requires:

<!-- docs-contract: run -->
```python
from datetime import datetime, timezone

from lauren import (
    MethodNotAllowedError,
    NotFoundError,
    ServiceUnavailableError,
    TooManyRequestsError,
    UnauthorizedError,
)

UnauthorizedError("bad token", challenge='Bearer realm="api", error="invalid_token"')  # WWW-Authenticate
TooManyRequestsError("slow down", retry_after=30)                                       # Retry-After: 30
ServiceUnavailableError("draining", retry_after=datetime(2026, 10, 23, 18, 0, tzinfo=timezone.utc))
#   → Retry-After: Fri, 23 Oct 2026 18:00:00 GMT (an RFC 9110 HTTP-date; naive datetimes are rejected)
MethodNotAllowedError("use PUT", allow=["PUT"])                                          # Allow: PUT
error = NotFoundError("gone", headers={"Cache-Control": "no-store"})
assert error.headers == (("cache-control", "no-store"),)
```

* `challenge=` (`UnauthorizedError` only) takes one string or a list — each
  becomes its own `WWW-Authenticate` header.
* `retry_after=` (`TooManyRequestsError`, `ServiceUnavailableError`,
  `RequestTimeoutError`) takes seconds (`int`/`float`, rounded up), a
  `timedelta`, or a timezone-aware `datetime`.
* The headers are sent in every `error_format`, including when the error is
  raised by a global middleware. An `@exception_handler` that returns its own
  `Response` owns that response: the error's headers are **not** merged into it
  (read `exc.headers` if you want them).

## Instance overrides

`HTTPError` accepts `status_code=` and `code=` that shadow the class attributes
on that one instance — an escape hatch for one-offs and for library code that
maps foreign errors. Subclassing remains the way to mint stable codes.

<!-- docs-contract: run -->
```python
from lauren import ConflictError, HTTPError

teapot = HTTPError("I'm a teapot", status_code=418, code="teapot")
stale = ConflictError("version mismatch", code="stale_version", detail={"expected": 4, "got": 3})
assert (teapot.status_code, stale.status_code, stale.code) == (418, 409, "stale_version")
```

`status_code` must be an `int` between 400 and 599 and `code` a non-empty
string (`ValueError` otherwise).

## Problem details (RFC 9457)

```python
app = LaurenFactory.create(
    AppModule,
    error_format="rfc9457",                           # "default" | "rfc9457" | "rfc7807"
    problem_type_base="https://errors.example.com/",  # optional
)
```

`raise UserNotFound("user does not exist", detail={"id": 7})` on `GET /users/7`
then answers with `Content-Type: application/problem+json`:

```json
{
  "type": "https://errors.example.com/user_not_found",
  "title": "Not Found",
  "status": 404,
  "detail": "user does not exist",
  "instance": "/users/7",
  "code": "user_not_found",
  "context": {"id": 7}
}
```

| Member | Rule |
|---|---|
| `type` | the class's `problem_type` if set; else `problem_type_base + code` when a base is configured; else `"about:blank"` (RFC 9457 §3.1.1) |
| `title` | the class's `problem_title` if set; else the HTTP status phrase |
| `status`, `code` | the error's status and stable code (`code` is a Lauren extension member) |
| `detail` | the error message (the title when the message is empty) |
| `instance` | the request path, as a relative URI reference |
| `context` | the error's structured `detail` dict (omitted when empty) |
| `errors` | an **array** of validation entries, for validation failures only |

Each `errors` entry has a human-readable `detail`, plus where the problem is:
`pointer` (an RFC 6901 JSON Pointer into the request body, as a URI fragment
such as `"#/address/city"`), or `in` + `parameter` for path / query / header /
cookie / form values, and a machine `code` when known. Entries come from an
`issues` list of `{"loc", "type", "msg"}` mappings in the error's `detail` (any
`HTTPError` carrying one gets the array), or — for today's extractor failures —
one entry per message of an `ExtractorError` naming a `field`.

Per-class `type` and `title`:

<!-- docs-contract: run -->
```python
from lauren import HTTPError


class OutOfCredit(HTTPError):
    status_code = 403
    code = "out_of_credit"
    problem_type = "https://example.com/probs/out-of-credit"
    problem_title = "You do not have enough credit."
```

`error_format="rfc7807"` is the **legacy** problem-details shape and stays
byte-for-byte as in earlier releases: a `urn:ietf:rfc:7231:<status>` default
`type`, no `instance`, and the `detail` dict in an object-valued `errors`
member (it does honour `problem_type_base`). Prefer `rfc9457` for new apps; a
future major release may make `rfc7807` an alias of it. Any other
`error_format` value falls back to `"default"` with a warning (a
`StartupError` from lauren 2.0).

## Guard denials

A guard whose `can_activate` returns `False` raises `ForbiddenError` (403) with
`detail={"guard": "<ClassName>"}`. A guard class can declare `deny_error` to
answer with another `HTTPError` — typically `UnauthorizedError` for
authentication guards — or a zero-argument factory returning a configured
instance (for a challenge header). It is validated by `LaurenFactory.create`:
anything else raises `GuardConfigError`. See
[Custom Guards](../guides/custom-guards.md#choosing-the-denial-error).

## Internal errors

An exception no handler claims is logged with its traceback and answered with
`500 internal_error`, `detail={"type": "<ExceptionClass>"}`. In development,
`LaurenFactory.create(..., debug=True)` replaces that response with a
[debug error page](../guides/debugging.md) for loopback clients.

!!! note "Deprecation notice"
    The `detail.type` member of `internal_error` and the `detail.guard` member
    of guard denials expose implementation class names. They stay in 1.x for
    compatibility; lauren 2.0 will emit them only when debug pages are enabled.
    Do not build client logic on them — match on `code`.

## WebSocket errors

WebSocket gateways use their own family (close codes instead of HTTP statuses);
see [WebSockets](../guides/websockets.md):

| Class | Base | Close code | `code` |
|---|---|---|---|
| `WebSocketError` | `LaurenError` | 1011 | `websocket_error` |
| `WebSocketDisconnect` | `WebSocketError` | 1000 | `websocket_disconnect` |
| `WebSocketValidationError` | `WebSocketError` | 1003 | `websocket_validation_error` |
| `WebSocketRouteNotFoundError` | `WebSocketError` | 1008 | `websocket_route_not_found` |
| `SocketIOError` | `LaurenError` | — | `socketio_error` (raised in-process, e.g. emitting on a closed Socket.IO connection) |

## Programmatic discovery

`error_catalog()` walks the hierarchy at call time and returns one
`ReflectedError` per class — framework errors and the application's own:

<!-- docs-contract: run -->
```python
from lauren import NotFoundError
from lauren.reflect import error_catalog


class InvoiceNotFound(NotFoundError):
    """No invoice has this number."""

    code = "invoice_not_found"


entry = next(e for e in error_catalog() if e.cls is InvoiceNotFound)
assert (entry.code, entry.status_code, entry.category) == ("invoice_not_found", 404, "http")
assert entry.summary == "No invoice has this number."

framework = error_catalog(include_user=False)          # only lauren's own classes
assert InvoiceNotFound not in {e.cls for e in framework}
```

`ReflectedError` fields: `cls`, `code`, `status_code` (`None` outside
`HTTPError`), `category` (`"startup"`, `"http"`, `"lifecycle"`, `"websocket"` or
`"other"`), `problem_type`, `problem_title`, and `summary` (the first paragraph
of the class docstring). It is meant for documentation, OpenAPI tooling and
tests — never call it on the request path.

This catalog is **closed and stable** — adding a framework error class is a
public-API change, made only through a justified design proposal, and noted in
the release notes.
