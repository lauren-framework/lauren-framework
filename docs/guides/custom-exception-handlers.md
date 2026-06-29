# Custom Exception Handlers

> Exception handlers translate **domain or framework exceptions into HTTP responses**. They live alongside guards and middleware in the metadata graph: declared with `@exception_handler(...)`, attached with `@use_exception_handlers(...)` or registered globally, and invoked when a matching exception bubbles out of a handler, a guard, an extractor, or downstream middleware.

## Two forms — class and function

### Class form (DI-injected)

The class form is preferred when you need dependencies — a logger, a metrics client, a tracing span:

```python
from lauren import exception_handler
from lauren.logging import Logger
from lauren.types import Response, Request

@exception_handler(NotFoundError, ConflictError)
class DomainErrors:
    def __init__(self, log: Logger) -> None:
        self.log = log

    async def catch(self, exc: Exception, request: Request) -> Response:
        self.log.warn(f"domain error: {type(exc).__name__}: {exc}")
        return Response.json(
            {"error": {"code": "domain_error", "message": str(exc)}},
            status=400,
        )
```

What `@exception_handler` does:

* Validates that at least one exception type was passed (bare `@exception_handler` and empty `@exception_handler()` are rejected — a handler with no scope is a bug).
* Validates that every argument is a `BaseException` subclass.
* Auto-marks the class as `@injectable(scope=Scope.SINGLETON)` so the handler participates in DI exactly like guards and middleware.
* Verifies the class defines `catch(self, exc, request)`.

### Function form

When no DI is needed, a plain async function is enough:

```python
@exception_handler(ValueError)
async def handle_value_error(exc: ValueError, request: Request) -> Response:
    return Response.json({"detail": str(exc)}, status=422)
```

Function-form handlers are invoked directly with `(exc, request)` and **do not** participate in DI. If you need to inject services, switch to the class form.

## Declaring multiple exception types

A single handler can cover several exception types. Pass them all in one call:

```python
@exception_handler(UnauthorizedError, ForbiddenError)
def to_login(exc, request):
    return Response.redirect("/auth/login", status=303)
```

…or **stack** the decorator — the two forms are equivalent, and stacking accumulates (it does not overwrite):

```python
@exception_handler(UnauthorizedError)
@exception_handler(ForbiddenError)
def to_login(exc, request):
    return Response.redirect("/auth/login", status=303)
```

Both register the handler for `UnauthorizedError` **and** `ForbiddenError`. Stacking is handy when the types come from different imports or you want one per line for readability; duplicate types are de-duplicated. (Like every metadata decorator in Lauren — route verbs, `@use_*` — stacking accumulates.)

## Three places to register

```python
# 1. Per route — handles only this handler's exceptions:
@get("/x")
@use_exception_handlers(NotFoundHandler)
async def x(self): ...

# 2. Per controller — handles every handler on the class:
@use_exception_handlers(DomainErrors)
@controller("/users")
class UserController: ...

# 3. Global — handles every request:
app = LaurenFactory.create(
    AppModule,
    global_exception_handlers=[DomainErrors, AuditFailures],
)
```

Resolution order (most specific wins):

1. Route-level handlers.
2. Controller-level handlers.
3. Global handlers.

Within each tier, handlers are tried **in registration order**, and the first one whose declared exception types match (`isinstance(exc, declared_types)` — subclasses included) handles the exception. If none match, Lauren's built-in error pipeline handles it (HTTPError-derived classes get their structured envelope; everything else becomes a 500).

## Decoration order is irrelevant

Lauren accepts both:

```python
@use_exception_handlers(NotFoundHandler)
@controller("/users")
class A: ...

@controller("/users")
@use_exception_handlers(NotFoundHandler)
class B: ...
```

The `@use_exception_handlers` decorator only attaches metadata; the actual wiring is done at startup.

## When are handlers invoked?

Lauren's exception handlers fire whenever a matching exception escapes:

* a route handler,
* an extractor (e.g. JSON validation failures, missing headers),
* a guard (e.g. `UnauthorizedError`, `ForbiddenError`),
* downstream middleware that re-raises.

What handlers **do not** see:

* Exceptions raised during `LaurenFactory.create(...)` — those are startup errors and should never be shaped as HTTP responses.
* Exceptions raised inside `@post_construct` or `@pre_destruct` — those are lifecycle errors with their own logging path.

## Composing with built-in `HTTPError`s

Lauren ships a 28-class error catalog. Most domain errors should subclass `HTTPError`:

```python
from lauren.exceptions import HTTPError

class UserNotFound(HTTPError):
    status_code = 404
    code = "user_not_found"

# In a handler:
raise UserNotFound("user does not exist", detail={"id": user_id})
```

Without any custom exception handler, this already serializes as:

```json
{"error": {"code": "user_not_found", "message": "user does not exist", "detail": {"id": 7}}}
```

Custom handlers are most valuable when you want to:

* Add logging, metrics, or audit-trail emission.
* Translate a *non*-`HTTPError` (e.g. `sqlalchemy.NoResultFound`) into a structured response.
* Override the default envelope shape for a specific exception family.

## Pattern: translate ORM errors

Suppose you use SQLAlchemy and want missing-row errors from the ORM to become 404s with no per-handler boilerplate:

```python
from sqlalchemy.exc import NoResultFound
from lauren import exception_handler
from lauren.types import Response

@exception_handler(NoResultFound)
class NotFoundFromOrm:
    async def catch(self, exc: NoResultFound, request) -> Response:
        return Response.json(
            {"error": {"code": "not_found", "message": "resource not found"}},
            status=404,
        )

# Register globally so every controller benefits:
app = LaurenFactory.create(AppModule, global_exception_handlers=[NotFoundFromOrm])
```

Now any handler can write `db.scalar(stmt).one()` without try/except — missing rows automatically render as 404s.

## Pattern: audit-trail integration

```python
@exception_handler(BillingError)
class AuditedBilling:
    def __init__(self, audit: AuditClient, log: Logger) -> None:
        self.audit = audit
        self.log = log

    async def catch(self, exc: BillingError, request) -> Response:
        await self.audit.record({
            "kind": "billing_error",
            "user_id": request.state.get("user_id"),
            "code": exc.code,
            "message": str(exc),
            "rid": request.state.get("rid"),
        })
        self.log.warn(f"billing error: {exc.code}")
        return Response.json(
            {"error": {"code": exc.code, "message": str(exc), "detail": exc.detail}},
            status=exc.status_code,
        )
```

The handler logs, audits, and shapes the response — all in one place, attached to whatever scope is appropriate (probably global for a billing audit policy).

## Pattern: per-route override

A specific endpoint needs a custom 401 body that doesn't match the global policy? Attach a per-route handler:

```python
@exception_handler(UnauthorizedError)
async def login_unauthorized(exc: UnauthorizedError, request) -> Response:
    return Response.json(
        {"error": "invalid_credentials", "redirect": "/login"},
        status=401,
    )

@controller("/auth")
class AuthController:
    @post("/login")
    @use_exception_handlers(login_unauthorized)
    async def login(self, body: Json[LoginRequest]) -> dict:
        if not is_valid(body):
            raise UnauthorizedError("bad credentials")
        return {"token": ...}
```

## Pattern: catch-all handler

You can register an `Exception` handler globally to override the framework's default 500 page:

```python
@exception_handler(Exception)
class CrashHandler:
    def __init__(self, log: Logger) -> None:
        self.log = log

    async def catch(self, exc: Exception, request) -> Response:
        self.log.error(f"unhandled: {type(exc).__name__}: {exc}")
        return Response.json(
            {"error": {"code": "internal_error", "message": "something went wrong"}},
            status=500,
        )

app = LaurenFactory.create(AppModule, global_exception_handlers=[CrashHandler])
```

A catch-all handler should always be the **last** registration — more specific handlers should win first.

## Inheritance

Like every other class-level decorator:

* `@exception_handler` does **not** propagate to subclasses — you must redecorate.
* `@use_exception_handlers` attaches to **the exact target** only — a subclass doesn't inherit it.

This avoids the surprise of "I subclassed my AdminController for a test fixture and now my exception handler runs in the test instead of the production controller" type bugs. See [Class Inheritance Rules](../core-concepts/inheritance.md) for the full reasoning.

## Errors raised at startup

| Error | Meaning |
|---|---|
| `ExceptionHandlerConfigError` | `@exception_handler` was used without arguments, with non-exception arguments, or a class form is missing `catch`. |
| `MetadataInheritanceError` | A subclass of an exception-handler class was registered without re-decoration. |
| Anything passed to `@use_exception_handlers` that isn't `@exception_handler`-decorated | Raised with a clear "decorate it with @exception_handler first" message. |

## Testing exception handlers

Drive them through the `TestClient` — the same pattern as guards and middleware:

```python
from lauren.testing import TestClient

def test_user_not_found_returns_404():
    c = TestClient(app)
    r = c.get("/users/9999")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "user_not_found"

def test_orm_no_result_handler():
    c = TestClient(app)
    r = c.get("/orders/missing")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "not_found"
```

For unit tests, instantiate the handler directly and call `catch`:

```python
import pytest

async def test_audited_billing_logs_and_responds():
    audit = FakeAudit()
    log = FakeLog()
    h = AuditedBilling(audit, log)
    request = build_test_request("/x")  # however your fixture builds requests
    resp = await h.catch(BillingError("declined"), request)
    assert resp.status == 402  # or whatever
    assert audit.records[-1]["kind"] == "billing_error"
```

## Best practices

* **Prefer subclassing `HTTPError` for domain errors.** They serialize cleanly without any custom handler. Reach for `@exception_handler` when you need *side effects* (logging, audit, metrics) or when you can't change the exception class (third-party library).
* **Register narrow handlers first.** A `Exception`-typed catch-all should be last; otherwise it'll consume errors more specific handlers were meant to handle.
* **Don't raise inside `catch`.** Exception handlers should *return* responses. A raise inside `catch` becomes a hard 500.
* **Keep handler logic small.** A handler that's more than ~30 lines is usually trying to be a controller or a service. Extract.

## See also

* [Custom Guards](custom-guards.md) — for raising auth errors that handlers will catch.
* [Custom Middleware](custom-middleware.md) — for cross-cutting concerns that aren't exception-driven.
* [Reference → Error Catalog](../reference/errors.md) — the 28 built-in error classes you can subclass and handle.
