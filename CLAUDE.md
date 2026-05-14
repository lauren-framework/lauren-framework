# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

> This file is read automatically by Claude Code, Cursor, Aider and
> similar coding agents. It encodes the project's conventions so that
> AI-generated patches integrate cleanly without manual review loops.
> Keep it short, opinionated, and actionable. When conventions change,
> update this file **first** — agents trust it as ground truth.

## 0. Commands

`nox` is the canonical task runner (see `noxfile.py`). All sessions install the package
in editable mode first, so no separate `pip install` step is needed.

```bash
# Tests
nox -s tests                        # full suite (unit + integration)
nox -s tests_unit                   # unit only  (tests/unit/)
nox -s tests_integration            # integration only  (tests/integration/)
nox -s coverage                     # tests + coverage report

# Run a single test file / specific test without nox overhead
pip install -e ".[dev]"             # one-time local install
pytest tests/unit/test_di.py -q
pytest tests/ -k "test_module_graph" -q

# Code quality
nox -s lint                         # ruff check (E, F, I rules)
nox -s format                       # ruff format (auto-fix)
nox -s typecheck                    # mypy

# Docs
nox -s docs_serve                   # live-reload at localhost:8000
nox -s docs                         # strict build (fails on warnings)

# Sync public API reference
nox -s llms_check                   # verify llms-full.txt matches lauren.__all__

# Release helpers
nox -s ver_inc -- --minor          # suggest the next minor tag + git tag command
nox -s ver_dec -- --patch          # inspect the previous patch tag
```

## 1. Project Identity

`lauren` is a **metadata-first** Python 3.11+ async web framework. Its
design north stars are:

- **Axum** — every route, extractor and middleware is a value composed
  at startup; the request path is pure traversal.
- **NestJS** — modules, controllers, DI with scopes, lifecycle hooks
  in topological order.
- **FastAPI** — Pydantic-driven validation and automatic OpenAPI.

The framework is intentionally **opinionated and small**. New features
should be justified against the existing mental model before adding
surface area. The current public-API surface is documented exhaustively
in `llms-full.txt` (~25 KB) and a short overview lives in `llms.txt`
(2 KB) per the [llmstxt.org] convention.

[llmstxt.org]: https://llmstxt.org

### Companion packages

The framework ships as part of a small ecosystem:

| Package | Purpose |
|---|---|
| `lauren` | The framework itself (this repo). |
| `lauren-middlewares` | Production-grade middleware: CORS, rate limit, GZip, security headers, request id, trusted hosts, request log, HTTPS redirect, body size limit, timeout. |
| `lauren-logging` | Configurable logging module with processor pipeline, contextvars binding, request-logging middleware, and pluggable backends (stdlib, structlog, console, file, fan-out, queue, …). Three `@classmethod` presets on `LoggingConfig`: `for_development()`, `for_production(backend)`, `for_testing()` → `(config, InMemoryBackend)`. |
| `lauren-guards` | Authentication and authorization guards: `bearer_token`, `jwt_bearer`, `api_key`, `basic_auth`, `oauth2_introspection`, `session_cookie`, `require_authenticated`, `require_roles`, `require_scopes`, `csrf`, `ip_allowlist`. All guards are `@injectable(scope=SINGLETON)`. Extend them via subclassing to add application-specific logic (e.g. a public-route bypass). |

When fixing a logging, middleware or auth concern, **first check whether
the right home is the companion package**, not core. The framework stays
small on purpose; cross-cutting concerns live next door.

## 2. Repository Layout

