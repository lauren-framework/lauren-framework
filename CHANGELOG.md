# Changelog

All notable changes to this project will be documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and this project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added — `lauren.reflect` full metadata introspection API

The `lauren.reflect` module was expanded from a narrow WS cross-cutting helper
into a comprehensive read-only metadata introspection API covering every
decorator in the framework.

#### Phase 1 — Static class readers

New functions in `lauren.reflect` (all read from `cls.__dict__` only, own-class
rule, return `None` / empty tuple for undecorated objects):

- `reflect_controller(cls)` → `ControllerMeta | None`
- `reflect_module(cls)` → `ModuleMeta | None`
- `reflect_injectable(cls)` → `InjectableMeta | None`
- `reflect_ws_controller(cls)` → `WsControllerMeta | None`
- `reflect_routes(cls)` → `tuple[ReflectedRoute, ...]` — folds controller prefix into `full_path`
- `reflect_ws_messages(cls)` → `tuple[ReflectedWsMessage, ...]`
- `reflect_exception_handlers(cls_or_fn)` → `tuple[Any, ...]` — reads `@use_exception_handlers`
- `get_controller_metadata(cls)` → `ReflectedController | None`
- `get_module_metadata(cls)` → `ReflectedModule | None`
- `reflect_user_metadata(obj, key=None, default=None)` — reads `@set_metadata` dict
- `reflect_encoder(cls_or_fn)` → encoder instance or `None`

#### Phase 2 — App-level readers

New `lauren.reflect._app_reader` module (no `_asgi` import at load time; uses
duck-typed `getattr` access; returns empty tuple / `None` before startup):

- `get_all_routes(app)` → `tuple[ReflectedRoute, ...]`
- `get_all_ws_gateways(app)` → `tuple[ReflectedWsGateway, ...]`
- `get_route_metadata(app, method, path)` → `ReflectedRoute | None`

#### New result types

New frozen dataclasses in `lauren.reflect._types` (all re-exported from
`lauren.reflect` and `lauren`):

- `ReflectedRoute` — `method`, `path`, `full_path`, `summary`, `response_model`, `tags`, `deprecated`, `handler`
- `ReflectedWsMessage` — `event`, `payload_model`, `summary`, `handler`
- `ReflectedController` — `cls`, `meta`, `guards`, `interceptors`, `middlewares`, `exception_handlers`, `routes`
- `ReflectedModule` — `cls`, `meta`
- `ReflectedWsGateway` — `cls`, `path_template`, `meta`, `guards`, `interceptors`, `middlewares`, `messages`, `owning_module`

---

### Added — `@propagate_metadata`

New decorator in `lauren.decorators` and re-exported from `lauren`:

```python
@propagate_metadata(
    source,
    *,
    guards=True,
    interceptors=True,
    middlewares=True,
    exception_handlers=True,
    encoder=True,
    user_metadata=True,
)
```

Copies Lauren's `@use_*` decorator metadata from `source` to the decorated
target — the `functools.wraps` equivalent for Lauren annotations.

- **List-based metadata** (guards, interceptors, middlewares, exception
  handlers): source entries are prepended before the target's own entries so
  that propagated behaviour runs as the outermost layer.
- **Encoder**: source encoder is copied only when the target has no
  encoder of its own.
- **User metadata** (`@set_metadata` key/value pairs): source dict is merged
  in; target's existing keys take precedence on conflict.
- `source` may be any class or callable decorated with Lauren's `@use_*`
  family.

---

## [1.6.0] - 2026-06-10

### Added — `lauren.reflect`: native guard & interceptor support for WebSocket gateways

Prior to this release, `@use_guards` and `@use_interceptors` had no effect on
`@ws_controller` classes. Applying them required a manual guard loop inside
`@on_connect`, and extension packages (e.g. `lauren-mcp`) had to ship their own
workaround middleware. This release closes that gap by teaching the WS runtime
to read and execute these decorators natively.

#### New module: `lauren.reflect`

- **`lauren/_ws_runtime.py`** — defines `WsConnectionContext` and
  `WsUpgradeRequest` as frozen dataclasses alongside the other WS runtime types.
  These are the context objects passed to guards and interceptors at connection
  time, duck-typed against `ExecutionContext` / `Request` so the same guard class
  works on both HTTP routes and WebSocket gateways without modification.
