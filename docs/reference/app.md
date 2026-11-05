# App & Factory

The top-level entry points for creating and running a Lauren application.

## LaurenFactory

::: lauren.LaurenFactory

### `LaurenFactory.create()` parameters

| Parameter | Type | Default | Description |
|---|---|---|---|
| `root_module` | `type` | *(required)* | Root `@module` class |
| `strict_lifecycle` | `bool` | `True` | Raise on double-startup/shutdown |
| `global_middlewares` | `Iterable[type]` | `None` | Middleware classes wrapping every request |
| `global_guards` | `Iterable[type]` | `None` | Guard classes running before route handlers |
| `global_interceptors` | `Iterable[type]` | `None` | Interceptor classes wrapping handler execution |
| `global_exception_handlers` | `Iterable[Any]` | `None` | Exception handlers (`@exception_handler` decorated) |
| `global_providers` | `Iterable[Any]` | `None` | DI providers visible to every module |
| `max_body_size` | `int` | `1_048_576` | Max request body size in bytes |
| `app_state` | `AppState` | `None` | Application-level state container |
| `logger` | `Logger` | `None` | Logger instance (defaults to `NullLogger`) |
| `openapi_info` | `dict[str, Any]` | `None` | OpenAPI info block (`title`, `version`, etc.) |
| `openapi_servers` | `list[dict[str, Any]]` | `None` | OpenAPI servers list |
| `openapi_security_schemes` | `dict[str, Any]` | `None` | OpenAPI security schemes |
| `openapi_url` | `str` | `None` | URL path for OpenAPI JSON endpoint |
| `docs_url` | `str` | `None` | URL path for Swagger UI |
| `redoc_url` | `str` | `None` | URL path for ReDoc |
| `arena` | `RequestArena` | `None` | Pre-built request arena (mutually exclusive with `arena_capacity`) |
| `arena_capacity` | `int` | `None` | Arena pool capacity (mutually exclusive with `arena`) |
| `json_encoder` | `JSONEncoder` | `None` | JSON encoder (`StdlibJSONEncoder`, `OrjsonEncoder`, `MsgspecEncoder`) |
| `signals` | `SignalBus` | `None` | Lifecycle event bus (per-app isolation) |
| `error_format` | `str` | `"default"` | Error envelope: `"default"`, `"rfc9457"` (RFC 9457 problem details) or `"rfc7807"` (legacy problem details) — see [Error Catalog](errors.md#problem-details-rfc-9457) |
| `problem_type_base` | `str` | `None` | Problem-details `type` prefix: `type = base + code` for errors without their own `problem_type` |
| `debug` | `bool \| DebugConfig` | `False` | [Debug error pages](../guides/debugging.md) for unhandled errors (development only; loopback clients by default) |
| `root_path` | `str` | `""` | ASGI root path prefix stripped from requests |
| `mounts` | `dict[str, Any]` | `None` | Pre-built sub-app mounts (`{path: app}`) |
| `sessions` | `SessionConfig` | `None` | Enable signed-cookie sessions |
| `content` | `ContentConfig` | `None` | Request-body content policy (`strict_content_type`) |
| `lazy_startup` | `bool` | `True` | Start on the first HTTP / WebSocket request when the server sent no lifespan events (logs a warning); `False` keeps the app cold until `await app.startup()` |
| `hosted_services` | `bool` | `True` | `False` discovers and validates `@hosted_service` classes but does not start them |
| `scheduler` | `bool` | `True` | `False` keeps scheduled jobs validated (and `SchedulerRegistry.trigger` working) but fires nothing |
| `strict_request_scope` | `bool` | `False` | `True` raises `DIScopeViolationError` (`reason="no_active_scope"`) when a `Scope.REQUEST` provider is resolved outside a request, WebSocket connection or `app.scope()`; the default falls back to an untracked transient and warns once per provider — see [Dependency Injection](../guides/dependency-injection.md#request-scoped-providers-resolved-outside-any-scope) |

### `LaurenFactory.create_context()`

`LaurenFactory.create_context(root_module, *, hosted_services=False, scheduler=False, drain_timeout=10.0, **create_kwargs)`
builds the app with `create()` and returns an
[`ApplicationContext`](commands.md#applicationcontext): `async with` starts it
(without serving HTTP) and shuts it down. Hosted services and the scheduler
are off unless enabled. See [Commands & Scripts](../guides/commands-and-scripts.md).

## Lauren

::: lauren.Lauren

FastAPI-inspired imperative API. Routes are registered via decorator methods (`@app.get`, `@app.post`, etc.) and compiled lazily — by the ASGI lifespan, `await app.startup()`, or the first request. `await app.startup()` only compiles; the ASGI lifespan — and `TestClient`, on entering `with` or on its first request, and an `ApplicationContext` (`ApplicationContext.from_lauren(app)`) — also start the app: singletons are built, their `@post_construct` hooks run, and then the `on_startup` hooks run (before hosted services start and before any request; a failing hook raises `StartupError`).

```python
from lauren import Lauren, Path

app = Lauren(title="My API", version="1.0.0")

@app.get("/items/{item_id}")
async def read_item(item_id: Path[int]) -> dict:
    return {"id": item_id}

# Include NestJS-style modules:
app.include_module(UserModule)
```

## LaurenApp

::: lauren.LaurenApp

### Key attributes

| Attribute | Type | Description |
|---|---|---|
| `router` | `Router` | Radix-tree router |
| `container` | `DIContainer` | Dependency injection container |
| `app_state` | `AppState` | Application-level state |
| `module_graph` | `ModuleGraph` | Compiled module graph |
| `arena` | `RequestArena` | Per-app request pool |
| `json_encoder` | `JSONEncoder` | Active JSON encoder |
| `signals` | `SignalBus` | Lifecycle event bus |
| `error_format` | `str` | `"default"`, `"rfc9457"` or `"rfc7807"` |
| `logger` | `Logger` | Application logger |
| `lifecycle_state` | `AppLifecycleState` | `CREATED`, `STARTING`, `READY`, `DRAINING`, `STOPPED` or `FAILED` — see [Health & Readiness Probes](../guides/health-checks.md#the-lifecycle-state) |

### Key methods

| Method | Description |
|---|---|
| `mount(path, app)` | Mount an ASGI sub-application at `path` |
| `startup()` | Run `@post_construct` hooks (then `Lauren.on_startup` hooks), start hosted services and emit lifecycle events |
| `shutdown(drain_timeout)` | Gracefully stop: drain requests, run `@pre_destruct` |
| `begin_draining(reason="")` | `READY → DRAINING`: readiness fails while requests are still served (idempotent) |
| `handle(request)` | Dispatch a `Request` through middleware, guards, and handler |
| `routes()` | Return compiled route list |
| `openapi()` | Return OpenAPI 3.1 document |
| `on_shutdown(callback)` | Register a shutdown callback (LIFO order) |
| `scope(name, *, values=None)` | Open an `OperationScope` for request-scoped providers outside a request |
| `context(*, hosted_services=False, scheduler=False, drain_timeout=10.0)` | Wrap the built, not yet started app in an [`ApplicationContext`](commands.md#applicationcontext) |
