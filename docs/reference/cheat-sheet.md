# Cheat Sheet

A single-page reference of every common pattern in Lauren.

## Application

```python
import asyncio
from lauren import LaurenFactory, module
from lauren.logging import default_logger

@module(controllers=[...], providers=[...])
class AppModule: ...

app = LaurenFactory.create(AppModule, logger=default_logger())
# Serve with uvicorn / hypercorn / granian.
```

## Routing

```python
@controller("/users", tags=["users"])
class UserController:
    @get("/{id}")           async def show(self, id: Path[int]) -> UserOut: ...
    @post("/")              async def create(self, body: Json[CreateUser]) -> tuple[UserOut, int]: ...
    @put("/{id}")           async def replace(self, id: Path[int], body: Json[UserIn]): ...
    @patch("/{id}")         async def update(self, id: Path[int], body: Json[UserPatch]): ...
    @delete("/{id}")        async def destroy(self, id: Path[int]) -> None: ...
    @head("/{id}")          async def head(self, id: Path[int]) -> None: ...
    @options("/")           async def options(self) -> None: ...
```

Multiple route decorators on the same method register multiple routes:

```python
@get("/ping")
@get("/health")
async def ping(self) -> dict: return {"ok": True}
```

## Extractors

```python
async def h(
    self,
    id: Path[int],                                # path parameter
    q: Query[str],                                # query string
    page: Query[int] = QueryField(default=1, ge=1, le=100),
    auth: Header[str] = HeaderField(alias="x-auth"),
    sid: Cookie[str],                             # cookie
    body: Json[CreateUser],                       # JSON body, Pydantic-validated
    form: Form[Login],                            # form-urlencoded
    raw: Bytes,                                   # raw body bytes
    state: State,                                 # request.state
    repo: Depends[UserRepo],                      # DI container
) -> UserOut: ...
```

## Field constraints

```python
QueryField(default=..., ge=1, le=200, gt=0, lt=1000,
           min_length=1, max_length=128, pattern=r"^[a-z]+$",
           alias="other_name")
HeaderField(alias="x-trace-id")
CookieField(default="")
PathField(...)
```

## Injectables

```python
@injectable(scope=Scope.SINGLETON)        # default
@injectable(scope=Scope.REQUEST)
@injectable(scope=Scope.TRANSIENT)

@injectable(provides=[EmailSender])               # Protocol binding
@injectable(provides=[EmailSender], multi=True)   # multi-binding
```

Constructor or class-field injection:

```python
@injectable()
class A:
    def __init__(self, dep: Dep) -> None: ...

@injectable()
class B:
    dep: Dep                                       # equivalent
```

Non-class tokens:

```python
from typing import Annotated
from lauren import Inject, Token

DB_URL = Token("DB_URL")

@injectable()
class C:
    def __init__(self, url: Annotated[str, Inject(DB_URL)]) -> None: ...
```

## Custom providers

```python
from lauren import use_value, use_class, use_factory, use_existing, OptionalDep

@module(providers=[
    use_value(provide=DB_URL, value="postgres://..."),
    use_class(provide=ConfigService, use=ProductionConfig, scope=Scope.SINGLETON),
    use_factory(provide=Engine, factory=make_engine, inject=[DB_URL]),
    use_factory(provide=Pool, factory=make_pool, inject=[DB_URL, OptionalDep("METRICS")]),
    use_existing(provide="LegacyLogger", existing=LoggerService),
])
class AppModule: ...
```

## Modules

```python
@module(
    controllers=[UserController],
    providers=[UserRepo, Db],
    imports=[SharedModule],
    exports=[UserRepo],
)
class UsersModule: ...
```

## Lifecycle hooks

```python
@injectable()
class Db:
    @post_construct
    async def open(self) -> None: ...

    @pre_destruct
    async def close(self) -> None: ...
```

`aclose(self)` on a request-scoped injectable is awaited automatically after each request.

## Guards

```python
from lauren import ExecutionContext, injectable

@injectable()
class AdminGuard:
    async def can_activate(self, ctx: ExecutionContext) -> bool:
        return ctx.request.headers.get("x-role") == "admin"

# Attach:
@use_guards(AdminGuard)
@controller("/admin")
class AdminController: ...

@get("/x")
@use_guards(AdminGuard)
async def x(self): ...

# Parametric:
@get("/x")
@use_guards(RoleGuard)
@set_metadata("required_role", "admin")
async def x(self): ...
```

## Middleware

```python
@middleware
class RequestId:
    async def dispatch(self, request, call_next):
        request.state.rid = uuid.uuid4().hex
        resp = await call_next(request)
        return resp.with_header("x-request-id", request.state.rid)

# Global:
app = LaurenFactory.create(AppModule, global_middlewares=[RequestId])

# Controller / route:
@use_middlewares(Timing)
@controller("/api")
class API: ...

@get("/x")
@use_middlewares(CacheControl)
async def x(self): ...
```

## Interceptors

```python
from lauren import interceptor, use_interceptors, ExecutionContext, CallHandler, Response

@interceptor()
class AuditLog:
    async def intercept(self, ctx: ExecutionContext, call_handler: CallHandler) -> Response:
        response = await call_handler.handle()
        print(f"{ctx.request.method} {ctx.request.path} → {response.status_code}")
        return response

# Global:
app = LaurenFactory.create(AppModule, global_interceptors=[AuditLog])

# Controller / route:
@use_interceptors(AuditLog)
@controller("/api")
class API: ...

@get("/x")
@use_interceptors(TimingInterceptor)
async def x(self): ...
```

