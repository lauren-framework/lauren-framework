# App & Factory

The top-level entry points for creating and running a Lauren application.

### `LaurenFactory`

```python
class LaurenFactory
```

Produces :class:`LaurenApp` instances via the 7-phase pipeline.

#### `LaurenFactory.create`

```python
def create(root_module: type, strict_lifecycle: bool = True, global_middlewares: Iterable[type] | None = None, global_guards: Iterable[type] | None = None, global_interceptors: Iterable[type] | None = None, global_exception_handlers: Iterable[Any] | None = None, global_providers: Iterable[Any] | None = None, max_body_size: int = 1048576, app_state: AppState | None = None, logger: Logger | None = None, openapi_info: dict[str, Any] | None = None, openapi_servers: list[dict[str, Any]] | None = None, openapi_security_schemes: dict[str, Any] | None = None, openapi_url: str | None = None, docs_url: str | None = None, redoc_url: str | None = None, arena: RequestArena | None = None, arena_capacity: int | None = None, json_encoder: JSONEncoder | None = None, signals: SignalBus | None = None, error_format: str = 'default', root_path: str = '', mounts: dict[str, Any] | None = None) -> LaurenApp
```

Build a :class:`LaurenApp` from a root ``@module`` class.

The seven-phase pipeline is logged through ``logger`` (defaults to a
no-op :class:`~lauren.logging.NullLogger` when not provided, so
existing test suites remain silent). Pass
``logger=lauren.logging.default_logger()`` for a production-ready
configuration that auto-detects TTY vs JSON output.

Passing any of ``openapi_url``, ``docs_url``, or ``redoc_url``
exposes the corresponding documentation endpoint. The common
pattern is ``openapi_url='/openapi.json'``, ``docs_url='/docs'``,
``redoc_url='/redoc'`` — matching FastAPI conventions.

``global_guards`` and ``global_exception_handlers`` mirror NestJS's
app-level guards and exception handlers: every request runs through
them after the route is resolved (guards) or whenever the handler
raises (handlers). Per-route ``@use_guards`` and
``@use_exception_handlers`` declarations compose with these globals —
route handlers are tried first, then globals.

### `Lauren`

```python
class Lauren(title: str = 'lauren application', version: str = '1.0.0', description: str | None = None, openapi_url: str | None = '/openapi.json', docs_url: str | None = '/docs', redoc_url: str | None = '/redoc', servers: list[dict[str, Any]] | None = None, security_schemes: dict[str, Any] | None = None, debug: bool = False, max_body_size: int = 1048576, strict_lifecycle: bool = True, app_state: AppState | None = None, logger: Any | None = None, global_middlewares: list[type] | None = None, global_guards: list[type] | None = None, global_interceptors: list[type] | None = None, global_exception_handlers: list[Any] | None = None, global_providers: list[Any] | None = None)
```

A FastAPI-inspired ASGI application over lauren's module pipeline.

All constructor arguments have defaults; the canonical call is simply
``Lauren()``. Routes are added with the verb decorators
(``get`` / ``post`` / ``put`` / ``delete`` / ``patch`` / ``head`` /
``options``), modules are merged with :meth:`include_module`, and
middleware is appended with :meth:`add_middleware`.

#### `Lauren.get`

```python
def get(self, path: str = '/', kw: Any = {}) -> Callable[[Callable[..., Any]], Callable[..., Any]]
```

Register a ``GET`` route. Mirrors FastAPI's ``@app.get``.

#### `Lauren.post`

```python
def post(self, path: str = '/', kw: Any = {}) -> Callable[[Callable[..., Any]], Callable[..., Any]]
```

#### `Lauren.put`

```python
def put(self, path: str = '/', kw: Any = {}) -> Callable[[Callable[..., Any]], Callable[..., Any]]
```

#### `Lauren.patch`

```python
def patch(self, path: str = '/', kw: Any = {}) -> Callable[[Callable[..., Any]], Callable[..., Any]]
```

#### `Lauren.delete`

```python
def delete(self, path: str = '/', kw: Any = {}) -> Callable[[Callable[..., Any]], Callable[..., Any]]
```

