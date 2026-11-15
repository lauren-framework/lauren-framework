# Reflect

The `lauren.reflect` sub-package provides typed readers for every piece of
decorator-attached metadata in a Lauren application, plus the context types and
composing utilities that power native `@use_guards` / `@use_interceptors` support
on `@ws_controller` classes.

The main readers and result types are re-exported from `lauren`; nested
manifest records and composition provenance live in `lauren.reflect`:

```python
from lauren import reflect_routes, get_all_routes, ReflectedRoute, propagate_metadata
from lauren.reflect import reflect_composed, ReflectedComposition
```

---

## WebSocket context types

### `WsConnectionContext`

::: lauren.WsConnectionContext

### `WsMessageContext`

::: lauren.WsMessageContext

### `WsUpgradeRequest`

::: lauren.WsUpgradeRequest

---

## Cross-cutting concern readers

All read from `cls.__dict__` only — own-class rule, no inheritance.

::: lauren.reflect_guards

::: lauren.reflect_interceptors

::: lauren.reflect_middlewares

::: lauren.reflect_all

::: lauren.reflect_exception_handlers

---

## Static class readers

All return `None` or empty tuple for undecorated objects, and read from
`cls.__dict__` only.

::: lauren.reflect_controller

::: lauren.reflect_module

::: lauren.reflect_injectable

::: lauren.reflect_ws_controller

::: lauren.reflect_routes

::: lauren.reflect_ws_messages

---

## Structured class getters

Return rich result types or `None` when the class is not the expected type.

::: lauren.get_controller_metadata

::: lauren.get_module_metadata

---

## User metadata and encoder

::: lauren.reflect_user_metadata

::: lauren.reflect_encoder

### Decorator composition provenance

`reflect_composed(obj)` returns `tuple[ReflectedComposition, ...]` for the
`compose(..., name=...)` bundles applied to a function or class (own `__dict__`
for classes). Each record names the bundle and the decorators inside it, in
written order. See [Typed Metadata, State Keys & Composition](../guides/typed-metadata-and-state.md#composing-decorators).

::: lauren.reflect.reflect_composed

---

## App-level readers

Require a started `LaurenApp` (return empty / `None` before startup).

::: lauren.get_all_routes

::: lauren.get_all_ws_gateways

::: lauren.get_route_metadata

---

## Compiled app manifest (before startup)

`describe_app(app)` reads the compiled graph immediately after
`LaurenFactory.create()` — **no** lifecycle hooks or external connections.
Unlike `get_all_routes(app)`, it works before `app.startup()`:

```python fragment
from lauren.reflect import describe_app

app = LaurenFactory.create(AppModule)
manifest = describe_app(app)
print(manifest.routes[0].guards)
print(manifest.to_json())
```

`app.describe()` is equivalent. `AppManifest` is a frozen result tree with
`app`, `globals`, `modules`, `providers`, `routes`, `websocket_gateways`,
`mounts`, `openapi` and `sections`. Route entries include compiled extractor
parameters and effective middleware / guard / interceptor chains (including
globals). The default renders only metadata keys; optional
`include_metadata_values=True` copies values and redacts nested `Secret`
wrappers. `to_dict()` and `to_json(indent=2)` produce deterministic snapshots.

::: lauren.describe_app

::: lauren.AppManifest

For CI and command-line use, see the [tooling guide](../guides/tooling.md).

---

## Result types

### `ReflectedRoute`

::: lauren.ReflectedRoute

### `ReflectedResponse`

::: lauren.ReflectedResponse

### `ReflectedWsMessage`

::: lauren.ReflectedWsMessage

### `ReflectedController`

::: lauren.ReflectedController

### `ReflectedModule`

::: lauren.ReflectedModule

### `ReflectedWsGateway`

::: lauren.ReflectedWsGateway

### `ReflectedError`

::: lauren.ReflectedError

### `ReflectedComposition`

::: lauren.reflect.ReflectedComposition

---

## Error catalog

Enumerate every error class — lauren's own and the application's — without
hard-coding the list. See [Error Catalog](errors.md#programmatic-discovery).

::: lauren.error_catalog

---

## Composing utilities

Low-level helpers used internally by the WS runtime. Available to extension
packages that need to run guard or interceptor chains outside Lauren's HTTP
pipeline.

::: lauren.reflect.apply_guards

::: lauren.reflect.apply_interceptors
