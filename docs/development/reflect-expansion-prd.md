# PRD — Expanding `lauren.reflect` to a Full Metadata Introspection API

**Status:** Draft  
**Date:** 2026-06-10  
**Scope:** `lauren-framework` / `lauren/reflect/`

---

## 1. Problem Statement

`lauren.reflect` (v1.6.0) reads *cross-cutting concern* metadata — guards,
interceptors, middlewares — from a single class's own `__dict__`. That is the
minimum surface needed to run `@use_guards` on `@ws_controller`.

However every major design element of a Lauren application is *already* stored as
stable, structured metadata on the decorated object:

| Lauren object | Metadata attribute | Payload type |
|---|---|---|
| `@module(…)` class | `__lauren_module__` | `ModuleMeta` |
| `@controller(prefix)` class | `__lauren_controller__` | `ControllerMeta` |
| `@get(path)` method | `__lauren_route__` | `list[RouteMeta]` |
| `@injectable(scope=…)` class | `__lauren_injectable__` | `InjectableMeta` |
| `@middleware()` class | `__lauren_middleware__` | `MiddlewareMeta` |
| `@interceptor()` class | `__lauren_interceptor__` | `InterceptorMeta` |
| `@ws_controller(path)` class | `__lauren_ws_controller__` | `WsControllerMeta` |
| `@on_message("event")` method | attached to method | `list[WsMessageMeta]` |
| `@use_guards(…)` | `__lauren_use_guards__` | `list[type]` |
| `@use_interceptors(…)` | `__lauren_use_interceptors__` | `list[type]` |
| `@use_middlewares(…)` | `__lauren_use_middlewares__` | `list[type]` |
| `@set_metadata(key, val)` | `__lauren_metadata__` | `dict[str, Any]` |
| `@use_encoder(enc)` | `__lauren_use_encoder__` | encoder instance |

None of this is currently readable through a public, stable API. Consumers either
import private constants (`CONTROLLER_META`, `ROUTE_META`, …) and access
`__dict__` directly — coupling them to internal implementation details — or they
avoid introspection altogether.

The gap affects several real use cases:

- **Tooling** (linters, doc generators, OpenAPI exporters): walk all routes
  without starting the server.
- **Extension packages** (`lauren-mcp`, `lauren-ai`): decide which methods to
  expose as tools without re-implementing route discovery.
- **Testing / auditing**: verify that a controller's guard set matches a policy,
  or enumerate all routes in a module for contract testing.
- **Developer experience**: `LaurenFactory.create()` already compiles a full
  route table; there is no public API to query it after the fact.

---

## 2. Goals

1. Expose a **stable, typed, public API** (`lauren.reflect.*`) for reading all
   decorator-attached metadata without importing private constants.
2. Support both **static** (pre-startup, class-level) and **runtime** (post-startup,
   compiled) introspection where feasible.
3. Remain **read-only**. No modification of registered metadata through the
   reflect API.
4. Keep the reflect module **import-safe**: no dependency on `_asgi`, `_modules`,
   or `_routing` at import time (same lazy-import discipline as today).
5. Ship **zero new framework magic** — every reader is a thin, typed wrapper
   around already-set `__dict__` attributes.

---

## 3. Non-Goals