Interceptors wrap the **handler** (run after guards, before response is sent). Use them
for cross-cutting logic that needs to read or transform the response — audit logs, timing,
response envelope injection. For transport-layer concerns (auth headers, CORS), use middleware.

## Exception handlers

```python
@exception_handler(NotFoundError, ConflictError)
class DomainErrors:
    def __init__(self, log: Logger) -> None: self.log = log
    async def catch(self, exc, request) -> Response:
        return Response.json({"error": str(exc)}, status=400)

@exception_handler(ValueError)
async def handle_value_error(exc, request) -> Response:
    return Response.json({"detail": str(exc)}, status=422)

# Attach:
@use_exception_handlers(DomainErrors)
@controller("/x")
class X: ...

@get("/y")
@use_exception_handlers(handle_value_error)
async def y(self): ...

# Or globally:
app = LaurenFactory.create(AppModule, global_exception_handlers=[DomainErrors])
```

## Custom extractors

```python
from lauren.extractors import Extraction, ExtractionMarker
from lauren.exceptions import UnauthorizedError
from lauren.types import ExecutionContext

class CurrentUser(ExtractionMarker):
    source = "app.current_user"

    async def extract(
        self,
        execution_context: ExecutionContext,
        extraction: Extraction,
    ) -> object:
        uid = execution_context.request.state.get("user_id")
        if uid is None:
            raise UnauthorizedError("missing auth")
        ...   # lookup uid in DB; raise UnauthorizedError if missing

@get("/me")
async def me(self, user: CurrentUser) -> dict: ...
```

For DI-injected extractors (constructor deps), add `@injectable` and list the class in `providers=`. The `extract` signature is identical:

```python
from lauren import injectable, Scope

@injectable(scope=Scope.REQUEST)
class CurrentUser(ExtractionMarker):
    source = "app.current_user"

    def __init__(self, repo: UserRepository) -> None:
        self._repo = repo

    async def extract(self, execution_context: ExecutionContext, extraction: Extraction) -> User:
        uid = execution_context.request.state.get("user_id")
        return await self._repo.get(uid)
```

## Auto-serialization

| Return | Result |
|---|---|
| `dict` / `list` | JSON 200 |
| `str` | text/plain 200 |
| `None` | 204 No Content |
| Pydantic model | JSON 200 (`model_dump(mode="json")`) |
| `list[BaseModel]` | JSON array |
| `(body, status)` | body + status |
| `(body, status, headers)` | body + status + headers |
| `Response.json(...)` etc. | passed through |

## HTTP errors

```python
from lauren.exceptions import HTTPError

class NotFoundError(HTTPError):
    status_code = 404
    code = "not_found"

raise NotFoundError("user not found", detail={"id": user_id})
```

Renders as:
```json
{"error": {"code": "not_found", "message": "user not found", "detail": {"id": 7}}}
```

## Streaming

```python
# Bytes:
return Response.stream(async_iterable, media_type="application/octet-stream")

# SSE (auto-promote dicts/strings to ServerSentEvent):
return Response.sse(async_iterable_of_events)

# SSE with keep-alive (long-lived browser streams):
from lauren import EventStream, ServerSentEvent
return EventStream(producer(), keep_alive=15.0)
```

## Logging

```python
from lauren.logging import default_logger, ConsoleLogger, JsonLogger, LogLevel

app = LaurenFactory.create(AppModule, logger=default_logger())   # TTY-aware
app = LaurenFactory.create(AppModule, logger=ConsoleLogger(level="DEBUG"))
app = LaurenFactory.create(AppModule, logger=JsonLogger(level=LogLevel.INFO))
```

Env vars: `LAUREN_LOG_LEVEL` (`DEBUG`/`VERBOSE`/`INFO`/`WARN`/`ERROR`), `LAUREN_LOG_FORMAT` (`console`/`json`).

## Graceful shutdown

```python
@app.on_shutdown
async def flush() -> None:
    await metrics.flush()

from lauren.signals import install_signal_handlers, wait_for_shutdown

event = install_signal_handlers(app, drain_timeout=30)
await wait_for_shutdown(event)
```

Phases: drain → `on_shutdown` callbacks (LIFO) → `@pre_destruct` hooks (reverse topo) → goodbye. Idempotent.

## Testing

```python
from lauren.testing import TestClient

c = TestClient(app)
r = c.get("/users/1", headers={"Authorization": "Bearer ..."})
assert r.status_code == 200
assert r.json()["id"] == 1
```

Methods: `get / post / put / patch / delete / options / head / request(...)`. `TestResponse`: `status_code`, `headers`, `body`, `text`, `json()`, `header(name)`, `headers_all(name)`.

## OpenAPI

```python
@get("/users/{id}", response_model=UserOut, operation_id="getUser",
     summary="Fetch a user", tags=["users"])
async def show(self, id: Path[int]) -> UserOut: ...

schema = app.openapi()      # OpenAPI 3.1 dict — feed to Swagger UI / ReDoc
```

## Manual container ops (mostly for tests)

```python
app.container.register(SomeClass)
app.container.compile()
inst = await app.container.resolve(SomeClass)
app.container.set_singleton(Clock, FakeClock())
app.container.has_provider(Clock)        # bool
app.container.get_provider(Clock)        # Provider metadata
```

## AI-readable docs

```python
from lauren import docs
print(docs.llms_full_txt())     # ~25 KB — paste into your AI assistant's context
```
