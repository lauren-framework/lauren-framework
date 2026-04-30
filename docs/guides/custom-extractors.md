# Custom Extractors

> Extractors decompose a request into typed Python values. Lauren ships nine built-ins (`Path`, `Query`, `Header`, `Cookie`, `Json`, `Form`, `Bytes`, `State`, `Depends`); the rest is up to you. A **custom extractor** is any subclass of `ExtractionMarker` that implements an `extract` method. Use them as parameter annotations and you've created a typed, reusable, declarative way to pull domain data into your handlers.

## Why custom extractors?

Built-in extractors handle the *transport-level* shape of a request — path segments, query strings, headers, JSON bodies. Custom extractors handle the **domain-level** shape: the *current user*, the *active tenant*, the *parsed pagination cursor*, the *idempotency key*, and so on.

Compare:

```python
# Without a custom extractor — repeat this in every handler:
@get("/me")
async def me(self, request: Request) -> dict:
    uid = request.state.get("user_id")
    if uid is None:
        raise UnauthorizedError("missing auth")
    user = await self.repo.get(uid)
    return {"id": user.id, "name": user.name}

# With CurrentUser as a custom extractor:
@get("/me")
async def me(self, user: CurrentUser) -> dict:
    return {"id": user.id, "name": user.name}
```

Authorization, repo lookup, error handling — all handled by the extractor, declared once and reused everywhere.

## Anatomy of a custom extractor

Every custom extractor subclasses `ExtractionMarker` and overrides `extract`:

```python
from lauren.extractors import Extraction, ExtractionMarker
from lauren.types import ExecutionContext

class CurrentUser(ExtractionMarker):
    source = "app.current_user"   # any unique string id (used in errors / logs)

    async def extract(
        self,
        execution_context: ExecutionContext,
        extraction: Extraction,
    ) -> User:
        uid = execution_context.request.state.get("user_id")
        if uid is None:
            if extraction.has_default:
                return extraction.default
            raise UnauthorizedError("not authenticated")
        ...
```

The method receives:

| Param | Type | What it is |
|---|---|---|
| `execution_context` | `ExecutionContext` | Full context for the in-flight request: the request object, the matched controller class, the matched handler function, the route template, and any handler-level metadata. |
| `extraction` | `Extraction` | Metadata about the parameter: `name`, `default`, `has_default`, `inner_type`, etc. |

**Return** the value the handler should receive. **Raise** any `HTTPError` subclass to short-circuit with the matching status.

### `ExecutionContext` reference

By the time `extract` runs, the route has already been matched and the handler is known. `ExecutionContext` carries everything extractors and guards need:

| Field | Type | Description |
|---|---|---|
| `ctx.request` | `Request` | The current request object. |
| `ctx.handler_class` | `type \| None` | The controller class (e.g. `UserController`). |
| `ctx.handler_func` | `Callable \| None` | The handler function (e.g. `get_user`). |
| `ctx.route_template` | `str \| None` | The declared path template, e.g. `"/users/{id}"`. |
| `ctx.metadata` | `dict[str, Any]` | Handler-level metadata set by `@set_metadata(...)`. |
| `ctx.get_metadata(key, default)` | `Any` | Convenience accessor for `ctx.metadata`. |

`ExecutionContext` is the same object guards receive via `can_activate(ctx)`, so extractor code that inspects metadata (e.g. to check a `@public` marker) works identically.

### DI in extractors — `@injectable` is optional

By default the framework instantiates the extractor class **once with no constructor arguments** and reuses that instance across all requests (process-wide cache). This is fine for stateless extractors.

When the extractor needs services, decorate it with `@injectable`:

```python
from lauren import injectable, Scope
from lauren.extractors import Extraction, ExtractionMarker
from lauren.types import ExecutionContext

@injectable(scope=Scope.REQUEST)
class CurrentUser(ExtractionMarker):
    source = "app.current_user"

    def __init__(self, session: DbSession) -> None:
        self._session = session

    async def extract(
        self,
        execution_context: ExecutionContext,
        extraction: Extraction,
    ) -> User:
        uid = execution_context.request.state.get("user_id")
        if uid is None:
            if extraction.has_default:
                return extraction.default
            raise UnauthorizedError("not authenticated")
        user = await self._session.get(User, uid)
        if user is None:
            raise UnauthorizedError("user vanished")
        return user
```