```
lauren/                      — framework package
├── _app.py                  — high-level orchestration (Lauren class)
├── _arena/                  — per-request allocation arena (private)
├── _asgi/                   — ASGI adapter, OpenAPI, docs
├── _di/                     — dependency injection container + custom providers
├── _lifecycle/              — post_construct / pre_destruct machinery
├── _modules/                — module graph & visibility
├── _routing/                — radix-tree HTTP router
├── _typing/                 — ForwardRef / PEP 563 resolver (private)
├── _ws_runtime.py           — WebSocket dispatch engine
├── _socketio.py             — Engine.IO/Socket.IO adapter (private)
├── decorators.py            — user-facing @controller, @module, @get, …
├── exceptions.py            — error hierarchy (28 classes)
├── extractors.py            — Path/Query/Json/Depends + pipes + custom extractors
├── logging.py               — built-in NestJS-style logger
├── serialization.py         — JSON encoders (stdlib, orjson, msgspec, pydantic)
├── background.py            — BackgroundTasks, TaskHandle (fire-and-forget after response)
├── signals.py               — POSIX signal integration + on_shutdown hooks
├── streaming.py             — StreamingResponse[T], Stream, StreamReader
├── sse.py                   — EventStream, ServerSentEvent, last_event_id
├── socketio.py              — public Socket.IO controller surface
├── testing.py               — TestClient + WsTestClient (in-process ASGI)
├── types.py                 — Request, Response, State, Headers, Scope, …
├── websockets.py            — @ws_controller, @on_message, WebSocket, BroadcastGroup
├── docs.py                  — programmatic access to llms.txt / llms-full.txt
├── llms.txt                 — short overview (llmstxt.org)
├── llms-full.txt            — complete reference for LLM ingestion
└── py.typed                 — PEP 561 marker
tests/
├── unit/                    — isolated unit tests (no ASGI app)
└── integration/             — full-stack tests via TestClient / WsTestClient
docs/                        — MkDocs Material site (mkdocs.yml at repo root)
```

Leading underscore = **private**. Never re-export from these modules
without updating `lauren/__init__.py::__all__` *and* `llms-full.txt`.

## 3. Golden Rules (Non-Negotiable)

1. **Startup validates; runtime dispatches.** Any feature that can
   fail must fail during `LaurenFactory.create(...)`, not on the first
   request. The dispatch path is *not* allowed to call `inspect`,
   `get_type_hints`, or any reflective API. The seven-phase factory
   (module graph → providers → protocol binding → DI compile → router
   compile → lifecycle → readiness) is the choke point — any new
   validation goes there.

2. **Decorators attach metadata; they never rewrite functions.**
   Every decorator in `decorators.py` (and `websockets.py`) sets a
   dunder attribute (e.g. `__lauren_controller__`,
   `__lauren_ws_controller__`) and returns the original object. Do
   NOT wrap, do NOT monkey-patch, do NOT create new function objects.

3. **Strict inheritance.** Subclasses of decorated classes
   (`@injectable`, `@controller`, `@module`, `@middleware()`,
   `@ws_controller`, `@exception_handler`) are NOT automatically of
   the same role. The container raises `MetadataInheritanceError` at
   startup if a non-redeclared subclass is registered. This is
   deliberate — see `docs/core-concepts/inheritance.md` for the full
   reasoning. **Method-level** decorators (`@get`, `@post`,
   `@on_message`, `@post_construct`, …) DO propagate via plain Python
   MRO; that's just attribute lookup.

4. **Type hints are introspection-ready.** Every module starts with
   `from __future__ import annotations`. When you need to inspect
   annotations, always route through `lauren._typing.resolve_type_hints`
   — never call `typing.get_type_hints` directly in framework code.

5. **No global state.** Singletons live inside a `DIContainer`, which
   is itself owned by a `LaurenApp`. Multiple apps must be able to
   coexist in one process (tests rely on this).

6. **Async-first, but not async-only.** Handlers may be sync (`def`) or
   async (`async def`); the dispatch engine adapts.  Sync handlers are
   **automatically offloaded** to a thread pool via
   `anyio.to_thread.run_sync` — they never block the event loop.
   Providers may also be sync or async factories.

7. **Pydantic is optional at runtime** for the core path. Guard every
   `import pydantic` with a try/except and expose a clear error when a
   feature that genuinely needs it is used without it.

8. **The 28-class error catalog is closed.** Adding a new error class
   is a public-API change. New runtime conditions should subclass an
   existing category (`StartupError`, `HTTPError`, `LifecycleError`)
   rather than creating a parallel hierarchy.

→ *See `skills/building-lauren-apps/` for the complete `LaurenFactory` bootstrap pattern and module wiring.*

## 4. Coding Conventions

- **Python version:** target 3.11+; use PEP 604 unions (`X | None`),
  PEP 695 type aliases where they read better than `TypeAlias`.
- **Imports:** `from __future__ import annotations` in every module.
  Std-lib first, third-party second, `.`-relative last.
- **Docstrings:** multi-line triple-double-quote. First line is a
  one-sentence summary; a blank line follows, then narrative with an
  imperative voice. No "Parameters/Returns" unless the shape is
  non-obvious — the type annotation already documents that.
