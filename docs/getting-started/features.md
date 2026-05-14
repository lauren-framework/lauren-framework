# Prominent Features

A guided tour of the flagship features that define Lauren. Each section is a quick conceptual overview; deep-dives live in the [Core Concepts](../core-concepts/index.md) and [Guides](../guides/index.md) sections.

## 1. Radix-tree router with O(depth) lookup

Routes are compiled into a radix tree at startup. Static segments take priority over parameters; parameters take priority over wildcards. Per-method dispatch sets the `Allow` header automatically on `405`.

```python
@controller("/files")
class FilesController:
    @get("/health")          # static — wins
    async def health(self): ...

    @get("/{name}")          # param — second priority
    async def show(self, name: Path[str]): ...

    @get("/*path")           # wildcard — fallback
    async def deep(self, path: Path[str]): ...
```

Two routes with the same `(method, path)` raise `RouterConflictError` at startup — never silently shadowed.

## 2. Three-scope dependency injection

```python
@injectable(scope=Scope.SINGLETON)   # one per app
class Clock: ...

@injectable(scope=Scope.REQUEST)     # one per request, shared in handler tree
class DbSession: ...

@injectable(scope=Scope.TRANSIENT)   # new every resolve
class IdGen: ...
```

Scope rules are enforced at startup:

* `SINGLETON` may depend on `SINGLETON` only.
* `REQUEST` may depend on `SINGLETON` or `REQUEST`.
* `TRANSIENT` may depend on anything.

Violations raise `DIScopeViolationError` — not at runtime, at boot.

## 3. Protocols, multi-bindings, and `list[T]` injection

Bind any number of providers to a `Protocol`, then ask for a single one or all of them:

```python
@runtime_checkable
class EmailSender(Protocol):
    def send(self, to: str, msg: str) -> None: ...

@injectable(provides=[EmailSender], multi=True)
class SmtpSender: ...

@injectable(provides=[EmailSender], multi=True)
class SmsSender: ...

@injectable()
class Dispatcher:
    def __init__(self, senders: list[EmailSender]) -> None:
        self._senders = senders   # exactly the senders, in registration order
```

Multiple providers without `multi=True` raise `ProtocolAmbiguityError` at startup. You can't accidentally bind two implementations to the same scalar token.

## 4. Typed extractors

```python
@get("/items/{id}")
async def show(
    self,
    id: Path[int],                                  # path variable
    fields: Query[list[str]] = QueryField(default=[]),
    auth: Header[str] = HeaderField(alias="x-auth"),
    body: Json[CreateItem] = ...,                   # Pydantic-validated
) -> ItemOut: ...
```

Built-in extractors: `Path`, `Query`, `Header`, `Cookie`, `Json`, `Form`, `Bytes`, `State`, `Depends`, `UploadFile`, `ByteStream`. `Query[T]` and `Json[T]` support Pydantic models, `msgspec.Struct`, and Python dataclasses. Plus **custom extractors** ([guide](../guides/custom-extractors.md)) — implement `extract` once and use the type as a parameter annotation forever.

## 5. Modules with explicit `imports` / `exports`

```python
@module(providers=[Clock], exports=[Clock])
class SharedModule: ...

@module(
    controllers=[UserController],
    providers=[UserRepo, DbSession],
    imports=[SharedModule],         # imports SharedModule's exports
)
class AppModule: ...
```

Visibility is **explicit**: a provider is reachable only if declared in this module or imported from another module's `exports`. Import cycles raise `CircularModuleError` at startup.

## 6. Lifecycle hooks in topological order

```python
@injectable()
class Db:
    @post_construct
    async def connect(self) -> None: ...

    @pre_destruct
    async def disconnect(self) -> None: ...
```

`@post_construct` runs in **topological order** (deps first). `@pre_destruct` runs in **reverse topological order** at shutdown, with bounded timeouts. Failures are collected and reported, never aborting the rest of teardown.

## 7. Auto-serialization of handler returns

Return whatever feels right; Lauren builds the `Response`:

```python
async def h1(self) -> dict:        return {"ok": True}              # JSON 200
async def h2(self) -> UserOut:     return UserOut(id=1, name="x")   # Pydantic → JSON 200
async def h3(self) -> list[UserOut]: return [u1, u2]                # JSON array
async def h4(self):                return {"id": 1}, 201            # body + status
async def h5(self):                return {"q": True}, 202, {"x-q": "default"}
async def h6(self) -> None:        return None                      # 204 No Content
async def h7(self):                return Response.html("<h1>hi</h1>")  # raw Response
```

The default JSON encoder handles Pydantic models, enums, datetimes, UUIDs, `Decimal`, `pathlib.Path`, sets, dataclasses, and `msgspec.Struct` instances out of the box. `Response` subclasses also pass through unchanged, so you can add domain-specific response types without fighting the runtime.

