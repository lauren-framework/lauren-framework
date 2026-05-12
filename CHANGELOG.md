# Changelog

All notable changes to this project will be documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and this project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- **`Query[T]` and `Json[T]` with non-Pydantic struct types** — `msgspec.Struct`
  subclasses and Python `dataclass` types now work correctly as parameter
  annotations, including with `OrjsonEncoder`.  Previously, `Query[PageParams]`
  returned a raw string instead of a `PageParams` instance; `Json[PageParams]`
  returned a raw dict without instantiation.  A bare `params: PageParams`
  annotation now auto-promotes to a JSON body parameter, mirroring the existing
  Pydantic behaviour.  New helpers: `_is_msgspec_struct_type`,
  `_is_dataclass_type`, `_is_struct_type`, `_convert_struct`.

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

- **Integration tests** — tests/integration/test_docs_custom_route_handlers.py — 16 tests covering every code snippet in the guide: instance/static/classmethod
bindings, both @staticmethod/@get orderings, decorators with and without @wraps (including the silent-404 and runtime-500 failure modes), the @feature flag
decorator (flag absent → fallback, flag present → original), class-body if/else conditional, and the retry_on_error custom descriptor. Two gotchas caught
during testing: (a) Python 3.11's inspect.iscoroutinefunction doesn't follow __wrapped__ (only 3.12+ does), so wrapped must explicitly be async def when fn
is async; (b) the env var must be set before the class body executes since decorators run at class-definition time.

### Changed
- Updated CLAUDE.md and contributor docs.
- Enhanced typing across the framework.
- Own-module provider now takes priority in structural Protocol resolution.

- The guide is at docs/guides/custom-route-handlers.md and the index is updated. Here's what it covers:

  Binding styles — three sections with working examples:
  - Instance method (default) — normal DI-injected self
  - @staticmethod — no receiver; DI still works for request-level parameters like Inject(); both decorator orderings shown and explained
  - @classmethod — cls is the controller class, instance still resolved for lifecycle hooks

  Writing your own decorators — the @functools.wraps rule is front and center, with:
  - The minimal skeleton every decorator should follow
  - A !!! warning block showing the two distinct failure modes (silent 404 vs runtime 500) with a clear causal explanation of each
  - A decorator-order diagram and the rule that either order is fine as long as every decorator in the chain uses @wraps

  Environment-conditional implementations — two patterns:
  - A reusable @feature(flag, fallback) decorator that picks an implementation at class-body time with zero per-request overhead, using @functools.wraps to
  carry the route marker across
  - A plain if/else in the class body evaluated at import time for the cleanest zero-overhead approach

  - Custom descriptors (advanced) — the __get__ protocol section documents the three requirements (callable, update_wrapper, __wrapped__) with a concrete
  retry_on_error descriptor example that shows exactly how it connects to Lauren's __get__-based dispatch.

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

[Unreleased]: https://github.com/your-org/lauren/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/your-org/lauren/releases/tag/v1.0.0