- **`lauren/reflect/__init__.py`** — new public sub-package with four public
  readers and the two context types re-exported from `_ws_runtime`:
  - `WsConnectionContext` — context object for WS guards/interceptors
  - `WsUpgradeRequest` — read-only view of the HTTP upgrade request
  - `reflect_guards(cls)` — reads `__lauren_use_guards__` from `cls.__dict__` only
  - `reflect_interceptors(cls)` — reads `__lauren_use_interceptors__`
  - `reflect_middlewares(cls)` — reads `__lauren_use_middlewares__`
  - `reflect_all(cls)` → `ReflectedMeta(guards, interceptors, middlewares)` NamedTuple
- **`lauren/reflect/_reader.py`** — own-class-only metadata readers (no inheritance)
- **`lauren/reflect/_composer.py`** — `apply_guards(...)` and
  `apply_interceptors(...)` helpers; used internally by the WS runtime and
  available for extension packages that build custom transports

#### Changes to existing modules

- **`lauren/_ws_runtime.py`** — `compile_gateways()` now populates
  `CompiledGateway.guards`, `.interceptors`, `.middlewares` via `reflect_guards`
  etc.; `handle_websocket()` runs guard checks and wraps `@on_connect` in the
  interceptor chain *before* accepting the connection — any rejection happens with
  close code 1008, before the MCP/WS handshake begins
- **`lauren/_asgi/__init__.py`** — `LaurenFactory.create()` and `LaurenApp`
  accept two new keyword arguments: `global_ws_guards` and
  `global_ws_interceptors`, which prepend to the effective chain for every
  gateway in the application

#### Breaking changes

None. The metadata attributes (`__lauren_use_guards__` etc.) set by
`@use_guards` / `@use_interceptors` on `@ws_controller` classes were previously
ignored at connection time; they are now executed. Code that relied on these
decorators silently doing nothing on WS gateways must be reviewed.

---

## [1.5.0] - 2026-06-07

This release completes a seven-phase initiative to make pydantic an **optional**
dependency of the framework rather than a hard requirement. `pip install lauren`
no longer pulls in `pydantic`, `pydantic-core`, or any Rust-compiled binary.
Users who want pydantic-backed validation install `pip install "lauren[pydantic]"`.

### Phase 1 — Dependency declaration

- **`pydantic>=2.0` moved from `dependencies` to `optional-dependencies`** in
  `pyproject.toml`. `pip install lauren` now installs only `anyio`. Three extras
  are provided:
  - `pip install "lauren[pydantic]"` — adds `pydantic>=2.0`
  - `pip install "lauren[msgspec]"` — adds `msgspec>=0.18`
  - `pip install "lauren[full]"` — installs both

### Phase 2 — Validation abstraction layer

- **`lauren/_validation.py`** — new internal module providing the framework's
  single, uniform interface for struct-type detection, validation dispatch, and
  JSON Schema generation. Requires zero third-party imports at module load time;
  all library imports are lazy inside function bodies.
  - `is_pydantic_model(tp)`, `is_msgspec_struct(tp)`, `is_dataclass(tp)`,
    `is_typeddict(tp)`, `is_json_body_type(tp)` — type-detection predicates
  - `validate_as(tp, data, *, field)` — unified dispatcher; routes to the correct
    backend (pydantic → `model_validate`, msgspec → `msgspec.convert`, dataclass →
    field-by-field construction, TypedDict → key validation + dict passthrough)
  - `json_schema_for(tp)` — delegates to `model_json_schema()` / `msgspec.json.schema()`
    / stdlib dataclass/TypedDict schema builders as appropriate
  - All validation failures raise `ExtractorError` with a `{"field": ..., "errors": [...]}` detail
    dict — callers never see pydantic-specific exception types

### Phase 3 — File-by-file pydantic call-site replacement

Seven framework source files migrated from direct pydantic calls to
`_validation.py`:

- **`lauren/extractors.py`** — `_validate_json()` rewritten to call `validate_as()`;
  direct pydantic `TypeAdapter` construction replaced; `_is_pydantic_model_type()`
  delegates to `is_pydantic_model()`