You can replace the app-wide encoder with `StdlibJSONEncoder`, `OrjsonEncoder`,
`MsgspecEncoder`, or `PydanticEncoder`:

```python
from lauren.serialization import PydanticEncoder

app = LaurenFactory.create(AppModule, json_encoder=PydanticEncoder())
```

That encoder is used consistently for normal JSON responses, structured error
payloads, `Response.sse(...)`, `EventStream`, and `WebSocket.send_json(...)`.
For one controller or route, use `@use_encoder(...)` to override the app
default locally.

## 8. Strict decorator inheritance

Subclasses of `@injectable` / `@controller` / `@module` / `@middleware()` classes are **not** automatically of the same role. You must opt in.

```python
@injectable()
class Base: ...

class Child(Base):
    pass    # registering this raises MetadataInheritanceError

@injectable()
class ChildOK(Base):
    pass    # explicit opt-in — fine
```

This is one of Lauren's most opinionated calls — and one developers thank us for after their first surprise refactor. See [Class Inheritance Rules](../core-concepts/inheritance.md).

## 9. Onion-model middleware + class/route guards

```python
@middleware()
class RequestId:
    async def dispatch(self, request, call_next):
        request.state.rid = uuid.uuid4().hex
        resp = await call_next(request)
        return resp.with_header("x-request-id", request.state.rid)

# Global, controller, or route-level — pick your scope:
app = LaurenFactory.create(AppModule, global_middlewares=[RequestId])

@use_middlewares(AuthMiddleware)
@controller("/private")
class P: ...
```

Guards work the same way:

```python
@use_guards(AdminGuard)
@controller("/admin")
class AdminController:
    @get("/purge")
    @use_guards(SuperAdminGuard)         # composes; AdminGuard runs first
    async def purge(self): ...
```

## 10. Interceptors — wrap the handler, not the transport

Interceptors run **around** the handler (after guards, before the response is sent) and
receive a `CallHandler` so they can observe or mutate both the inbound context and the
outbound response. They compose with `@use_interceptors` at the global, controller, or
route level — same scoping rules as guards.

```python
from lauren import interceptor, use_interceptors, ExecutionContext, CallHandler, Response

@interceptor()
class AuditLog:
    async def intercept(self, ctx: ExecutionContext, call_handler: CallHandler) -> Response:
        response = await call_handler.handle()
        # response is available here — inspect or wrap it
        print(f"[audit] {ctx.request.method} {ctx.request.path} → {response.status_code}")
        return response

# Global:
app = LaurenFactory.create(AppModule, global_interceptors=[AuditLog])

# Controller or route:
@use_interceptors(AuditLog)
@controller("/api")
class API: ...
```

Interceptors execute **after** guards and see the real response, unlike middleware which wraps the entire transport layer. Full guide: [Interceptors](../guides/interceptors.md).

## 11. Custom exception handlers

Catch domain errors with class-form (DI-injected) or function-form handlers:

```python
@exception_handler(NotFoundError, ConflictError)
class DomainErrors:
    def __init__(self, log: Logger) -> None:
        self.log = log
    async def catch(self, exc, request) -> Response:
        return Response.json({"error": str(exc)}, status=400)

@exception_handler(ValueError)
async def handle_value_error(exc, request) -> Response:
    return Response.json({"detail": str(exc)}, status=422)
```

Attach with `@use_exception_handlers(...)` per controller / route, or globally via `LaurenFactory.create(..., global_exception_handlers=[...])`. Full guide: [Custom Exception Handlers](../guides/custom-exception-handlers.md).

## 12. Custom providers (NestJS-style recipes)

When `@injectable` isn't enough — environment-conditional swaps, externally-built objects, alias tokens — Lauren ships the four NestJS recipes:

```python
from lauren import use_value, use_class, use_factory, use_existing, Token

DB_URL = Token("DB_URL")

@module(providers=[
    use_value(provide=DB_URL, value="postgres://..."),
    use_class(provide=ConfigService, use=ProductionConfigService),
    use_factory(provide="CONNECTION", factory=make_conn, inject=[DB_URL]),
    use_existing(provide="LegacyLogger", existing=LoggerService),
])
class AppModule: ...
```

Full guide: [Custom Providers](../guides/custom-providers.md).

## 13. OpenAPI 3.1 generation

```python
@get("/users/{id}", response_model=UserOut, operation_id="getUser", tags=["users"])
async def show(self, id: Path[int]) -> UserOut: ...

# Then:
schema = app.openapi()    # dict; serve at /openapi.json or feed to Swagger UI / ReDoc
```

Field descriptors emit constraints (`ge`, `le`, `pattern`, `alias`, ...) into the parameter schema. Pydantic response models become `components.schemas` references.