With `@injectable`, the DI container resolves the extractor instance, injecting constructor dependencies automatically. The `extract` method signature stays the same regardless.

| | Stateless (no `@injectable`) | Injectable |
|---|---|---|
| **Instance lifecycle** | Created once (no-arg), shared process-wide | Resolved by DI container per scope |
| **Constructor deps** | None (no-arg `__init__`) | Injected automatically |
| **`providers` list** | Not required | Must be in module's `providers` |
| **`@injectable` inheritance** | N/A | Not inherited — re-decorate each subclass |

## Step-by-step: build a `TenantId` extractor

Suppose your service is multi-tenant and every authenticated request carries an `x-tenant` header. The handler shouldn't care about the header lookup or the validation — it just wants a `Tenant`.

### Step 1 — model the value type

```python
from dataclasses import dataclass

@dataclass(frozen=True)
class Tenant:
    id: str
    name: str
    plan: str
```

### Step 2 — write the extractor

Since this extractor depends on `TenantRepository`, mark it with `@injectable`:

```python
from lauren import injectable, Scope
from lauren.extractors import Extraction, ExtractionMarker
from lauren.exceptions import HTTPError
from lauren.types import ExecutionContext

class BadTenantError(HTTPError):
    status_code = 400
    code = "bad_tenant"

@injectable(scope=Scope.REQUEST)
class TenantId(ExtractionMarker):
    source = "app.tenant_id"

    def __init__(self, repo: TenantRepository) -> None:
        self._repo = repo

    async def extract(
        self,
        execution_context: ExecutionContext,
        extraction: Extraction,
    ) -> Tenant:
        raw = execution_context.request.headers.get("x-tenant")
        if not raw:
            raise BadTenantError("missing x-tenant header")
        tenant = await self._repo.find(raw)
        if tenant is None:
            raise BadTenantError("unknown tenant", detail={"id": raw})
        return tenant
```

Add `TenantId` to the module's `providers` list alongside `TenantRepository`.

### Step 3 — use it as an annotation

```python
@controller("/dashboard")
class DashboardController:
    @get("/")
    async def index(self, tenant: TenantId) -> dict:
        return {"tenant": tenant.id, "plan": tenant.plan}

    @get("/users")
    async def users(self, tenant: TenantId, repo: Depends[UserRepo]) -> list[dict]:
        return [u.dict() for u in await repo.for_tenant(tenant.id)]
```

That's it. Every handler that takes `tenant: TenantId` gets a fully-validated `Tenant` object. Missing or invalid headers turn into `400 Bad Request` automatically.

## Patterns

### Optional values

Use `extraction.has_default` / `extraction.default` to support `User | None = None` style:

```python
class CurrentUser(ExtractionMarker):
    source = "app.current_user"

    async def extract(
        self,
        execution_context: ExecutionContext,
        extraction: Extraction,
    ) -> User | None:
        uid = execution_context.request.state.get("user_id")
        if uid is None:
            if extraction.has_default:
                return extraction.default
            raise UnauthorizedError("not authenticated")
        ...
```

Now both work:

```python
async def me(self, user: CurrentUser) -> dict: ...                     # required
async def search(self, user: CurrentUser | None = None) -> dict: ...   # optional
```

### Reading route metadata

The `execution_context.get_metadata(key)` method lets extractors read handler-level metadata set by `@set_metadata`. This is exactly the same API guards use, so patterns like `@public` extend naturally to extractors:

```python
from lauren import set_metadata

TENANT_REQUIRED_KEY = "app.tenant_required"
require_tenant = set_metadata(TENANT_REQUIRED_KEY, True)

class TenantId(ExtractionMarker):
    source = "app.tenant_id"

    async def extract(
        self,
        execution_context: ExecutionContext,
        extraction: Extraction,
    ) -> Tenant | None:
        if not execution_context.get_metadata(TENANT_REQUIRED_KEY):
            return None   # opt-out route
        raw = execution_context.request.headers.get("x-tenant")
        ...
```

### Composing on top of built-in extractors

If you need a Pydantic-validated body *and* some side-effect, write a small wrapper extractor instead of doing it inline:

```python
class IdempotentCreate(ExtractionMarker):
    source = "app.idempotent_create"

    async def extract(
        self,
        execution_context: ExecutionContext,
        extraction: Extraction,
    ) -> tuple[dict, str]:
        req = execution_context.request
        key = req.headers.get("idempotency-key")
        if not key:
            raise HTTPError("missing idempotency-key", status_code=400)
        body = await req.json()
        return body, key
```

### Streaming-aware extractors

For large uploads, take the streaming primitives:

```python
class CSVRows(ExtractionMarker):
    source = "app.csv_rows"

    async def extract(
        self,
        execution_context: ExecutionContext,
        extraction: Extraction,
    ):
        async def rows():
            buf = b""
            async for chunk in execution_context.request.stream():
                buf += chunk
                while b"\n" in buf:
                    line, _, buf = buf.partition(b"\n")
                    yield line.decode()
        return rows()
```

### Legacy classmethod form (backward compat)

If you have an existing extractor that uses the `@classmethod` form with explicit `container` and `request_cache`, it still works unchanged:

```python
class MyExtractor(ExtractionMarker):
    source = "my.extractor"

    @classmethod
    async def extract(cls, request, extraction, *, container, request_cache):
        ...
```

New extractors should use the instance-method form documented above.

## Testing custom extractors

The `TestClient` is the easiest way to test extractors end-to-end:

```python
from lauren.testing import TestClient

c = TestClient(app)

# happy path
r = c.get("/dashboard/", headers={"x-tenant": "acme"})
assert r.status_code == 200
assert r.json() == {"tenant": "acme", "plan": "pro"}

# missing header
r = c.get("/dashboard/")
assert r.status_code == 400
assert r.json()["error"]["code"] == "bad_tenant"

# unknown tenant
r = c.get("/dashboard/", headers={"x-tenant": "ghost"})
assert r.status_code == 400
assert r.json()["error"]["detail"]["id"] == "ghost"
```

For unit-testing an extractor in isolation, construct an `ExecutionContext` directly:

```python
import pytest
from lauren.types import ExecutionContext
from lauren.testing import make_request   # or any Request factory

async def test_bad_tenant_header():
    repo = MockTenantRepository(returns=None)
    extractor = TenantId(repo=repo)

    ctx = ExecutionContext(request=make_request(headers=[("x-tenant", "ghost")]))
    ext = Extraction(name="tenant", source="app.tenant_id", ...)

    with pytest.raises(BadTenantError):
        await extractor.extract(ctx, ext)
```

## Things to avoid

| Don't... | Because... |
|---|---|
| ... use `inspect`, `get_type_hints`, or `typing.get_args` inside `extract()` | The dispatch path is reflection-free. Resolve types at startup, not at request time. |
| ... store per-request state on `self` in a stateless (non-injectable) extractor | The same instance is shared process-wide. Use `execution_context.request.state` for per-request state. |
| ... hand-build a `Response` from inside an extractor | Raise an `HTTPError` instead. Extractors produce *values*; middleware/exception-handlers produce responses. |
| ... inherit `@injectable` from a parent extractor | `@injectable` is **not inherited**. The DI container enforces a strict no-inheritance rule. Re-decorate each subclass with `@injectable`, or keep `extract` stateless so `@injectable` is unnecessary. |

## Discoverability — making extractors part of your stdlib

Custom extractors thrive when each application has a small `extractors.py` module with the project's domain-specific decoders:

```python
# app/extractors.py
class CurrentUser(ExtractionMarker): ...
class TenantId(ExtractionMarker): ...
class IdempotencyKey(ExtractionMarker): ...
class Pagination(ExtractionMarker): ...
```

Now any new handler that wants the current user, tenant, idempotency key, or pagination cursor just imports and annotates. No copy-pasted authorization logic. No "did I forget to fetch the user this time?" bugs.

## See also

* [Core Concepts → Request & Response](../core-concepts/request-response.md) — the `Request` API available via `execution_context.request`.
* [Custom Guards](custom-guards.md) — guards share the same `ExecutionContext`; extractors are about *parsing*, guards are about *allowing or denying*.
* [Custom Exception Handlers](custom-exception-handlers.md) — pair with extractors to turn raised `HTTPError`s into structured responses.
* [Extractors vs Dependencies vs Guards vs Middlewares](../concepts/extractors-vs-dependencies-vs-guards-vs-middlewares.md) — when to use each tool.
