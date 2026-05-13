# Changelog

All notable changes to this project will be documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and this project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Documentation

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