- **Private helpers:** leading underscore in `snake_case`. Private
  classes: leading underscore with `PascalCase`.
- **Error classes:** inherit from `LaurenError`. New errors go in
  `exceptions.py` and are re-exported from `lauren/__init__.py`. Every
  HTTP-mapped error has a stable `code` string.
- **Tests:** one behaviour per test function, AAA shape, pytest
  parametrize for axis-of-variation tables. No `unittest.TestCase`.
- **Line length:** soft 88 (black-compatible); hard 120.

**Choosing the right abstraction:**

| I need to… | Use |
|---|---|
| Block or allow a request (auth, rate-limit) | `CanActivate` guard + `@use_guards` |
| Read/modify raw request or response bytes | `@middleware()` with `call_next` |
| Wrap handler execution (timing, caching, transforms) | `@injectable` interceptor + `@use_interceptors` |
| Inject a typed value into a handler parameter | Custom `Extractor` (`extractors.py`) |
| Share state across a single request | `Scope.REQUEST` injectable |
| Share state for the app lifetime | `Scope.SINGLETON` injectable |
| Override JSON encoder per route or controller | `@use_encoder(OrjsonEncoder())` on method or class |

See `docs/concepts/extractors-vs-dependencies-vs-guards-vs-middlewares.md` for detailed trade-offs.

## 5. How to Add a Feature

```
1. Sketch the API in a new or existing integration test first.
2. Implement the smallest version that makes the test pass.
3. Ensure the change is observable via startup validation — add a
   dedicated exception subclass if a misuse is possible.
4. Update `lauren/__init__.py::__all__` if there's a new public name.
5. Update `README.md`, `llms.txt`, and `llms-full.txt` with the new
   surface. The two `llms*` files are read by AI agents and matter.
6. Add a docs page under `docs/guides/` (or extend an existing one).
7. Run the full suite: `pytest -q`. Every test must pass.
```

→ *See `AGENTS.md §Definition-of-Done` for the merge-readiness checklist. See `skills/building-lauren-apps/` for project scaffolding.*

## 6. How to Add a Decorator

- Sentinel attribute name: `__lauren_<feature>__`. Stored on the
  decoratee, never on a wrapper.
- Validate arguments immediately; raise `DecoratorUsageError` (for
  generic config errors) or the feature-specific config error
  (`ExceptionHandlerConfigError`, `MiddlewareConfigError`,
  `GuardConfigError`) with a message that shows the user the correct
  incantation.
- Reject **bare usage** (`@my_decorator` without parentheses) when the
  decorator takes optional configuration — silent passing of the
  decorated function as the first arg is the leading source of
  silent-broken patches in the codebase. Use `_reject_bare_usage` from
  `decorators.py` as a model.
- Never combine decoration with execution — the decorated object must
  be the same object after decoration.

## 7. WebSocket and SSE Conventions

WebSockets and SSE are first-class peers of HTTP, not bolt-ons.

- **WebSocket gateways** live next to HTTP controllers in modules:
  `@module(controllers=[HTTP_or_WS_class])`. The `@ws_controller(path)`
  decorator is the analogue of `@controller(prefix)` and it auto-marks
  the gateway as `@injectable(scope=Scope.REQUEST)` so each connection
  gets its own instance. Hooks (`@on_connect`, `@on_message("event")`,
  `@on_disconnect`, `@on_error`) attach metadata to the *method*; the
  same strict-inheritance rule applies (subclasses must re-decorate the
  gateway class).
- **Typed messages.** A `@on_message("chat.send")` handler that takes
  `body: Json[ChatMessage]` runs through the same Pydantic validation
  pipeline as HTTP `Json[T]` extractors — the validator is built once
  at startup. Discriminated-union payloads work the same way.
- **Broadcast/rooms.** `BroadcastGroup` is a DI-injectable provider
  with `subscribe / unsubscribe / broadcast / unsubscribe_all`. The
  default in-process implementation is fine for single-worker dev;
  multi-worker production wants a Redis-backed subclass with the same
  surface.
- **SSE = `EventStream`.** A handler returns
  `EventStream(async_iterable, keep_alive=15.0)` and the framework
  frames each yielded item per the HTML living standard. The yielded
  items can be `ServerSentEvent` instances, plain strings, dicts, or
  bytes. `last_event_id(req.headers)` gives access to the
  `Last-Event-ID` header for resumable streams.