- **`lauren/streaming.py`** — `_build_adapter()` replaced with `_validation`-based
  detection; stream item serialisation works for all four struct backends; the
  `_PYDANTIC_AVAILABLE` flag is set at import time by probing `sys.modules`
- **`lauren/serialization.py`** — `PydanticEncoder` now raises `RuntimeError` with
  a clear install hint when pydantic is absent, rather than failing at module load;
  `auto_encoder()` correctly falls back to `StdlibJSONEncoder` when neither orjson
  nor msgspec is available
- **`lauren/_asgi/__init__.py`** — request body coercion path uses `validate_as()`;
  `_coerce_streaming_response` uses `encoder.encode_compact()` directly for
  non-pydantic item types
- **`lauren/_asgi/_openapi.py`** — schema generation for request/response models
  delegates to `json_schema_for()`; pydantic `$defs` blocks are flattened into
  `components/schemas` consistently regardless of backend
- **`lauren/_ws_runtime.py`** — WebSocket frame validation uses `validate_as()`
- **`lauren/websockets.py`** — `Json[T]` body extraction in `@on_message` handlers
  uses the unified dispatcher

### Phase 4 — Pydantic-free discriminated unions

- **`Discriminated[A | B, "key"]`** — new public type (exported from `lauren`) that
  routes tagged-union JSON bodies to the correct variant class using only stdlib.
  No pydantic required. Supported variant types: `@dataclass`, `TypedDict`,
  `msgspec.Struct`, and `pydantic.BaseModel`.
  - **Missing discriminator field** → 422 `"missing discriminator field 'key'"`
  - **Unknown tag value** → 422 `"unknown discriminator value '...'"` 
  - **Non-dict payload** → 422 `"expected a JSON object"`
  - Auto-promotion: bare `body: Animal` (no `Json[…]`) is recognised and promoted
    to a JSON body parameter
- **`lauren/_discriminated.py`** — internal module owning `_DiscriminatorMarker`,
  `is_discriminated_union()`, the validation dispatcher, and the OpenAPI schema
  builder for native discriminated unions
- **OpenAPI output**: `oneOf` array + `discriminator.propertyName` +
  `discriminator.mapping` — generated without pydantic for all variant types

### Phase 5 — OpenAPI schema generation without pydantic

- **`GET /openapi.json` now produces complete, valid OpenAPI 3.1 output** for
  endpoints whose models are `@dataclass`, `TypedDict`, `msgspec.Struct`, or
  `Discriminated[…]` — all without pydantic installed.
- Nested model `$ref` deduplication handles both pydantic's embedded `$defs`
  format and the flat-reference format produced by stdlib schema builders.
- Recursive / self-referential dataclass schemas no longer cause infinite
  recursion during spec build.
- `openapi-spec-validator` added to the `test` extra; every OpenAPI test asserts
  the spec is structurally valid.

### Phase 6 — `msgspec` as the preferred pydantic alternative

- **Full feature parity** for `msgspec.Struct` across all framework integration
  points:
  - Request body validation via `msgspec.convert()` with informative error messages
  - Response serialisation via `msgspec.to_builtins()` and `msgspec.json.encode()`
  - `StreamingResponse[T]` item serialisation for `Struct` item types
  - `Stream[T]` / `StreamReader[T]` item validation via `msgspec.convert()`
  - WebSocket payload validation (`@on_message` with `Json[MyStruct]`)
  - OpenAPI schema generation via `msgspec.json.schema()` (requires msgspec>=0.18)
- **`MsgspecEncoder`** — new `JSONEncoder` subclass backed by `msgspec.json.encode`.
  Serialises any Python value; is selected by `auto_encoder()` when msgspec is
  available and orjson is not.

### Phase 7 — Test strategy

