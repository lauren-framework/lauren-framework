# Reflect

The `lauren.reflect` sub-package provides typed readers for every piece of
decorator-attached metadata in a Lauren application, plus the context types and
composing utilities that power native `@use_guards` / `@use_interceptors` support
on `@ws_controller` classes.

All symbols are re-exported from the top-level `lauren` namespace:

```python
from lauren import reflect_routes, get_all_routes, ReflectedRoute, propagate_metadata
# or
import lauren.reflect as reflect
```

---

## WebSocket context types

### `WsConnectionContext`

::: lauren.WsConnectionContext

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

---

## App-level readers

Require a started `LaurenApp` (return empty / `None` before startup).

::: lauren.get_all_routes

::: lauren.get_all_ws_gateways

::: lauren.get_route_metadata

---

## Result types

### `ReflectedRoute`

::: lauren.ReflectedRoute

### `ReflectedWsMessage`

::: lauren.ReflectedWsMessage

### `ReflectedController`

::: lauren.ReflectedController

### `ReflectedModule`

::: lauren.ReflectedModule

### `ReflectedWsGateway`

::: lauren.ReflectedWsGateway

---

## Composing utilities

Low-level helpers used internally by the WS runtime. Available to extension
packages that need to run guard or interceptor chains outside Lauren's HTTP
pipeline.

::: lauren.reflect.apply_guards

::: lauren.reflect.apply_interceptors