- **Typed streaming = `StreamingResponse[T]`.** When the stream is
  homogeneous (same Pydantic model every time) prefer
  `StreamingResponse[T]`: the framework negotiates between SSE,
  NDJSON, and JSON Lines from `Accept`. Use raw `EventStream` only
  when you need explicit control of the SSE envelope.
- **WebSocket connection rejection — three safe patterns.** All three
  are idempotent and produce identical client-visible close codes:
  1. `await ws.close(code=4401); return` — preferred
  2. `raise WebSocketDisconnect("reason", close_code=4401)` — runtime closes
  3. `await ws.close(code=4401); raise WebSocketDisconnect(...)` — safe combination
  The runtime tracks connection state internally and never emits a
  duplicate `websocket.close` ASGI frame regardless of which pattern
  you use.
- **SSE mid-stream client disconnect.** The runtime wraps the streaming
  body loop in a try/except so a client disconnecting mid-stream is
  handled silently. Generators should use `try/finally` for cleanup:
  ```python
  async def producer():
      resource = await acquire()
      try:
          async for item in resource:
              yield ServerSentEvent(data=item)
      finally:
          await resource.release()
  ```

When adding tests for either, drive a real app through
`lauren.testing.WsTestClient` (websockets) or `TestClient` (SSE — the
buffered client returns the entire stream body, which makes assertions
deterministic).

→ *See `skills/building-lauren-streaming/` for copy-paste SSE and WebSocket gateway patterns.*

## 8. Common Pitfalls

- ❌ Calling `get_type_hints` directly — use `_typing.resolve_type_hints`.
  `_safe_type_hints` in `_asgi/__init__.py` has a three-tier fallback:
  `resolve_type_hints` → retry with frame locals → `inspect.get_annotations(eval_str=True)`.
  This ensures handler files can freely use `from __future__ import annotations`.
- ❌ Importing `typing.List / Dict / Optional` — use `list`, `dict`,
  `X | None`.
- ❌ Creating a `dataclass` with `field(default_factory=lambda: X())`
  where `X` is mutable and shared — always use a factory callable.
- ❌ Adding a runtime branch that swallows exceptions — surface them.
- ❌ Silent fallbacks that change semantics. The lenient ForwardRef
  resolver is an exception; it returns a `ForwardRef` so the caller
  can detect the fallback explicitly.
- ❌ Using bare `@decorator` instead of `@decorator()` when the
  decorator takes options — see rule 7 above. The framework rejects
  this loudly via `DecoratorUsageError`; mirror that pattern in any
  new decorator.
- ❌ Forgetting to re-decorate a subclass that inherits from a
  controller / injectable / module / middleware / ws_controller /
  exception_handler — the framework raises
  `MetadataInheritanceError` at startup, not at runtime.
- ❌ Returning a `StreamingResponse` from a handler that uses the
  buffered `TestClient` and expecting per-frame assertions — the
  buffered client materialises the entire body. Use `TestClient` for
  end-state assertions; instantiate the response and iterate it
  manually for chunk-by-chunk inspection.
- ❌ Calling asyncio primitives (e.g. `asyncio.Queue.put_nowait`)
  directly from inside a sync handler. Sync handlers run in a thread
  pool; asyncio objects are not thread-safe. Use
  `asyncio.get_running_loop().call_soon_threadsafe(...)` to schedule
  work back on the event loop.
- ❌ Mutating singleton (`Scope.SINGLETON`) state from a sync handler
  without a lock. Sync handlers can run concurrently across requests.
  Use `threading.Lock` to protect shared mutable state.
- ❌ Passing `Scope.REQUEST` DI instances as `BackgroundTasks.add_task`
  args/kwargs — request-scoped instances are torn down after the handler
  returns, before tasks run. Capture plain values (IDs, strings) or
  `Scope.SINGLETON` instances instead.
- ❌ Expecting `@post_construct` / `@pre_destruct` to fire per-request
  on a controller — `@controller` defaults to `Scope.SINGLETON` (NestJS
  behaviour), so lifecycle hooks fire once at startup/shutdown. If a
  controller needs per-request construction, add
  `@injectable(scope=Scope.REQUEST)` *below* `@controller` (bottom-up
  application means `@injectable` runs first and wins).