- Writing or modifying metadata at runtime (that is the decorator's job).
- Replacing `LaurenFactory.create()` or the existing compile pipeline.
- Generating OpenAPI from reflect output (a separate concern; OpenAPI is already
  built into `_asgi/_openapi.py`).
- Dynamic route registration (out of scope for this PRD).

---

## 4. Proposed API

### 4.1 Class-level readers (static, no app required)

These work on any decorated class before `LaurenFactory.create()` runs. They read
only `cls.__dict__` (own-class rule — no inheritance).

```python
# All importable from lauren.reflect or directly from lauren

reflect_controller(cls) -> ControllerMeta | None
reflect_module(cls)     -> ModuleMeta | None
reflect_injectable(cls) -> InjectableMeta | None
reflect_middleware(cls) -> MiddlewareMeta | None
reflect_interceptor_meta(cls) -> InterceptorMeta | None   # renamed to avoid clash
reflect_ws_controller(cls) -> WsControllerMeta | None

# Already exists (v1.6.0):
reflect_guards(cls)        -> tuple[type, ...]
reflect_interceptors(cls)  -> tuple[type, ...]
reflect_middlewares(cls)   -> tuple[type, ...]
reflect_all(cls)           -> ReflectedMeta
```

### 4.2 Method/handler readers (static)

```python
reflect_routes(cls) -> tuple[ReflectedRoute, ...]
```

Returns one `ReflectedRoute` per `(method, path)` pair across all handlers in
`cls`, assembled from `ROUTE_META` lists.

```python
@dataclass(frozen=True)
class ReflectedRoute:
    method: str              # "GET", "POST", etc.
    path: str                # "/items/{id}"
    full_path: str           # controller prefix + route path, e.g. "/api/items/{id}"
    handler: Callable        # the unbound method
    handler_name: str        # "get_item"
    route_meta: RouteMeta    # raw RouteMeta (summary, tags, response_model, …)
    guards: tuple[type, ...]           # from handler + controller __dict__
    interceptors: tuple[type, ...]
    middlewares: tuple[type, ...]
    user_metadata: dict[str, Any]      # from @set_metadata
    encoder: Any | None                # from @use_encoder
```

```python
reflect_ws_messages(cls) -> tuple[ReflectedWsMessage, ...]
```

```python
@dataclass(frozen=True)
class ReflectedWsMessage:
    event: str
    handler: Callable
    handler_name: str
    message_meta: WsMessageMeta
```

### 4.3 Application-level readers (runtime, requires compiled app)

These operate on a `LaurenApp` / the compiled module graph, available after
`LaurenFactory.create()`.

```python
get_all_routes(app: LaurenApp) -> tuple[ReflectedRoute, ...]
```

Returns all HTTP routes across all controllers registered in the app, in router
registration order. Each entry has a fully-resolved `full_path`.

```python
get_all_ws_gateways(app: LaurenApp) -> tuple[ReflectedWsGateway, ...]
```

```python
@dataclass(frozen=True)
class ReflectedWsGateway:
    controller_cls: type
    path_template: str
    guards: tuple[type, ...]
    interceptors: tuple[type, ...]
    messages: tuple[ReflectedWsMessage, ...]
    on_connect: Callable | None
    on_disconnect: Callable | None
```

```python
get_route_metadata(app: LaurenApp, method: str, path: str) -> ReflectedRoute | None
get_controller_metadata(cls) -> ReflectedController
get_module_metadata(cls) -> ReflectedModule
```

```python
@dataclass(frozen=True)
class ReflectedController:
    cls: type
    prefix: str
    tags: tuple[str, ...]
    guards: tuple[type, ...]
    interceptors: tuple[type, ...]
    middlewares: tuple[type, ...]
    routes: tuple[ReflectedRoute, ...]

@dataclass(frozen=True)
class ReflectedModule:
    cls: type
    controllers: tuple[type, ...]
    providers: tuple[type, ...]
    imports: tuple[type, ...]
    exports: tuple[type, ...]
```

---

## 5. Feasibility Analysis

### 5.1 What already exists

All metadata attributes are set at decoration time and survive import. No runtime
compilation is needed to read them:

- `__lauren_controller__` → `ControllerMeta(prefix, tags, summary, …)` ✓
- `__lauren_route__` on methods → `list[RouteMeta]` ✓
- `__lauren_module__` → `ModuleMeta(controllers, providers, imports, exports)` ✓
- `__lauren_injectable__` → `InjectableMeta(scope, provides, …)` ✓
- `__lauren_ws_controller__` → `WsControllerMeta(path, …)` ✓
- `__lauren_use_guards__` / `__lauren_use_interceptors__` / `__lauren_use_middlewares__` ✓
- `__lauren_metadata__` → `dict[str, Any]` ✓

**Static readers (§4.1, §4.2) are entirely feasible today** — they are thin
`__dict__` lookups wrapped in typed return values.

### 5.2 The `full_path` challenge

`ReflectedRoute.full_path` (controller prefix + route path) requires knowing
which controller a handler belongs to. This is knowable statically when reading
via `reflect_routes(cls)` — the controller is the argument. For
`get_all_routes(app)` it requires walking the compiled module graph, which the
app already has post-startup.

**Risk:** Low. The compiled route table in `_asgi` already holds full paths.
Reading it is a dict traversal.

### 5.3 Application-level readers — coupling concern

`get_all_routes(app)` and `get_route_metadata(app, …)` need access to the
compiled route entries in `_asgi/__init__.py`. Two approaches:

**Option A — Read from compiled state**  
`LaurenApp` exposes a `routes` property (new) that returns the already-built
`list[RouteEntry]` from the internal router. `reflect` reads it and maps to
`ReflectedRoute`. This adds one small `@property` to the public API of `LaurenApp`.

**Option B — Recompute from module graph**  
Walk `app._module_graph.iter_controllers()` and apply `reflect_routes(cls)` to
each. No new `LaurenApp` property needed, but requires importing from `_modules`.

**Recommendation:** Option A. One property is a smaller surface change than
importing `_modules` into `reflect`. The property can also be lazy-cached.

### 5.4 Guard/interceptor inheritance at the route level

HTTP routes accumulate guards from three layers: global (app-level), controller
(`@use_guards` on the class), and route (`@use_guards` on the method). A
`ReflectedRoute` should surface *all three* combined, or separately, to be useful
for auditing.

Proposed: `ReflectedRoute` carries `guards` (combined, in execution order) plus
`controller_guards` and `route_guards` for callers that need provenance.

**Risk:** Low complexity. All three layers are readable statically.

### 5.5 Circular import discipline

`reflect` must not import `_asgi`, `_modules`, `_routing`, or `_di` at module
load time (same rule as today for `_reader.py` / `_composer.py`). Application-
level functions must use lazy imports inside their function bodies.

This is the exact pattern already established for `apply_guards` /
`apply_interceptors` in `_composer.py` and `compile_gateways` / `handle_websocket`
in `_ws_runtime.py`. **No new pattern required.**

### 5.6 Stability of `__lauren_*__` attribute names

These attribute names are effectively public API — they are documented in
`CLAUDE.md`, `AGENTS.md`, and referenced by `lauren-mcp`, `lauren-guards`, and
`lauren-ai`. They have not changed across any v1.x release. Reflect readers
should import the constants from `decorators.py` (already exported in
`decorators.__all__`) rather than repeating the string literals.

---

## 6. Risks & Mitigations

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| `RouteMeta` / `ControllerMeta` shape changes in a future release | Low | Medium | Readers return the raw `Meta` objects directly — callers that hold a `ControllerMeta` see field changes immediately rather than through an opaque wrapper |
| `get_all_routes(app)` called before startup completes | Low | Low | Return empty tuple or raise `StartupError` with a clear message; add a guard on `app._started` flag |
| Name clash: `reflect_interceptor_meta(cls)` vs existing `reflect_interceptors(cls)` | Certain if both exist | Low | Use `reflect_interceptor_meta` for the `@interceptor()` class marker (i.e., "is this class *an* interceptor?") vs `reflect_interceptors` for "which interceptors are *applied to* this class" |
| Breaking change if private `_reader.py` consumers pin to internal shapes | Low | Medium | `reflect/__init__.py` is the stability boundary; `_reader.py`, `_composer.py` remain private |
| `reflect_routes(cls)` on a non-controller silently returns `()` | Certain (by design) | Low | Return type is `tuple[ReflectedRoute, ...]`; empty tuple is correct for undecorated classes |

---

## 7. Phased Implementation Plan

### Phase 1 — Static class and route readers (low risk, high value)

Implement in `reflect/_reader.py` (extend existing module):

- `reflect_controller(cls)` → `ControllerMeta | None`
- `reflect_module(cls)` → `ModuleMeta | None`
- `reflect_injectable(cls)` → `InjectableMeta | None`
- `reflect_ws_controller(cls)` → `WsControllerMeta | None`
- `reflect_routes(cls)` → `tuple[ReflectedRoute, ...]`
- `reflect_ws_messages(cls)` → `tuple[ReflectedWsMessage, ...]`
- `get_controller_metadata(cls)` → `ReflectedController`
- `get_module_metadata(cls)` → `ReflectedModule`

New `reflect/_types.py` module for `ReflectedRoute`, `ReflectedController`,
`ReflectedModule`, `ReflectedWsGateway`, `ReflectedWsMessage`.

Export everything from `reflect/__init__.py` and `lauren/__init__.py::__all__`.

**Tests:** Unit tests in `tests/unit/test_reflect_readers.py`. No app startup
needed; build decorated classes directly.

### Phase 2 — Application-level readers (requires `LaurenApp` cooperation)

Add `LaurenApp.routes: tuple[ReflectedRoute, ...]` property (lazy-cached,
returns empty before startup, populated after).

Implement in `reflect/_app_reader.py` (new, lazy imports only):

- `get_all_routes(app)` → `tuple[ReflectedRoute, ...]`
- `get_all_ws_gateways(app)` → `tuple[ReflectedWsGateway, ...]`
- `get_route_metadata(app, method, path)` → `ReflectedRoute | None`

**Tests:** Integration tests in `tests/integration/test_reflect_app.py`. Build a
small app, call `LaurenFactory.create()`, assert on the returned route set.

### Phase 3 — User-facing metadata reader

- `get_user_metadata(obj, key, default=None)` — reads `@set_metadata` dict from
  a class or method's own `__dict__`.
- `reflect_encoder(cls_or_fn)` — reads `@use_encoder` from own `__dict__`.
- `reflect_exception_handlers(cls_or_fn)` — reads `@use_exception_handlers`.

These are pure `__dict__` lookups; trivial to add but low priority until a
concrete consumer exists.

---

## 8. Open Questions

1. **Should `ReflectedRoute.full_path` be computed lazily** (requiring the app) or
   eagerly (just prefix + path, with no guarantee of absolute correctness for
   sub-mounted apps)? Recommendation: provide both `path` (route-relative) and
   `full_path` (absolute, `None` if computed outside of an app context).

2. **Should `get_all_routes(app)` include routes from mounted sub-apps?**
   `LaurenApp` supports `app.mount("/prefix", sub_app)`. Sub-app routes are not
   in the main router. Recommendation: out of scope for Phase 2; document the
   limitation.

3. **`reflect_injectable` name**: should it return `InjectableMeta | None` or a
   richer `ReflectedInjectable` that also includes `provides`, `scope`, and the
   resolved protocol list? Recommendation: return `InjectableMeta` directly in
   Phase 1 and wrap it in Phase 3 when there is a real consumer.

4. **Naming convention for "is-a" vs "has-a" checks**: `reflect_controller(cls)`
   returns `None` if the class is not a controller — that is the "is-a" check.
   `get_controller_metadata(cls)` returns a richer `ReflectedController` but
   should it raise or return `None` for non-controllers? Recommendation: raise
   `DecoratorUsageError` — callers of `get_controller_metadata` already know they
   have a controller.

---

## 9. Success Criteria

- `lauren-mcp` can remove its own `reflect_guards` / `reflect_interceptors`
  re-implementation and import from `lauren.reflect` exclusively.
- A tool can enumerate every HTTP route in a `@module`-decorated class (with
  full path, HTTP method, guards, and response model) without importing any
  private symbol.
- A running `LaurenApp` exposes its full compiled route table through a single
  public call.
- `prek` / `llms_check` pass — all new public symbols documented in
  `llms-full.txt` and `lauren.__all__`.
- No regression across the existing test suite (~3 200 tests).