#### `Lauren.head`

```python
def head(self, path: str = '/', kw: Any = {}) -> Callable[[Callable[..., Any]], Callable[..., Any]]
```

#### `Lauren.options`

```python
def options(self, path: str = '/', kw: Any = {}) -> Callable[[Callable[..., Any]], Callable[..., Any]]
```

#### `Lauren.include_module`

```python
def include_module(self, module_cls: type) -> None
```

Merge a NestJS-style ``@module`` class into this application.

Every provider declared inside the included module (and anything it
re-exports) becomes visible to the synthetic app-level module. Call
as many times as needed before :meth:`startup`.

#### `Lauren.include_router`

```python
def include_router(self, other: 'Lauren', prefix: str = '') -> None
```

Merge another :class:`Lauren` instance's routes and modules.

Every buffered route on ``other`` is copied into ``self`` with
``prefix`` prepended, and ``other``'s included modules are pulled
in too. Neither instance compiles until :meth:`startup` runs on
``self``. ``other`` must itself be uncompiled.

#### `Lauren.add_middleware`

```python
def add_middleware(self, cls: type) -> None
```

Register a middleware class globally.

The class must be decorated with ``@middleware()`` (defining a
``dispatch(request, call_next)`` coroutine). Middleware runs in
insertion order around every request.

#### `Lauren.add_guard`

```python
def add_guard(self, cls: type) -> None
```

Register a guard class globally.

The class must define ``can_activate(context)``. Global guards
run before any per-route ``@use_guards`` chain on every request.
Equivalent to passing ``global_guards=[...]`` to
:meth:`LaurenFactory.create`.

#### `Lauren.add_interceptor`

```python
def add_interceptor(self, cls: type) -> None
```

Register an interceptor class globally.

The class must be decorated with ``@interceptor``. Global interceptors
run after guards and before route handlers on every request. Equivalent
to passing ``global_interceptors=[...]`` to :meth:`LaurenFactory.create`.

#### `Lauren.add_exception_handler`

```python
def add_exception_handler(self, handler: Any) -> None
```

Register an exception handler globally.

``handler`` must already be decorated with ``@exception_handler``.
It runs whenever a request handler raises an exception that
matches the handler's declared exception tuple, after any
per-route or controller-level handlers have been tried.

#### `Lauren.add_provider`

```python
def add_provider(self, provider: Any) -> None
```

Register a provider globally (visible to every module).

Accepts the same shapes as the module ``providers=`` list: an
``@injectable`` class, a function provider, or the result of
``use_value`` / ``use_class`` / ``use_factory`` / ``use_existing``.
Must be called before the application compiles.

#### `Lauren.on_startup`

```python
def on_startup(self, fn: Callable[[], Any]) -> Callable[[], Any]
```

Register a callable to run during application startup.

Accepts both sync and async callables. Hooks run after the DI
graph is built and after all ``@post_construct`` hooks have fired,
but before any request is dispatched.

#### `Lauren.on_shutdown`

```python
def on_shutdown(self, fn: Callable[[], Any]) -> Callable[[], Any]
```

Register a callable to run during graceful shutdown.

Executes after in-flight requests drain and before
``@pre_destruct`` hooks on singleton providers.

#### `Lauren.startup`

```python
def startup(self) -> Any
```

Compile the application and run all startup hooks.

Safe to call multiple times — subsequent invocations are
no-ops that return the already-compiled :class:`LaurenApp`.
Returns the underlying :class:`LaurenApp` so tests can introspect it.

#### `Lauren.shutdown`

```python
def shutdown(self, drain_timeout: float = 10.0) -> None
```

Gracefully stop the underlying :class:`LaurenApp`.

A no-op if the app never compiled (nothing to shut down).

#### `Lauren.openapi`

```python
def openapi(self) -> dict[str, Any]
```

Return the OpenAPI 3.1 document.

Requires the app to have been compiled (either via :meth:`startup`
or an ASGI request). Raises :class:`LifecycleViolationError` if
called before compilation.

#### `Lauren.routes`