- **Four new test tiers** with 50+ tests covering the validation dispatch layer
  across all extras combinations:
  - `tests/unit/test_pydantic_import_guard.py` — verifies every Lauren submodule
    imports cleanly when pydantic and msgspec are absent; confirms `_PYDANTIC_AVAILABLE`
    is `False` in that environment
  - `tests/integration/test_pydantic_optional.py` — module-scoped integration
    tests with pydantic explicitly blocked; covers dataclass endpoints,
    discriminated-union routing, and OpenAPI generation without pydantic
  - `tests/integration/test_pydantic_regression.py` — regression guard: pydantic
    validation, 422 responses, and native discriminated-union dispatch must work
    correctly when pydantic IS installed and after another test module has blocked it
  - `tests/e2e/test_full_stack_e2e.py` — full-stack end-to-end tests driving all
    validator backends (pydantic, dataclass, TypedDict, discriminated unions) in
    one app via `TestClient`
  - `tests/property/test_validation_properties.py` — Hypothesis property tests for
    `validate_as` invariants; skipped gracefully when hypothesis is absent
  - `tests/conftest.py` (repo root) — session-scoped `_preload_lauren` autouse
    fixture that pre-imports Lauren's core modules before any test blocks optional
    dependencies, preventing `_PYDANTIC_AVAILABLE` contamination across modules;
    registers custom pytest markers (`pydantic`, `msgspec`, `dataclass`,
    `typeddict`, `slow`)
- **`hypothesis>=6.0`** added to the `dev` optional-dependency group
- **`test` dependency group** in `pyproject.toml` for CI extras-matrix installs
- **New nox sessions** — `tests_e2e` and `tests_property`; the `tests` session
  now covers all four tiers
- **CI jobs** — `e2e` (Python 3.11–3.14), `property`, and `extras-matrix`
  (bare / pydantic / msgspec / full × Python 3.11–3.13)

## [1.4.2] - 2026-05-28

### Changed

- **`CallHandler.handle()` now always returns a coerced `Response`** —
  Previously interceptors received the raw handler return value (dict, Pydantic
  model, tuple, `None`, etc.) and had to replicate the full `_coerce_to_response`
  dispatch table to work robustly.  The innermost `CallHandler` now wraps a
  coercing shim so every layer of the interceptor chain — including the outermost
  interceptor — always receives a `Response` from `handle()`.  Interceptors can
  safely call `.status_code`, `.body`, `.headers`, `.with_header()`, etc. without
  any `isinstance` guard.

  **Migration:** interceptors that checked `isinstance(result, dict)` or
  `isinstance(result, Response)` before acting must be updated.  To modify JSON
  body content: `json.loads(result.body)` → mutate → `result.with_body(...)`.
  Interceptors that only pass the result through (`return await ch.handle()`) are
  unaffected.

### Fixed

- **Non-callable custom descriptors now work as route handlers: Part 2** —
  Preventive cyclic `__wrapped__` calls in `_unwrap_handler_descriptor`.

## [1.4.1] - 2026-05-22

### Fixed

- **Non-callable custom descriptors now work as route handlers** —
  `_unwrap_handler_descriptor` previously required `callable(descriptor)` to be
  `True`, silently dropping any descriptor that omitted `__call__` (e.g. a
  caching or retry wrapper that only implements `__get__`).  The function now
  walks the full `__wrapped__` chain (set by `functools.update_wrapper` /
  `functools.wraps`) until it finds the innermost callable; that callable is
  used for route-metadata and signature inspection while `__get__` is still
  used for dispatch.  This handles both a single non-callable descriptor and
  arbitrarily deep stacks of them (e.g. `@cache_a` on top of `@cache_b` on top
  of `@get`).  Descriptors that implement `__call__` are unaffected.

## [1.4.0] - 2026-05-21

### Added

- **Generator function providers** — `@injectable()`-decorated generator and
  async generator functions now support a FastAPI-style lifecycle: code before
  `yield` acts as `post_construct` (setup) and code after `yield` acts as
  `pre_destruct` (teardown). The yielded value is the resolved dependency.
  Teardown is invoked automatically when the scope ends:
  - `SINGLETON` — at shutdown via `LifecycleScheduler.run_pre_destruct()`.
  - `REQUEST` — after response is sent via existing ASGI/WS cleanup (`aclose()`
    protocol).
  - `TRANSIENT` — disallowed; raises `StartupError` at registration because
    transient instances are not tracked for cleanup.
  Both sync and async generators are supported. Use `try/finally` in the
  generator for unconditional teardown even when a handler raises.

