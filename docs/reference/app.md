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
| `error_format` | `str` | `"default"` | Error envelope: `"default"` or `"rfc7807"` |
| `root_path` | `str` | `""` | ASGI root path prefix stripped from requests |
| `mounts` | `dict[str, Any]` | `None` | Pre-built sub-app mounts (`{path: app}`) |

## Lauren

::: lauren.Lauren

FastAPI-inspired imperative API. Routes are registered via decorator methods (`@app.get`, `@app.post`, etc.) and compiled lazily on first request or explicit `startup()`.

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
| `error_format` | `str` | `"default"` or `"rfc7807"` |
| `logger` | `Logger` | Application logger |

### Key methods

| Method | Description |
|---|---|
| `mount(path, app)` | Mount an ASGI sub-application at `path` |
| `startup()` | Run `@post_construct` hooks and emit lifecycle events |
| `shutdown(drain_timeout)` | Gracefully stop: drain requests, run `@pre_destruct` |
| `handle(request)` | Dispatch a `Request` through middleware, guards, and handler |
| `routes()` | Return compiled route list |
| `openapi()` | Return OpenAPI 3.1 document |
| `on_shutdown(callback)` | Register a shutdown callback (LIFO order) |