```python
def routes(self) -> list[Any]
```

Return the compiled route list.

Before compilation this reflects buffered routes as a list of
``(method, path, handler)`` tuples; afterwards it returns the
underlying :class:`LaurenApp`'s :class:`RouteEntry` objects for
parity with the classic API.

### `LaurenApp`

```python
class LaurenApp(router: Router, container: DIContainer, module_graph: ModuleGraph, lifecycle: LifecycleScheduler, compiled_handlers: dict[tuple[str, str], CompiledHandler], global_middlewares: list[type], app_state: AppState, strict_lifecycle: bool = True, max_body_size: int = 1048576, logger: Logger | None = None, ws_router: Router | None = None, ws_gateways: dict[str, Any] | None = None, arena: RequestArena | None = None, signals: SignalBus | None = None, error_format: str = 'default', global_guards: list[type] | None = None, global_exception_handlers: list[Any] | None = None, global_interceptors: list[type] | None = None, global_providers: list[Any] | None = None)
```

A compiled, ready-to-serve ASGI application.

Instances of this class are produced exclusively by :meth:`LaurenFactory.create`.

#### `LaurenApp.routes`

```python
def routes(self) -> list[RouteEntry]
```

#### `LaurenApp.openapi`

```python
def openapi(self) -> dict[str, Any]
```

#### `LaurenApp.on_shutdown`

```python
def on_shutdown(self, callback: Callable[[], Any]) -> Callable[[], Any]
```

Register a callback to run during :meth:`shutdown`.

Complements ``@pre_destruct`` by letting callers attach arbitrary
cleanup coroutines (or plain callables) without needing to express
them as DI-scoped providers. Usable as a decorator::

    @app.on_shutdown
    async def flush_buffers() -> None:
        ...

Callbacks run in reverse registration order (LIFO) after in-flight
requests have drained and **before** ``@pre_destruct`` hooks, so
they can use the DI graph if needed.

#### `LaurenApp.mount`

```python
def mount(self, path: str, app: Any) -> None
```

Mount an ASGI sub-application at *path*.

All requests whose path starts with *path* (or equals it exactly) are
forwarded to *app* after stripping the prefix.  The stripped portion
is appended to ``scope["root_path"]`` so the sub-application can
reconstruct absolute URLs correctly.

Mounts are checked in descending prefix-length order so a more-specific
prefix (``/api/v2``) always wins over a shorter one (``/api``).

Example::

    app = LaurenFactory.create(AppModule)
    app.mount("/legacy", legacy_asgi_app)
    app.mount("/files", static_files_app)

The same result can be achieved at build time via
``LaurenFactory.create(..., mounts={"/legacy": legacy_asgi_app})``.

#### `LaurenApp.startup`

```python
def startup(self) -> None
```

#### `LaurenApp.shutdown`

```python
def shutdown(self, drain_timeout: float = 10.0) -> None
```

Gracefully stop the application.

Steps (each logged as an event):

1. Mark the app not-running — no new request scheduling.
2. Drain in-flight requests (up to ``drain_timeout`` seconds).
3. Invoke user-registered ``on_shutdown`` callbacks in reverse order.
4. Run ``@pre_destruct`` hooks in reverse topological order.

Idempotent: concurrent or repeated calls return as soon as the first
shutdown has completed.

#### `LaurenApp.handle`

```python
def handle(self, request: Request) -> Response
```

Dispatch a :class:`Request` through middleware, guards and handler.

Global middlewares run **before** routing so they can intercept every
request — including OPTIONS preflight — regardless of whether a
matching route exists.  Per-route and controller middlewares run after
routing (they need access to the compiled handler).

Every dispatch acquires a :class:`RequestAllocation` bundle from
the app's :class:`RequestArena`. The bundle's ``request_cache``
holds request-scoped DI instances; ``framework_values`` is
re-populated per call with the live ``Request`` so the DI
container can short-circuit ``Request`` lookups; ``kwargs``
holds handler arguments assembled from the extractor plan.

Every container returned from the lease is cleared on exit, so
pooled allocations never leak user data across requests.