## [1.3.0] - 2026-05-14

### Added

- **`PydanticEncoder`** — fourth pluggable JSON encoder backed by
  `pydantic-core`'s Rust serializer. Calls `model.model_dump_json()` /
  `TypeAdapter.dump_json(items)` directly, skipping the intermediate Python
  dict produced by `model_dump(mode="json")`. Honours every Pydantic
  serialization rule (`@field_serializer`, `model_config`, `AliasGenerator`).
  Falls back to `StdlibJSONEncoder` transparently for non-Pydantic values.

- **`@use_encoder(encoder_instance)`** — per-route and per-controller encoder
  override. Applies to handler return-value coercion, `EventStream` framing,
  `Response.sse()` dict events, and error responses within the route. Method
  level wins over controller level, which wins over the app-level encoder set
  at `LaurenFactory.create` time. Validated at decoration time: must be called
  with parentheses; passing a non-`JSONEncoder` instance raises
  `DecoratorUsageError` immediately.

### Fixed

- **JSON encoder gaps — all four output paths now use the configured encoder.**
  Previously, `StdlibJSONEncoder` was always used for:
  - *HTTP error responses* — `_error_response()` now receives and passes
    `encoder=` at every call site.
  - *WebSocket `send_json()`* — `WebSocket.__init__` gains `json_encoder=`
    parameter; the `_ws_runtime` passes `app._json_encoder`; `_encode_json()`
    uses the encoder.
  - *SSE events* — `_encode_data()`, `format_sse_event()`,
    `ServerSentEvent.encode()`, and `_frame_event_stream()` each gain an
    optional `encoder=` parameter. `EventStream` injects the app encoder via
    a new `_reframe(encoder)` method called from `_coerce_to_response()`.
  - *`Response.sse()` dict payloads* — the `_wrap()` generator now uses the
    provided encoder or `get_active_encoder()` instead of raw `json.dumps`.

- **`EventStream._clone()` dropped custom attributes** — builder methods such
  as `with_header()` returned a new `EventStream` without `_source`,
  `_keep_alive`, `_keep_alive_comment`, or `_encoder`, causing
  `AttributeError` when `_reframe` was subsequently called. Fixed by
  overriding `_clone()` in `EventStream` to copy these extra fields.

- **`@pre_destruct` sync hooks blocked the event loop indefinitely** — sync
  `@pre_destruct` methods ran inline on the event loop thread; a blocking
  shutdown operation (DB disconnect, file flush, socket close) would freeze
  the entire server with no way for the timeout to intervene. Sync hooks now
  run in a thread pool via `asyncio.to_thread`, keeping the event loop
  responsive and giving `asyncio.wait_for` a real cancellation point. Both
  sync and async hooks now receive identical timeout protection.

- **`Response` subclassing: `__slots__` removed** — `Response` declared
  `__slots__` which prevented subclasses from adding instance attributes
  without declaring their own `__slots__`. Removed to make subclassing
  friction-free; the small per-instance memory trade-off is negligible for
  short-lived response objects.

- **`Response._clone()` preserved subclass type** — `_clone()` hardcoded
  `Response.__new__(Response)`, silently downgrading any subclass instance to
  plain `Response` after a `with_*` builder call. Changed to
  `type(self).__new__(type(self))` so the concrete subclass type is preserved
  through any chain of builder methods.

### Changed

- **`ExtractionMarker` class vars** — `source` and `reads_body` are now
  annotated explicitly as `ClassVar[...]`, making the extractor marker contract
  clearer to both static analysis and IDEs.

- **`lauren/extractors.py` typing coverage** — substantially expanded static
  type information across the extractor pipeline without changing runtime
  behaviour. Added explicit type aliases and protocols for DI resolution,
  request caches, pipe targets, custom extractor call shapes, multipart upload
  caching, and parsed extractor hints; tightened the internal annotations for
  `FieldDescriptor`, `_ParamSpec`, `Extraction`, pipe execution, struct
  conversion, and custom extractor dispatch. The module now passes the full
  repository `mypy` check with the stronger annotations in place.

