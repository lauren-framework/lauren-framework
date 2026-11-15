# Reflection & App Manifests

Inspect declarations and compiled application graphs.

## Compiled manifest (before startup)

### `describe_app`

Describe the compiled app before or after its lifecycle starts.

Read the factory's immutable graph. Never resolve providers, invoke
``@post_construct`` hooks or expose user metadata values by default;
``include_metadata_values=True`` retains only JSON-safe snapshots and
redacts :class:`~lauren.config.Secret` values even when nested.

### `AppManifest`

A versioned, deterministic snapshot of an app's compiled graph.

Call :meth:`to_json` to save or diff the manifest; no lifecycle methods or
network connections run while building it. The schema is additive within
one major manifest version.

## Running-app readers

### `get_all_routes`

Return all compiled HTTP routes from *app*.

Reads from ``app._handlers`` (a ``dict[(method, path_template),
CompiledHandler]`` populated during ``LaurenFactory.create()``).

Returns an empty tuple when the app has not started yet.

Example::

    app = LaurenFactory.create(AppModule)
    TestClient(app)  # triggers startup

    for route in get_all_routes(app):
        print(route.method, route.full_path, route.handler.__name__)

### `get_route_metadata`

Return the :class:`~lauren.reflect.ReflectedRoute` for *(method, path)*,
or ``None`` if the route is not registered or the app has not started.

*path* must be the exact path template as registered (e.g.
``"/users/{id}"``), not a concrete URL (e.g. ``"/users/42"``).
For host-routed entries, pass the canonical host pattern as ``host=``;
the returned ``full_path`` remains the path, not the internal lookup key.

Example::

    route = get_route_metadata(app, "GET", "/users/{id}")
    if route is not None:
        print(route.handler.__name__, route.guards)

### `get_all_ws_gateways`

Return all compiled WebSocket gateways from *app*.

Reads from ``app._ws_gateways`` (a ``dict[path_template, CompiledGateway]``
populated during ``LaurenFactory.create()``).

Returns an empty tuple when the app has not started or has no WS gateways.

Example::

    for gw in get_all_ws_gateways(app):
        print(gw.path_template, gw.guards)

## Static readers

### `reflect_routes`

Return all HTTP routes declared on *cls* via ``@get``, ``@post``, etc.

The :attr:`~ReflectedRoute.full_path` of each route combines the
controller prefix (from ``@controller(prefix)``) with the route-relative
path.  For classes without ``@controller`` the prefix is treated as
empty, so ``full_path`` equals ``path``.

Returns an empty tuple for undecorated classes or classes with no route
methods.  Reads only from ``cls.__dict__`` — inherited routes from a
parent class are NOT included.

Example::

    @controller("/users")
    class UserController:
        @get("/{id}")
        async def get_user(self, id: Path[int]): ...

    routes = reflect_routes(UserController)
    routes[0].method     # "GET"
    routes[0].path       # "/{id}"
    routes[0].full_path  # "/users/{id}"

### `reflect_controller`

Return the :class:`~lauren.decorators.ControllerMeta` for *cls*, or
``None`` if the class is not decorated with ``@controller()``.

Reads from ``cls.__dict__`` only — subclasses that do not re-decorate
always return ``None``.

### `reflect_module`

Return the :class:`~lauren.decorators.ModuleMeta` for *cls*, or
``None`` if the class is not decorated with ``@module()``.

Reads from ``cls.__dict__`` only.

### `reflect_injectable`

Return the :class:`~lauren._di.InjectableMeta` for *cls*, or
``None`` if the class is not decorated with ``@injectable()``.

Reads from ``cls.__dict__`` only.

### `reflect_ws_controller`

Return the :class:`~lauren.websockets.WsControllerMeta` for *cls*, or
``None`` if the class is not decorated with ``@ws_controller()``.

Reads from ``cls.__dict__`` only.

### `reflect_ws_messages`

Return all ``@on_message`` declarations on *cls*.

Reads only from ``cls.__dict__`` — inherited messages are NOT included.
Returns an empty tuple when *cls* has no ``@on_message`` methods.

### `reflect_guards`

Return the guard classes declared on *cls* via ``@use_guards``.

Reads from ``cls.__dict__`` only — matches Lauren's "metadata is never
inherited" rule.  Returns an empty tuple when no guards are declared.

Example::

    @use_guards(AuthGuard, RateLimitGuard)
    @ws_controller("/chat")
    class ChatGateway: ...

    reflect_guards(ChatGateway)   # → (AuthGuard, RateLimitGuard)

### `reflect_interceptors`

Return the interceptor classes declared on *cls* via ``@use_interceptors``.

Reads from ``cls.__dict__`` only.  Returns an empty tuple when none are
declared.

### `reflect_middlewares`

Return the middleware classes declared on *cls* via ``@use_middlewares``.

Reads from ``cls.__dict__`` only.  Returns an empty tuple when none are
declared.

### `reflect_all`

Return all cross-cutting metadata declared on *cls*.

Equivalent to calling all three individual readers and bundling the
results into a :class:`ReflectedMeta` named tuple.

Example::

    meta = reflect_all(MyGateway)
    meta.guards          # tuple[type, ...]
    meta.interceptors    # tuple[type, ...]
    meta.middlewares     # tuple[type, ...]

