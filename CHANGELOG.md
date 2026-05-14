# Changelog

All notable changes to this project will be documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and this project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **`Response.file(path, …)` — async file streaming** — new `async classmethod`
  that opens a file with `anyio.open_file` (non-blocking), auto-detects MIME
  type via `mimetypes.guess_type`, streams in configurable chunks (default
  64 KB), and sets `Content-Disposition: attachment` (or `inline` when
  `inline=True`) with a configurable filename. Raises `FileNotFoundError` for
  missing paths.

- **`Response.xml(data, …)` — XML response factory** — convenience classmethod
  that sets `Content-Type: application/xml`. Accepts `str` (UTF-8 encoded) or
  `bytes`.

- **`PydanticEncoder`** — fourth pluggable JSON encoder backed by
  `pydantic-core`'s Rust serializer. Calls `model.model_dump_json()` /
  `TypeAdapter.dump_json(items)` directly, skipping the intermediate Python
  dict produced by `model_dump(mode="json")`. Honoures every Pydantic
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

- Added `docs/guides/file-responses.md` covering `Response.file()`,
  `Response.xml()`, MIME detection, path traversal safety, and
  inline-vs-attachment disposition.
- Added `docs/guides/custom-responses.md` covering `Response` subclassing,
  adding instance attributes, builder-method type preservation, streaming
  bodies, and interceptor integration.
- Refreshed the README, MkDocs pages, AI-agent guides, skills index, and
  `llms*.txt` references so they reflect the current `v1.2.0` framework
  surface, release workflow, and companion-package ecosystem.

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

[Unreleased]: https://github.com/lauren-framework/lauren-framework/compare/v1.2.0...HEAD
[1.2.0]: https://github.com/lauren-framework/lauren-framework/releases/tag/v1.2.0
[1.1.0]: https://github.com/lauren-framework/lauren-framework/releases/tag/v1.1.0
[1.0.2]: https://github.com/lauren-framework/lauren-framework/releases/tag/v1.0.2
[1.0.1]: https://github.com/lauren-framework/lauren-framework/releases/tag/v1.0.1
[1.0.0]: https://github.com/lauren-framework/lauren-framework/releases/tag/v1.0.0