- **Typed field helper factories** — `PathField()`, `QueryField()`,
  `HeaderField()`, and `CookieField()` now expose their accepted keyword
  arguments via a shared typed kwargs shape instead of `**kwargs: Any`,
  improving IDE completion and static checking for descriptor construction.

### Documentation

- Refreshed the docs and related AI-ingestion files around the extractor typing
  work, including `README.md`, `AGENTS.md`, `CLAUDE.md`, `lauren/llms.txt`,
  `lauren/llms-full.txt`, the guides index, the development docs, and the
  skills index so they match the current post-`v1.2.0` framework surface.
- Expanded the hand-written docs and skills for the post-`v1.2.0` encoder and
  lifecycle changes: app-wide and route-level JSON encoder selection,
  `PydanticEncoder`, SSE / WebSocket encoder propagation, sync lifecycle hooks
  running in worker threads, and the maintainer workflow for deriving docs and
  `CHANGELOG.md` updates from `git --no-pager log -p $(git describe --tags
  --abbrev=0)..HEAD`.

## [1.2.0] - 2026-05-13

### Added

- **Custom `Response` subclasses** — handlers may now return any `Response`
  subclass and the dispatch pipeline preserves the concrete type unchanged.
  Builder methods such as `with_header()` and `with_cookie()` clone the same
  subclass, so domain-specific response types can add factory helpers, extra
  attributes, and streaming bodies safely.

- **Response guides** — added dedicated documentation for custom response
  subclasses, `Response.file()`, and `Response.xml()` to make response shaping,
  downloads, and XML output first-class documented patterns.

### Changed

- **Release version helpers** — `nox -s ver_inc` and `nox -s ver_dec` now
  derive the next semantic version from existing `vX.Y.Z` tags and print
  copy/paste-ready annotated tag commands for release engineers.

## [1.1.0] - 2026-05-12

### Fixed

- **`Query[T]` and `Json[T]` with non-Pydantic struct types** — `msgspec.Struct`
  subclasses and Python `dataclass` types now work correctly as parameter
  annotations, including with `OrjsonEncoder`. Previously, `Query[PageParams]`
  returned a raw string instead of a `PageParams` instance; `Json[PageParams]`
  returned a raw dict without instantiation. A bare `params: PageParams`
  annotation now auto-promotes to a JSON body parameter, mirroring the existing
  Pydantic behaviour. New helpers: `_is_msgspec_struct_type`,
  `_is_dataclass_type`, `_is_struct_type`, `_convert_struct`.

## [1.0.2] - 2026-05-12

### Added

- **Descriptor-based route handlers** — route dispatch now resolves handlers
  through `__get__`, which makes `@staticmethod`, `@classmethod`, decorators
  that preserve `__wrapped__`, environment-conditional handlers, and advanced
  custom descriptors all behave consistently on the request path.

- **Companion-package authoring skill** — added
  `skills/building-companion-packages/` with guidance for building, testing,
  versioning, and publishing first-party or third-party Lauren ecosystem
  packages.

### Fixed

- **`StreamingResponse[T]` with non-Pydantic item types** — Using
  `MsgspecEncoder` or `OrjsonEncoder` with a `StreamingResponse[Greeting]`
  where `Greeting` is a `msgspec.Struct` (or any non-Pydantic type) no longer
  raises `PydanticSchemaError`. Three changes were required:
  1. `lauren/streaming.py` `_build_adapter` now catches any exception from
     `pydantic.TypeAdapter(target)` and caches/returns `None` for non-Pydantic
     types, using `key in _ADAPTER_CACHE` to correctly distinguish "not yet
     cached" from "cached as None".
  2. `lauren/_asgi/__init__.py` `_dump` (inside `_coerce_streaming_response`)
     now passes items directly to `encoder.encode_compact(item)` when no
     Pydantic adapter is available, letting native backends serialise their own
     types without a Pydantic intermediary.
  3. `lauren/types.py` `_json_default` now handles `msgspec.Struct` instances
     (detected via `__struct_fields__`) by converting them to plain dicts,
     fixing the `OrjsonEncoder` fallback path for struct values.

### Changed

- **Documentation and examples** — expanded the custom route handlers and
  implicit-parameter documentation to cover descriptor dispatch, decorator
  ordering with `@functools.wraps`, and non-Pydantic struct extraction.