- ❌ Extracting the current request from DI when you need `route_template`
  or handler metadata — use `ExecutionContext` injection instead. Any
  handler parameter typed as `ExecutionContext` (from `lauren.types`) is
  automatically provided by the dispatch engine at zero cost; no extractor
  marker is needed.

## 9. Testing Playbook

- **Unit** (`tests/unit/`) — import the specific module, call directly.
  Pure-Python, no event loop unless the module is async-only.
- **Integration** (`tests/integration/`) — build a `@module`, call
  `LaurenFactory.create`, drive via `lauren.testing.TestClient`.
- **WebSocket** — use `lauren.testing.WsTestClient`; the session
  context-manager guarantees the server task is awaited so unhandled
  server-side exceptions surface cleanly into the test.
- **SSE** — return `EventStream` from a handler, drive it through
  `TestClient`, and parse the buffered body. There's a tiny
  `parse_sse_body` helper in `tests/integration/test_sse.py` that's
  sufficient for assertions.
- **Startup-failure tests** — build the module, `LaurenFactory.create`
  inside `pytest.raises(SomeError)`, assert on `detail` keys too.
- **Regression shape:** every bug fix gets a test in the same file as
  its nearest existing neighbour.

→ *See `skills/testing-lauren-apps/` for `TestClient` setup, async test patterns, and mock-provider recipes.*

## 10. Commit Messages

`<scope>: <imperative sentence under 72 chars>`. Scopes: `di`, `asgi`,
`routing`, `ws`, `sse`, `streaming`, `background`, `typing`,
`extractors`, `exceptions`, `tests`, `docs`, `meta`. Example:
`typing: resolve ForwardRef annotations via _typing sub-package`.

## 11. Where to Look First

- `lauren/_asgi/__init__.py` — the request lifecycle, top to bottom.
- `lauren/_di/__init__.py` — provider graph & cycle detection.
- `lauren/_di/custom.py` — `use_value` / `use_class` / `use_factory`
  / `use_existing` recipes, `Token`, `Inject`.
- `lauren/websockets.py` — gateway runtime + `BroadcastGroup`.
- `lauren/sse.py` — `EventStream`, `ServerSentEvent`, framing.
- `lauren/streaming.py` — `StreamingResponse[T]`, `Stream`,
  `StreamReader`, content-negotiation logic.
- `lauren/background.py` — `BackgroundTasks`, `TaskHandle`, `_BG_TASKS_ATTR`.
- `lauren/decorators.py` — every public decorator. The `_reject_bare_usage`
  pattern is the model for any new decorator.
- `lauren/exceptions.py` — full 28-class error catalog. Pick the
  closest existing class before adding a new one.
- `llms-full.txt` — machine-readable reference, the source of truth
  for what the public API is supposed to do.
- `docs/` — long-form prose explanations and conceptual articles.

**Symbol lookup (fastest navigation):** Use `codemap` before grepping — it
returns exact file + line ranges so you only read what you need:

```bash
codemap find "InjectableError"       # → lauren/exceptions.py:45-52
codemap find "resolve_type_hints"    # → lauren/_typing/__init__.py:12-38
codemap show lauren/_di/__init__.py  # full symbol map with line ranges
codemap find "on_connect" --type method
```

When in doubt: grep the tests. They express the invariants more
precisely than any English prose. Around **2120 tests** currently pass
in ~15 seconds.

## 12. Injectable Logger Pattern

All four built-in logger classes (`ConsoleLogger`, `JsonLogger`,
`NullLogger`, `InMemoryLogger`) are decorated with
`@injectable(scope=Scope.SINGLETON, provides=(Logger,))`. This means
any of them can be passed to `global_providers` and will resolve for
any service that declares `log: Logger`:

```python
app = LaurenFactory.create(AppModule, global_providers=[ConsoleLogger])

@injectable()
class MyService:
    log: Logger   # → ConsoleLogger SINGLETON, visible from every module
```

To inject a specific pre-built instance, use `use_value`:

```python
app = LaurenFactory.create(
    AppModule,
    global_providers=[use_value(provide=Logger, value=my_logger_instance)],
)
```

`Lauren` exposes the same via `global_providers=` constructor arg and
`app.add_provider(provider)` imperative method. See
`tests/integration/test_global_providers.py` for the full test suite.
