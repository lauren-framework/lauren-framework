# Custom Extractors

> Extractors decompose a request into typed Python values. Lauren ships nine built-ins (`Path`, `Query`, `Header`, `Cookie`, `Json`, `Form`, `Bytes`, `State`, `Depends`); the rest is up to you. A **custom extractor** is any subclass of `_ExtractorMarker` that implements an async `extract` method. Use them as parameter annotations and you've created a typed, reusable, declarative way to pull domain data into your handlers.

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

Lauren supports two forms. Choose the one that fits your extractor's complexity.

### Classmethod form

```python
from lauren.extractors import _ExtractorMarker
from lauren.exceptions import UnauthorizedError

class CurrentUser(_ExtractorMarker):
    source = "app.current_user"      # any unique string id (used in errors / logs)

    @classmethod
    async def extract(cls, request, extraction, *, container, request_cache):
        uid = request.state.get("user_id")
        if uid is None:
            raise UnauthorizedError("missing auth")
        session = await container.resolve(
            DbSession,
            request_cache=request_cache,
            framework_values={type(request): request},
        )
        user = await session.get(User, uid)
        if user is None:
            raise UnauthorizedError("user vanished")
        return user
```

The classmethod receives:

| Param | Type | What it is |
|---|---|---|
| `request` | `Request` | The current request object. |
| `extraction` | `_Extraction` | Metadata about the parameter (`name`, `default`, `has_default`, ...). |
| `container` | `DIContainer` | The DI container. Prefer the injectable form for service dependencies; use this only for dynamic/conditional resolution. |
| `request_cache` | `dict` | Per-request DI cache. Pass to `container.resolve(...)` if you do use it, to avoid creating duplicate request-scoped instances. |

**Return** the value the handler should receive. **Raise** any `HTTPError` subclass to short-circuit with the matching status.

### Injectable instance-method form

When the extractor needs services injected via the DI container, mark it with `@injectable` and write `extract` as a regular instance method. Constructor dependencies are injected automatically — no manual `container.resolve()` calls inside `extract`:

```python
from lauren import injectable, Scope
from lauren.extractors import _ExtractorMarker
from lauren.exceptions import UnauthorizedError

@injectable(scope=Scope.SINGLETON)
class CurrentUser(_ExtractorMarker):
    source = "app.current_user"

    def __init__(self, session: DbSession) -> None:
        self._session = session

    async def extract(self, request, extraction) -> User:
        uid = request.state.get("user_id")
        if uid is None:
            if extraction.has_default:
                return extraction.default
            raise UnauthorizedError("missing auth")
        user = await self._session.get(User, uid)
        if user is None:
            raise UnauthorizedError("user vanished")
        return user
```

The instance method receives only `request` and `extraction` — the same semantics as the classmethod form, but deps come from `__init__`.

**Rules:**

- Declare the extractor in the module's `providers` list so the DI container can resolve it.
- `@injectable` is **not inherited**. Each subclass must be decorated independently; Lauren raises `StartupError` at startup (not at request time) if an instance-method extractor lacks its own `@injectable`.
- If the extractor is request-scoped, use `Scope.REQUEST`; for process-wide singletons use `Scope.SINGLETON` (the default).

```python
@module(
    providers=[DbSession, CurrentUser],  # extractor must be in providers
    controllers=[ProfileController],
)
class AppModule: ...
```

#### Choosing between the two forms

| | Classmethod form | Injectable instance-method form |
|---|---|---|
| **Dependencies** | None, or dynamic/conditional via `container.resolve(...)` | Declared in `__init__`, injected automatically |
| **Boilerplate** | Minimal for stateless use; verbose when calling `container.resolve()` | None — constructor args declared normally |
| **Startup check** | No | Yes — missing `@injectable` raises `StartupError` |
| **When to use** | Stateless extractors (header/state reads, no services needed) | Anything that depends on a service |

> **Rule of thumb:** if your classmethod `extract` calls `container.resolve(...)`, rewrite it as an injectable instead. The only reason to keep `container` in a classmethod is *dynamic* or *conditional* resolution — resolving different types at runtime based on request content — which constructor injection cannot express.

## Step-by-step: build a `TenantId` extractor

Suppose your service is multi-tenant and every authenticated request carries an `x-tenant` header. The handler shouldn't care about the header lookup or the validation — it just wants a `TenantId`.

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

Since this extractor depends on `TenantRepository`, the injectable form is the right choice:

```python
from lauren import injectable, Scope
from lauren.extractors import _ExtractorMarker
from lauren.exceptions import HTTPError

class BadTenantError(HTTPError):
    status_code = 400
    code = "bad_tenant"

@injectable(scope=Scope.SINGLETON)
class TenantId(_ExtractorMarker):
    source = "app.tenant_id"

    def __init__(self, repo: TenantRepository) -> None:
        self._repo = repo

    async def extract(self, request, extraction):
        raw = request.headers.get("x-tenant")
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

### Cached value within a request

If the extractor is expensive, lean on the `request_cache`. Lauren passes the same cache to every extractor in the same request, so two handlers depending on `CurrentUser` end up running the lookup once.

You can also key your own cache off `request.state` if you need finer control:

```python
class CurrentUser(_ExtractorMarker):
    source = "app.current_user"
    @classmethod
    async def extract(cls, request, extraction, *, container, request_cache):
        cached = request.state.get("__current_user")
        if cached is not None:
            return cached
        # ... resolve once, store on state ...
        request.state.set("__current_user", user)
        return user
```

### Optional values

Use the `extraction.has_default` / `extraction.default` to support `: User | None = None` style:

```python
class CurrentUser(_ExtractorMarker):
    source = "app.current_user"
    @classmethod
    async def extract(cls, request, extraction, *, container, request_cache):
        uid = request.state.get("user_id")
        if uid is None:
            if extraction.has_default:
                return extraction.default
            raise UnauthorizedError("missing auth")
        ...
```

Now both work:

```python
async def me(self, user: CurrentUser) -> dict: ...                 # required
async def search(self, user: CurrentUser | None = None) -> dict: ...  # optional
```

### Composing on top of built-in extractors

If you need a Pydantic-validated body *and* some side-effect, write a small wrapper extractor instead of doing it inline:

```python
class IdempotentCreate(_ExtractorMarker):
    source = "app.idempotent_create"
    @classmethod
    async def extract(cls, request, extraction, *, container, request_cache):
        key = request.headers.get("idempotency-key")
        if not key:
            raise HTTPError("missing idempotency-key", status_code=400)
        # Could check Redis here for a previous response and short-circuit.
        body = await request.json()
        return body, key
```

### Streaming-aware extractors

For large uploads, take the streaming primitives:

```python
class CSVRows(_ExtractorMarker):
    source = "app.csv_rows"
    @classmethod
    async def extract(cls, request, extraction, *, container, request_cache):
        async def rows():
            buf = b""
            async for chunk in request.stream():
                buf += chunk
                while b"\n" in buf:
                    line, _, buf = buf.partition(b"\n")
                    yield line.decode()
        return rows()
```

The handler receives an async iterator and can stream over the upload without buffering it all in memory.

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

## Things to avoid

| Don't... | Because... |
|---|---|
| ... use `inspect`, `get_type_hints`, or `typing.get_args` inside `extract()` | The dispatch path is reflection-free. Resolve types at startup, not at request time. |
| ... store extractor state on the class | Class state is shared across requests — race conditions at scale. Use `request_cache` or `request.state`. |
| ... hand-build a `Response` from inside an extractor | Raise an `HTTPError` instead. Extractors produce *values*; middleware/exception-handlers produce responses. |
| ... resolve request-scoped deps without `request_cache` | They'll be built fresh every time. Always pass `request_cache=request_cache` to `container.resolve(...)`. |
| ... inherit `@injectable` from a parent extractor | `@injectable` is **not inherited**. The DI container enforces a strict no-inheritance rule; a subclass that uses an instance-method `extract` but lacks its own `@injectable` raises `StartupError` at startup. Re-decorate each subclass explicitly, or use the `@classmethod` form which needs no `@injectable`. |

## Discoverability — making extractors part of your stdlib

Custom extractors thrive when each application has a small "extractors" module with the project's domain-specific decoders:

```python
# app/extractors.py
class CurrentUser(_ExtractorMarker): ...
class TenantId(_ExtractorMarker): ...
class IdempotencyKey(_ExtractorMarker): ...
class Pagination(_ExtractorMarker): ...
```

Now any new handler that wants the current user, tenant, idempotency key, or pagination cursor just imports and annotates. No copy-pasted authorization logic. No "did I forget to fetch the user this time?" bugs.

## See also

* [Core Concepts → Request & Response](../core-concepts/request-response.md) — the `Request` API your `extract` method will use.
* [Custom Guards](custom-guards.md) — for authorization decisions; extractors are about *parsing*, guards are about *allowing or denying*.
* [Custom Exception Handlers](custom-exception-handlers.md) — pair with extractors to turn raised `HTTPError`s into structured responses.