## 14. Structured logging — Console or JSON

```python
from lauren.logging import default_logger, ConsoleLogger, JsonLogger, LogLevel

# TTY-aware default + LAUREN_LOG_LEVEL / LAUREN_LOG_FORMAT env vars:
app = LaurenFactory.create(AppModule, logger=default_logger())

# Or pick explicitly:
app = LaurenFactory.create(AppModule, logger=JsonLogger(level=LogLevel.INFO))
```

Per-request traces fire at `DEBUG` for 2xx/3xx, `WARN` for 4xx, `ERROR` for 5xx. Production runs at `INFO` stay quiet unless something wants attention.

```
[Lauren] 18:22:01.123  INFO  [LaurenFactory]  Starting application (root=AppModule)
[Lauren] 18:22:01.124  INFO  [RouterExplorer] Mapped {GET /users/{id}} → UserController.show
[Lauren] 18:22:01.124  INFO  [LaurenApp]      Application ready (1.2ms)  routes=12
[Lauren] 18:22:01.240  WARN  [Request]        GET /users/999 404 2.1ms → UserController.show
[Lauren] 18:22:01.314  INFO  [Shutdown]       Shutdown complete. Goodbye.
```

## 15. Graceful shutdown with signals

```python
from lauren.signals import install_signal_handlers, wait_for_shutdown

@app.on_shutdown
async def flush_metrics():
    await metrics_client.flush()

event = install_signal_handlers(app, drain_timeout=30)
await wait_for_shutdown(event)
```

Four phases, all logged: drain → `on_shutdown` callbacks → `@pre_destruct` hooks → goodbye. Idempotent — concurrent calls return once the first drain has completed.

## 16. WebSockets, SSE, and Socket.IO

* **WebSockets** — `@ws_controller` gateways with `@on_connect`, `@on_message("event")`, and `@on_disconnect` hooks; typed Pydantic frames; `BroadcastGroup` for room-scoped fan-out.
* **Server-Sent Events** — `Response.sse(async_iter)` or `EventStream` with `keep_alive=N` for long-lived browser streams and `Last-Event-ID` resumability.
* **Socket.IO** — Engine.IO v4 / Socket.IO v5 adapter that lets the official `socket.io-client` connect with no glue.

## 17. Typed bidirectional streaming

`Stream[T]` (inbound) and `StreamingResponse[T]` (outbound) form a symmetrical
streaming primitive. The same wire-format vocabulary (`text/event-stream`,
`application/x-ndjson`, `application/json+stream`) is honoured in both directions:

```python
from lauren import Stream, StreamingResponse

@post("/transcribe")
async def transcribe(
    self, audio: Stream[AudioChunk]
) -> StreamingResponse[Transcript]:
    async def produce():
        async for chunk in audio:         # validated AudioChunk
            yield Transcript(text=chunk.text.upper(), confidence=0.95)
    return produce()
```

Content negotiation is automatic from the `Accept` header. The OpenAPI document
carries `x-streaming: true` and lists all three negotiable content types.

## 18. Lifecycle event bus

`SignalBus` is an in-process typed pub/sub system that fires at well-known lifecycle
points — startup, per-request, background tasks, shutdown — without coupling user
code to framework internals:

```python
from lauren.signals import RequestComplete

@app.signals.on(RequestComplete)
def on_complete(event: RequestComplete) -> None:
    metrics.record("request.duration", event.duration_s, tags={"status": event.status})
```

Listeners are sync or async. Errors are logged but never propagate out of `emit`.
A listener on `LifecycleEvent` receives every event (firehose). MRO dispatch means
base-class subscriptions are first-class.

## 19. AI-ready documentation (`llms.txt` / `llms-full.txt`)

Lauren ships an [llms.txt](https://llmstxt.org)-format overview and a complete LLM-ready reference at the package root, also available programmatically:

```python
from lauren import docs
print(docs.llms_full_txt())     # ~25 KB — paste into any AI assistant
```

Coding agents (Claude, Cursor, Aider) can ingest the full reference and produce idiomatic Lauren code on the first try.

---

## Where to dive next

| Want to... | Go to |
|---|---|
| Understand modules, controllers, injectables | [Core Concepts](../core-concepts/index.md) |
| Write a custom extractor | [Custom Extractors](../guides/custom-extractors.md) |
| Add an authorization guard | [Custom Guards](../guides/custom-guards.md) |
| Write request-tracing middleware | [Custom Middleware](../guides/custom-middleware.md) |
| Add cross-cutting response logic | [Interceptors](../guides/interceptors.md) |
| Handle a domain error | [Custom Exception Handlers](../guides/custom-exception-handlers.md) |
| Compare to FastAPI / Litestar / BlackSheep | [Comparisons](../comparisons/python-frameworks.md) |