## [1.0.1] - 2026-05-09

### Fixed
- **Multi-binding with mixed custom provider types** — `use_value`, `use_class`,
  and `use_factory` can now all be registered with `multi=True` for the same
  `provide=` token and will all be collected correctly into `list[T]`.
  Previously the module graph (`lauren/_modules/__init__.py`) stored custom
  providers in a `dict[token → CustomProvider]`, silently discarding every
  provider after the first for a given token. The dict is now
  `dict[token → list[CustomProvider]]` and the ASGI bootstrap registers all
  entries. Additionally, the DI container (`lauren/_di/__init__.py`) now uses
  `id(provider)` as the singleton cache key for multi-binding providers instead
  of the shared `provide=` token, preventing the first registered provider's
  cached value from being returned for all sibling providers.

## [1.0.0] — 2026-05-08

### Added
- **Radix-tree router** — O(depth) dispatch, zero regex overhead.
- **Dependency Injection** — Singleton, Request, and Transient scopes with
  topological lifecycle scheduling (`@post_construct`, `@pre_destruct`).
- **Extractor system** — typed `Path`, `Query`, `Header`, `Cookie`, `Json`,
  `Form`, `Depends`, custom extractors and pipes.
- **Module system** — explicit `imports`/`exports`, DI graph visibility rules.
- **WebSockets** — `@ws_controller`, `@on_connect`, `@on_message`,
  `@on_disconnect`, `BroadcastGroup` (in-process + Redis-extensible).
- **Server-Sent Events** — `EventStream`, `ServerSentEvent`, resumable streams
  via `last_event_id`.
- **Typed streaming** — `StreamingResponse[T]` with content-negotiation (SSE,
  NDJSON, JSON Lines).
- **Socket.IO** — Engine.IO/Socket.IO adapter for real-time pub/sub.
- **Background tasks** — `BackgroundTasks`, `TaskHandle` for fire-and-forget
  work after the response is sent.
- **Signals** — POSIX signal integration, `on_shutdown` hooks.
- **ASGI adapter** — full ASGI 3 compliance; compatible with uvicorn, hypercorn,
  daphne.
- **Sync handler support** — sync handlers are offloaded to a thread pool via
  `anyio.to_thread.run_sync`.
- **OpenAPI 3.1** — auto-generated schema from route decorators and extractors.
- **28-class typed error catalog** — `StartupError`, `HTTPError`,
  `LifecycleError` hierarchies with stable `code` strings.
- **Strict inheritance** — `MetadataInheritanceError` prevents silent
  decorator-inheritance bugs.
- **Guards, Middlewares, Exception Handlers, Interceptors** — pluggable
  cross-cutting concerns.
- **`py.typed`** — PEP 561 inline types.
- **LLM docs** — `llms.txt` (2 KB overview) and `llms-full.txt` (~25 KB
  complete reference) shipped inside the wheel.
- **`TestClient` / `WsTestClient`** — in-process ASGI test clients.

[Unreleased]: https://github.com/lauren-framework/lauren-framework/compare/v1.5.0...HEAD
[1.5.0]: https://github.com/lauren-framework/lauren-framework/releases/tag/v1.5.0
[1.4.2]: https://github.com/lauren-framework/lauren-framework/releases/tag/v1.4.2
[1.4.1]: https://github.com/lauren-framework/lauren-framework/releases/tag/v1.4.1
[1.4.0]: https://github.com/lauren-framework/lauren-framework/releases/tag/v1.4.0
[1.3.0]: https://github.com/lauren-framework/lauren-framework/releases/tag/v1.3.0
[1.2.0]: https://github.com/lauren-framework/lauren-framework/releases/tag/v1.2.0
[1.1.0]: https://github.com/lauren-framework/lauren-framework/releases/tag/v1.1.0
[1.0.2]: https://github.com/lauren-framework/lauren-framework/releases/tag/v1.0.2
[1.0.1]: https://github.com/lauren-framework/lauren-framework/releases/tag/v1.0.1
[1.0.0]: https://github.com/lauren-framework/lauren-framework/releases/tag/v1.0.0