### `reflect_exception_handlers`

Return the exception handler classes/functions attached via
``@use_exception_handlers``.

For class targets reads from ``cls.__dict__`` only (own-class rule).
For function/method targets reads via ``getattr`` (same storage used by
the ``@use_exception_handlers`` decorator for function targets).
Returns an empty tuple when none are declared.

Example::

    @use_exception_handlers(DomainErrorHandler)
    @controller("/api")
    class ApiController: ...

    reflect_exception_handlers(ApiController)  # → (DomainErrorHandler,)

### `reflect_user_metadata`

Return ``@set_metadata`` / typed-key values declared on *obj* (a class or function).

When *key* is ``None`` the full metadata dict is returned (a copy).
A string *key* returns that value, or *default* if absent. A typed key
(:class:`~lauren.MetadataKey` / :class:`~lauren.MetadataSetKey`) returns
its typed value — the key's default, or ``()``, when absent.

Reads from ``obj.__dict__`` for classes (own-class rule) and from
``getattr`` for functions. Values are what *obj* itself declares; the
per-route merge across levels happens in ``LaurenFactory.create``.

Example::

    @set_metadata("rate_limit", 100)
    @controller("/api")
    class ApiController: ...

    reflect_user_metadata(ApiController)                   # {"rate_limit": 100}
    reflect_user_metadata(ApiController, "rate_limit")     # 100
    reflect_user_metadata(ApiController, "missing", 0)    # 0

### `reflect_encoder`

Return the ``@use_encoder`` instance attached to *cls_or_fn*, or
``None`` when not set.

For class targets reads from ``cls.__dict__`` only.
For function/method targets reads via ``getattr``.

Example::

    @use_encoder(OrjsonEncoder())
    @controller("/fast")
    class FastController: ...

    reflect_encoder(FastController)  # OrjsonEncoder instance

### `get_controller_metadata`

Return a :class:`ReflectedController` for *cls*, or ``None`` if
*cls* is not a ``@controller``-decorated class.

The returned object bundles the :class:`~lauren.decorators.ControllerMeta`,
all cross-cutting concern metadata, and a tuple of
:class:`ReflectedRoute` entries.

Example::

    meta = get_controller_metadata(UserController)
    if meta is None:
        raise RuntimeError("not a controller")
    meta.meta.prefix      # "/users"
    meta.routes           # tuple[ReflectedRoute, ...]
    meta.guards           # tuple[type, ...]

### `get_module_metadata`

Return a :class:`ReflectedModule` for *cls*, or ``None`` if *cls* is
not a ``@module``-decorated class.

Example::

    meta = get_module_metadata(AppModule)
    if meta is None:
        raise RuntimeError("not a module")
    meta.meta.controllers   # tuple[type, ...]
    meta.meta.providers     # tuple[type, ...]

### `reflect_composed`

```python
def reflect_composed(obj: Any) -> tuple[ReflectedComposition, ...]
```

Return the :func:`~lauren.compose` applications recorded on *obj*.

Reads ``obj.__dict__`` for classes (own-class rule) and attribute lookup
for functions; entries are in application order (an inner composition
precedes the one that contains it)::

    admin = compose(use_guards(JwtGuard), Roles("admin"), name="admin")

    @controller("/stats")
    class Stats:
        @get("/")
        @admin
        async def show(self) -> dict: ...

    reflect_composed(Stats.show)
    # (ReflectedComposition(name="admin", decorators=("use_guards", "app.roles('admin')")),)

## Result types

### `ReflectedRoute`

A single HTTP route declared via ``@get``, ``@post``, etc.

Returned by :func:`~lauren.reflect.reflect_routes` and
:func:`~lauren.reflect.get_all_routes`.

### `ReflectedWsGateway`

All metadata for a ``@ws_controller``-decorated class, post-startup.

Returned by :func:`~lauren.reflect.get_all_ws_gateways`.

### `ReflectedResponse`

One documented response of a route: a declared success, a ``@raises`` error or a manual entry.

Returned in :attr:`ReflectedRoute.responses_typed`.

### `ReflectedController`

All metadata for a ``@controller``-decorated class.

Returned by :func:`~lauren.reflect.get_controller_metadata`.
Returns ``None`` when the class is not a ``@controller``.

### `ReflectedModule`

All metadata for a ``@module``-decorated class.

Returned by :func:`~lauren.reflect.get_module_metadata`.
Returns ``None`` when the class is not a ``@module``.

### `ReflectedWsMessage`

A single ``@on_message("event")`` declaration on a WS gateway.

Returned by :func:`~lauren.reflect.reflect_ws_messages` and
:func:`~lauren.reflect.get_all_ws_gateways`.

### `ReflectedError`

One error class of the catalog — framework or application.

Returned by :func:`~lauren.reflect.error_catalog`.

### `error_catalog`

Every :class:`~lauren.exceptions.LaurenError` class, as :class:`ReflectedError` entries.

Walks ``LaurenError`` and its subclasses recursively **at call time**
(depth-first, in definition order), so application errors defined so far
are included; ``include_user=False`` keeps only the public classes defined
by lauren itself. Meant for documentation, OpenAPI generation, tooling and
tests — never call it on the request path.
