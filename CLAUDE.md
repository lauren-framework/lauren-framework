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
nox -s tests                        # full suite (unit + integration + e2e + property)
nox -s tests_unit                   # unit only  (tests/unit/)
nox -s tests_integration            # integration only  (tests/integration/)
nox -s tests_e2e                    # end-to-end only  (tests/e2e/)
nox -s tests_property               # Hypothesis property tests (tests/property/)
nox -s coverage                     # tests + branch coverage; fails below 90 %

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
nox -s docs_contract                # docs-as-tests: compile doc code blocks, run `run` blocks,
                                    # check docs_contract.toml claims + generated facts
python scripts/sync_doc_facts.py    # regenerate fact regions (error catalog table/tree)

# Release helpers (full procedure: §13 Releasing)
nox -s ver_inc -- --minor          # print the next tag after the latest v* tag + the tag/push commands
nox -s ver_dec -- --patch          # inspect the previous patch tag
```

## 1. Project Identity

`lauren` is a **metadata-first** Python 3.11+ async web framework. Its
design north stars are:

- **Axum** — every route, extractor and middleware is a value composed
  at startup; the request path is pure traversal.
- **NestJS** — modules, controllers, DI with scopes, lifecycle hooks
  in topological order.
- **FastAPI** — validation-driven (Pydantic, msgspec, dataclass, TypedDict) and automatic OpenAPI.

The framework is intentionally **opinionated and small**. New features
should be justified against the existing mental model before adding
surface area. The current public-API surface is documented exhaustively
in `llms-full.txt` and a short overview lives in `llms.txt` per the
[llmstxt.org] convention. Both are checked by `nox -s docs_contract`.

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
├── _asgi/                   — ASGI adapter, OpenAPI, docs, RFC 9457 rendering (_problem.py), debug pages (_debug.py)
├── _config/                 — typed configuration: coercion, sources, loader, ConfigModule (Phase 2b), Secret
├── _di/                     — dependency injection container + custom providers; _scope.py = shared scope finaliser
├── _discovery.py            — startup discovery pass over provider methods/classes (Phase 6a, private)
├── _discriminated.py        — Discriminated[A|B,"key"] runtime (detection, validation, OpenAPI)
├── _encoders/               — optional encoder backends (pydantic)
├── _events/                 — application event bus: @on_event marker, handler plans + dispatch table, EventBus/EventPublisher
├── _health/                 — health probes: @health_check marker, HealthRuntime (timeouts, single-flight, cache), indicators, HealthModule
├── _lifecycle/              — post_construct / pre_destruct machinery
├── _modules/                — module graph & visibility
├── _pagination/             — pagination: PaginationConfig resolution, paging plans ("pagination" source), cursor codec, Link URLs, Page rendering, OpenAPI contribution
├── _routing/                — radix-tree HTTP router
├── _scheduling/             — hosted-service supervisor, scheduler, cron parser, clocks, ScheduleModule (private)
├── _signing.py              — the shared HMAC signer (key rotation, constant-time verify, namespaces): sessions, cursors, signed URLs
├── _schema/                 — native validation engine: type compiler, validator nodes, issues,
│                              pydantic/msgspec backends, JSON-Schema emitter, request/frame/item plans
├── _typing/                 — ForwardRef / PEP 563 resolver (private)
├── _validation.py           — provider-agnostic type detection + the 1.x shallow `validate_as` (legacy/warn modes, config loader)
├── _ws_runtime.py           — WebSocket dispatch engine + WsConnectionContext / WsUpgradeRequest
├── _socketio.py             — Engine.IO/Socket.IO adapter (private)
├── _stores/                 — Store protocol, bounded MemoryStore/TTLMap, Lock, StoresModule (private)
├── _cache/                  — Cache service, @cache_response / @idempotent runtime, CacheModule (private)
├── _schema/                 — native validation engine (F02); `_contract.py` = response projectors
├── responses.py             — typed results: Ok / Created / Accepted / NoContent / Status
├── reflect/                 — public sub-package: full metadata introspection API
│   ├── __init__.py          — re-exports all public symbols
│   ├── _reader.py           — static class readers (own-class rule, no inheritance)
│   ├── _app_reader.py       — app-level readers (get_all_routes, get_all_ws_gateways, …)
│   ├── _types.py            — frozen result dataclasses (ReflectedRoute, ReflectedController, …)
│   ├── _context.py          — WsConnectionContext / WsUpgradeRequest re-export
│   └── _composer.py         — apply_guards / apply_interceptors helpers
├── decorators.py            — user-facing @controller, @module, @get, …
├── exceptions.py            — the closed error catalog (docstrings feed docs/reference/errors.md)
├── extractors.py            — Path/Query/Json/Depends + pipes + custom extractors
├── logging.py               — built-in NestJS-style logger
├── serialization.py         — JSON encoders (stdlib, orjson, msgspec, pydantic)
├── background.py            — BackgroundTasks, TaskHandle (fire-and-forget after response)
├── context.py               — ApplicationContext (create_context) + OperationScope (app.scope())
├── commands.py              — @command, Opt, CommandContext, CommandResult (public surface)
├── _commands/               — command compiler (argparse at startup) + `python -m lauren` runner
├── __main__.py              — `python -m lauren exec | commands | console`
├── config.py                — ConfigModule, Secret, sources, Profile, load_settings (typed configuration)
├── context.py               — OperationScope (request scope outside HTTP, via app.scope())
├── events.py                — EventBus, EventPublisher, @on_event, EventContext, EmitResult (domain events)
├── scheduling.py            — @hosted_service, @interval/@cron/@run_once, ScheduleModule, SchedulerRegistry, ManualClock
├── health.py                — HealthModule, @health_check, HealthResult, HealthStatus (liveness/readiness probes)
├── pagination.py            — OffsetPagination, PageNumberPagination, CursorPagination, PaginationConfig, Page, CursorPage, paginate_sequence
├── signals.py               — POSIX signal integration + on_shutdown hooks
├── streaming.py             — StreamingResponse[T], Stream, StreamReader
├── sse.py                   — EventStream, ServerSentEvent, last_event_id
├── stores.py                — public Store / MemoryStore / StoresModule / Lock / session adapters
├── cache.py                 — public Cache / CacheModule / cache_response / idempotent
├── socketio.py              — public Socket.IO controller surface
├── testing/                 — TestClient / AsyncTestClient, Test.create_testing_module, pytest_plugin
├── types.py                 — Request, Response, State, Headers, Scope, Discriminated, …
├── validation.py            — public validation surface: markers, ValidationPolicy, ValidationIssue, TypeCompiler, validate
├── websockets.py            — @ws_controller, @on_message, WebSocket, BroadcastGroup
├── docs.py                  — programmatic access to llms.txt / llms-full.txt
├── llms.txt                 — short overview (llmstxt.org)
├── llms-full.txt            — complete reference for LLM ingestion
└── py.typed                 — PEP 561 marker
tests/
├── unit/                    — isolated unit tests (no ASGI app)
├── integration/             — full-stack tests via TestClient / WsTestClient
├── e2e/                     — end-to-end multi-backend tests (full stack, all validator paths)
├── property/                — Hypothesis property tests for validation invariants
└── conftest.py              — session-scoped _preload_lauren fixture + pytest markers
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
   coexist in one process (tests rely on this). *Known exceptions,
   scheduled for removal:* the pipe / extractor instance caches in
   `extractors.py` and `_add_ws_route` temporarily widening
   `Router.HTTP_METHODS`. Do not add more. (Validators are compiled by the
   app-owned `TypeCompiler` — `app.type_compiler`; there is no global cache.) The import-time declaration registries (`lauren._discovery`
   markers, `lauren._di._scope.register_framework_value`) are not
   exceptions: they record which framework decorators and context types
   exist, append-only, and never hold per-app data.

6. **Async-first, but not async-only.** Handlers may be sync (`def`) or
   async (`async def`); the dispatch engine adapts.  Sync handlers are
   **automatically offloaded** to a thread pool via
   `anyio.to_thread.run_sync` — they never block the event loop.
   Providers may also be sync or async factories.

7. **Pydantic is optional at runtime** for the core path. Guard every
   `import pydantic` with a try/except and expose a clear error when a
   feature that genuinely needs it is used without it.

8. **The error catalog is closed.** Adding a new error class is a
   public-API change that happens only through a justified design proposal
   (the HTTP family — 400/404/406/409/410/412/415/429/503/504 — was the last
   such growth); new runtime conditions should subclass an existing
   category (`StartupError`, `HTTPError`, `LifecycleError`) rather than
   creating a parallel hierarchy, and framework code that needs an HTTP
   status raises the built-in class (with `headers=` / `retry_after=` /
   `challenge=` / `allow=` as needed) instead of hand-building a response.
   The complete, generated table lives in `docs/reference/errors.md` and
   `lauren.reflect.error_catalog()` returns it as data — never write the
   count in prose. A new class needs a one-line docstring (it becomes the
   table's "Meaning"), entries in `lauren.exceptions.__all__` and
   `lauren.__all__`, then `python scripts/sync_doc_facts.py`.

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
| Decouple modules with domain events | `EventBus` / `EventPublisher` + `@on_event` (`lauren.events`) |
| Expose liveness / readiness / startup probes | `HealthModule.for_root()` + `@health_check("name")` on a singleton method (`lauren.health`) |
| Fail readiness before the server stops accepting traffic | `app.begin_draining()` (or `install_signal_handlers(app, shutdown_delay=...)`); read `app.lifecycle_state` |
| Let a global guard skip public routes | `ctx.get_metadata(PUBLIC_METADATA_KEY, False)` in the guard; mark routes with `@set_metadata(PUBLIC_METADATA_KEY, True)` |
| Use request-scoped providers outside a request (script, job, worker) | `async with app.scope("name") as scope:` + `await scope.resolve(T)` |
| Run a long-lived loop for the app's lifetime (consumer, relay, heartbeat) | `@hosted_service()` class with `async def run(self, ctx: ServiceContext)` |
| Run code on a schedule | `@interval` / `@cron` / `@run_once` on a singleton method + `ScheduleModule.for_root()` |
| Persist state across requests for one client | Sessions (`sessions=SessionConfig(...)`; inject `session: Session`) |
| Run code with the app's DI graph outside HTTP | `LaurenFactory.create_context(AppModule)` / `app.scope()` |
| Admin / maintenance task (seed, migrate, purge) | `@command("db:seed")` on a provider method + `python -m lauren exec` |
| Cache an expensive computation | `CacheModule.for_root()` + `await cache.get_or_set(key, factory, ttl=..., tags=(...), type=T)` |
| Cache a GET response server-side | `@cache_response(ttl=..., vary=(...), key=...)` on the handler |
| Make an unsafe request safe to retry | `@idempotent(ttl=..., scope=...)` (`Idempotency-Key` header) |
| Share state across workers (sessions, cache, locks) | One `Store` via `StoresModule.for_root(default=...)` |
| Paginate a list endpoint | `paging: OffsetPagination` / `PageNumberPagination` / `CursorPagination[C]` + return `Page[T]` / `CursorPage[T]` (`Page.of`, `Page.from_rows`, `CursorPage.from_rows`); `PaginationConfig` app-wide or in `Annotated[...]` |
| Sign a value the client must not forge | `lauren._signing.Signer(secrets, namespace="<feature>")` (first key signs, all verify) — never hand-rolled `hmac` |
| Read typed configuration | `ConfigModule.for_root(Settings)` (or `for_feature`) + inject `Settings`; `load_settings` for factory kwargs |
| Override JSON encoder per route or controller | `@use_encoder(OrjsonEncoder())` on method or class |
| Declare a route's success status | `@post("/", status_code=201)` or a typed result `-> Created[T]` (`Ok` / `Accepted` / `NoContent` / `Annotated[T, Status(206)]`) |
| Keep internal fields off the wire (declare the output shape) | Annotate the public type and set `response_validation="filter"` (app or route; `"strict"` also validates) |
| Document the errors a route raises | `@raises(NotFoundError, ...)` on the method or controller |
| Constrain a field or parameter (bounds, length, regex) | `Annotated[int, Ge(1)]` markers from `lauren.validation` (`QueryField(...)` also works) |
| Reject unknown keys / rename fields on a stdlib model | `@validation_config(extra="forbid", rename="camel")` |
| Validate a payload outside a request (service, job, command) | `app.type_compiler.compile(T).validate(data)` |
| Copy Lauren metadata (guards, interceptors, …) to another class/fn | `@propagate_metadata(source)` |
| Read all routes declared on a controller | `reflect_routes(cls)` → `tuple[ReflectedRoute, ...]` |
| Replace a provider / guard / middleware in a test | `Test.create_testing_module(AppModule).override_provider(X).use_value(fake).compile()` |
| Query the compiled route table of a running app | `get_all_routes(app)`, `get_route_metadata(app, method, path)` |
| Read any `@use_*` metadata from a class without `__dict__` access | `reflect_guards`, `reflect_interceptors`, `reflect_exception_handlers`, … |
| Answer with a standard HTTP error (400/404/409/429/503/…) | Raise the built-in (`NotFoundError`, `ConflictError`, `TooManyRequestsError(retry_after=…)`, …); subclass it for a domain `code` |
| Send `WWW-Authenticate` / `Retry-After` / `Allow` / any header with an error | `UnauthorizedError(challenge=…)`, `retry_after=`, `MethodNotAllowedError(allow=…)`, `headers=` on any error |
| Make a guard's `False` answer 401 instead of 403 | `deny_error = UnauthorizedError` class attribute on the guard |
| Emit RFC 9457 problem details | `LaurenFactory.create(..., error_format="rfc9457", problem_type_base=…)` |
| See tracebacks for unhandled errors while developing | `LaurenFactory.create(..., debug=True)` (or `DebugConfig(...)`) |
| List every error class (framework + app) as data | `error_catalog()` → `tuple[ReflectedError, ...]` |

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
   Mark the canonical example ```` ```python run ```` (MkDocs pages:
   `<!-- docs-contract: run -->` on the line before the fence) so it
   executes in CI; mark intentional excerpts `fragment`. List any newly
   documented attribute in `docs_contract.toml`. Never hard-code counts or
   file sizes in prose — link the generated table instead.
6. Add a docs page under `docs/guides/` (or extend an existing one).
7. Run the full suite (`pytest -q`) and `nox -s docs_contract`. Both must pass.
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
- **Route-decorator options go through one table.** `@get` … `@options`
  (and `Lauren`'s `@app.get` …) validate their keyword options with
  `_validate_route_options` against `_ROUTE_OPTION_VALIDATORS` in
  `decorators.py` (name → `validate(value)` returning the normalised value or
  raising `_RouteOptionError(reason, message)`); a bad value becomes a
  `DecoratorUsageError` with `detail = {"decorator", "target", "option",
  "reason"}` at decoration time. To add an option (`consumes`, `name`,
  `cache_control`, …): add the keyword to `_route_decorator`'s factory and to
  `RouteMeta.__init__`, and register one validator in the table — never
  validate inline in a single verb. Options that need the resolved return
  annotation (e.g. 204 vs a body type) are checked in Phase 5
  (`lauren/_asgi/_contracts.py::describe_responses`).
- **Stacking accumulates, never overwrites.** Every metadata decorator in
  the framework is stackable and *additive* — `@get`/`@post` append to
  `ROUTE_META`, `@use_guards`/`@use_interceptors`/`@use_middlewares`/
  `@use_exception_handlers` append to their lists, `@set_metadata` merges,
  and `@exception_handler` accumulates its exception types (reading the
  target's **own** `__dict__` so the strict-inheritance rule holds). A new
  decorator that stores a single value must merge with any value already on
  the target rather than clobbering it.

**Decorators the framework must find on providers at startup** (a scheduled
job method, a health check, an event handler, a command, a hosted-service
class) hook into the shared discovery pass in `lauren/_discovery.py` — never
add another MRO walk:

1. At import of the feature's public module, call
   `register_marker(MarkerSpec("__lauren_<feature>__", ...))`:
   `kind="class"` for class markers, `framework_types=(MyContext,)` for
   parameters the feature supplies, `extra_params="collect"` when leftover
   parameters are the feature's own (CLI arguments). If constructors may
   receive the context too, also call
   `lauren._di._scope.register_framework_value(MyContext)`.
2. `LaurenFactory.create` Phase 6a (after the final DI compile, before the
   lifecycle order) builds `app._discovery`, an immutable `DiscoveryIndex`.
   Method markers follow the route rule (first MRO definition wins, so an
   undecorated override drops the marker); class markers are own-`__dict__`
   only (`MetadataInheritanceError` otherwise); order is provider
   registration order, then MRO; function / value / factory / alias
   providers are skipped, `use_class` scans the implementation class.
3. The consumer reads `app._discovery.methods[name]` /
   `.classes[name]` and runs an entry with `entry.invoke(instance, scope,
   values=...)`: the `CallPlan` was compiled at startup (annotations via
   `resolve_type_hints`, DI parameters validated against the provider's
   module, `UnresolvableParameterError` naming method and parameter), so
   nothing reflects at run time. Open the scope with `app.scope(...)` when
   `entry.plan.needs_scope`.

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
- **`@use_guards` and `@use_interceptors` work natively on
  `@ws_controller` classes.** The WS runtime reads the metadata at
  connection time, resolves guards from DI, and calls
  `can_activate(WsConnectionContext)` before `@on_connect` runs. The
  same guard class works on both HTTP and WS — `WsConnectionContext` and
  `WsUpgradeRequest` duck-type with `ExecutionContext` and `Request`. See
  `lauren/reflect/` for the helpers used internally and available to
  extension packages (`apply_guards`, `apply_interceptors`,
  `reflect_guards`, `reflect_interceptors`, `reflect_middlewares`).
- **Typed messages.** A `@on_message("chat.send")` handler that takes
  `body: Json[ChatMessage]` runs through the same validation pipeline as
  HTTP `Json[T]` extractors (pydantic, msgspec, dataclass, TypedDict) —
  the validator is built once at startup. `Discriminated[A | B, "key"]`
  payloads work the same way without requiring pydantic.
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
`client.websocket_connect(...)` / `lauren.testing.WsTestClient` (websockets) or
`TestClient` (SSE — `client.get(...)` returns a finite stream's entire body,
`client.stream(...)` reads an endless one event by event and disconnects on
exit).

→ *See `skills/building-lauren-streaming/` for copy-paste SSE and WebSocket gateway patterns.*

## 8. Common Pitfalls

- ❌ Persisting state on the `Request` object. `Request` instances are pooled
  and reused; `Request.reset()` clears the instance's entire `__dict__`
  between requests, so any attribute set on `request` (or read from a previous
  request) is unreliable. Use `request.state` for request-scoped hand-off and
  copy anything you retain (`dict(request.path_params)`).
- ❌ Clearing a per-request container **in place** when an accessor hands that
  same object to user code. `reset()` used to do `self._path_params.clear()`;
  because `path_params` returns the live `self._path_params`, a middleware or
  background task holding the dict from an earlier request then saw it emptied
  or overwritten once the next request reused the pooled instance. **Assign a
  fresh container** instead (`self._path_params = {}`) — exactly as `reset()`
  already does for `State`. Regression-locked by
  `tests/unit/test_request_reset.py`,
  `tests/property/test_request_pool_isolation.py` and
  `tests/integration/test_request_pool_no_leak.py`.
  The one sanctioned in-place reuse is the router populating the dict `reset()`
  just allocated (`lauren/_asgi/__init__.py`: `req._path_params.clear()` then
  `.update(params)`), which is safe **only** because `reset()` guarantees that
  dict is fresh and unshared with any earlier request — a global middleware
  snapshot taken before routing depends on the params appearing there. Locked
  by `tests/integration/test_pre_routing_path_params_snapshot.py`.
- ❌ `asyncio.create_task(...)` inside `@post_construct` to start a background
  loop. Nothing supervises or stops it: it can still use the database after the
  pool's `@pre_destruct` closed it, its exceptions surface only as "Task
  exception was never retrieved", and it dies with `TestClient`'s startup loop.
  Use `@hosted_service()` (stopped before `@pre_destruct`, restarted on failure)
  or `@interval` / `@cron`.
- ❌ Assuming one worker for scheduled jobs. `uvicorn --workers 4` runs every
  `@interval` / `@cron` job four times. Use `exclusive=True` with a shared
  `SchedulerLock` (the default `InMemorySchedulerLock` is per process), or run
  scheduled work in one dedicated process (`ScheduleModule.for_root(enabled=False)`
  / `scheduler=False` in the web workers).
- ❌ Scheduling a method on a `Scope.REQUEST` / `TRANSIENT` provider, or
  forgetting `ScheduleModule.for_root()` — both raise `LifecycleConfigError` at
  startup. Request-scoped work inside a job belongs in a job **parameter**
  (resolved per run in a fresh `OperationScope`), not in the job's provider.
- ❌ Reading `os.environ` in `__init__` or at import time. It fails late (after
  `create()` returned, or while Python imports the module graph), one key at a
  time, and makes two apps with different configuration impossible in one
  process. Declare a settings class, import `ConfigModule.for_root(Settings)`
  and inject `Settings`; tests pass `environ={...}`.
- ❌ `load_dotenv(override=True)` — a `.env` file must never override the real
  environment, and the framework never writes `os.environ`. `DotenvSource`
  parses into a private mapping and `EnvSource()` (always last) wins.
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
- ❌ Calling `client.get(...)` on an endless stream and expecting per-frame
  assertions — `get` buffers the whole body (and hits the client `timeout`
  on a stream that never ends). Use `with client.stream(...) as r:` and
  `r.iter_sse()` / `r.iter_bytes()`; leaving the block disconnects.
- ❌ Driving one app with two independent test clients. An app's loop-bound
  resources live on its driver's event loop: a second `TestClient(app)`
  attaches to the first client's loop, entering it with `with` raises, and an
  `AsyncTestClient` on another loop raises. Use `client.new_session()` for a
  second user and build a fresh app (or testing module) per lifespan.
- ❌ Rebuilding a whole test module with `use_value(...)` to swap one provider
  — use `Test.create_testing_module(AppModule).override_provider(X).use_value(fake).compile()`;
  it replaces the provider wherever it lives in the graph and is validated by
  the real factory (a target nothing registers fails `compile()`).
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
  `Scope.SINGLETON` instances instead. The same applies to a `Session`:
  capture `session.as_dict()` or specific values, never the live object.
- ❌ Resolving a `Scope.REQUEST` provider outside a request, a WebSocket
  connection or `app.scope()` (e.g. `await app.container.resolve(DbSession)`
  in a script or job). It silently falls back to transient: a new instance per
  call that is never torn down (no `@pre_destruct`, no generator teardown).
  Use `async with app.scope("job") as scope: await scope.resolve(DbSession)`.
- ❌ Using `SignalBus` (or a `LifecycleEvent` subclass) for domain events. Its
  listeners cannot receive dependencies and their errors are swallowed; publish
  on the application `EventBus` / `EventPublisher` and handle with `@on_event`
  methods, which are DI-aware and validated at startup.
- ❌ Putting the `Request` (or any request-scoped instance) inside an event.
  Requests are pooled and request scopes close before deferred handlers run;
  copy the values a handler needs into the event.
- ❌ Injecting `EventPublisher` into a singleton, a job or an `app.scope()`
  block — it joins the current request / connection / handler scope and
  raises elsewhere. Inject `EventBus` there (one operation scope per emission).
- ❌ Asserting on a `"concurrent"` `@on_event` handler driven through the
  synchronous `TestClient` — each sync request runs in its own event loop,
  which cancels the handler's task when it closes. Use `wait=True`, an async
  test (`await client.arequest(...)` then `await bus.drain()`) or
  `"background"`.
- ❌ Checking dependencies in liveness probes. A database outage must take
  pods **out of rotation** (readiness), not **restart** them (liveness) — that
  turns a hiccup into a restart storm. `@health_check` feeds `"ready"` by
  default; tag a check `"live"` only when its failure means *this process* is
  broken.
- ❌ Hand-rolled `{"status": "ok"}` health endpoints that ignore the lifecycle
  state — they answer "ok" before `@post_construct` ran (nothing starts the app
  lazily on a first request), while draining and while dependencies are down.
  Use `HealthModule.for_root()`, or at least answer from `app.lifecycle_state`.
  Under uvicorn `shutdown()` runs after the socket closed, so readiness must
  flip earlier: `app.begin_draining()` from a `preStop` window or
  `install_signal_handlers(app, shutdown_delay=...)`.
- ❌ Comparing private flags (`app._started`, `app._running`) or keeping a
  private copy of the app's state in a framework component — both are derived
  from `app.lifecycle_state`; read that (the event bus does).
- ❌ Writing another request-scope teardown loop. HTTP, WebSocket and
  `OperationScope` all call `lauren._di._scope.finalize_scope`, which finds
  providers through the container's precomputed `instance_index()`.
- ❌ Exposing raw database ids or offsets as cursors, or hand-rolling
  base64 + HMAC cursors. Use `CursorPagination[C]`: cursors are opaque, signed
  with the shared `lauren._signing.Signer` in their own namespace, versioned
  and optionally expiring; a tampered one is a 400 `invalid_cursor`. Give it a
  dedicated `PaginationConfig(cursor_secret=...)` — never the session secret —
  or the route fails startup (`reason="pagination_secret_missing"`).
- ❌ Guessing `has_next` from `len(items) == limit`, or hand-building a
  `{"data": ..., "pagination": ...}` envelope per endpoint. Return
  `Page.of(rows, paging, total=...)` (or fetch `limit + 1` rows and use
  `Page.from_rows`); the app's `PaginationConfig(envelope=...)` decides the
  shape once, and only pages built from a paging value carry `Link` headers.
- ❌ Injecting `session: Session` (or reading `request.state.session`)
  without `sessions=SessionConfig(...)` on the factory — raises
  `SessionConfigError` at startup, not at runtime.
- ❌ Storing confidential data in `SignedCookieSessionStore` — the cookie
  is *signed* (tamper-proof) but *not* encrypted; the client can read it.
  Use a server-side store for anything sensitive.
- ❌ Forgetting `session.regenerate_id()` at login / privilege change —
  leaves the app open to session fixation. Call it whenever the session's
  trust level changes.
- ❌ Expecting `session.invalidate()` to revoke a *bare* `SignedCookieSessionStore`
  session server-side — it can't (the store is stateless); logout relies on
  the browser honouring the `Max-Age=0` cookie. To make the cookie store
  revocable, pass `SessionConfig(revocation_store=InMemoryRevocationStore())`:
  `invalidate()`/`regenerate_id()` then deny-list the cookie's token, and
  `RevocationStore.revoke_user(user_id)` does "log out everywhere". Revocation
  needs a finite `max_age` (else `SessionConfigError` at startup).
- ❌ Relying on cookie sessions for JSON routes without
  `content=ContentConfig(strict_content_type=True)` or `lauren-guards.csrf`.
  A `Json[T]` body is parsed whatever its `Content-Type`, and a cross-site
  form / `no-cors` fetch can send `text/plain` without a preflight while the
  browser attaches the cookie. Session apps default to `"warn"` (accept + log
  once per route and media type) and get one startup WARN until a policy is
  set explicitly; strict mode answers 415 `UnsupportedMediaTypeError` before
  the body is read (guards still run first).
- ❌ Registering a store (or any object with `@post_construct` / `startup()`)
  through `use_value` and expecting its hooks to run — `use_value` never runs
  lifecycle hooks. Use `StoresModule.for_root(default=...)`, pass it to
  `SessionConfig(store=...)` / `CacheModule.for_root(store=...)` (both run its
  hooks, once per object), or `DIContainer.register_instance` in framework code.
- ❌ `@cache_response` on per-user data without `key=` — every caller shares
  one entry per URL. Pass `key=lambda ctx: ...` (returning `None` skips the
  cache); a cached route that injects `Session` without `key=` logs a startup
  warning, and requests carrying `Authorization` bypass the cache unless
  `key=` / `vary` / `allow_authenticated=True` says otherwise.
- ❌ Constructing `MemoryStore()` without `max_entries`, or adding an in-memory
  map that grows with traffic — stores must be bounded (`TTLMap` in
  `lauren/_stores/_memory.py` is the building block).
- ❌ Expecting a model back from `Cache.get` / `get_or_set` hits without
  `type=` — hits decode to JSON data; pass `type=Model` or use `cache.typed(Model)`.
- ❌ Serving a directory that contains secrets with
  `StaticFilesModule.for_root(..., dotfiles="allow")`. The default
  (`"allow_well_known"`) answers an empty 404 for any path segment starting
  with `.` (`.env`, `.git/config`) and still serves `/.well-known/**`;
  `"deny"` hides `/.well-known/` too. Never widen it for a build output you
  have not audited.
- ❌ Putting `@use_guards` on a WebSocket gateway class and expecting it to
  run per message — class-level (and `global_ws_guards`) guards run **once,
  at connect**. To authorize individual events, decorate the `@on_message`
  handler: its guards run for every frame (with a `WsMessageContext`) and a
  denial answers with a `forbidden` error frame. `@use_middlewares`,
  `@use_exception_handlers` and `@use_encoder` on any WS hook, and
  guards/interceptors on `@on_disconnect` / `@on_error` or on Socket.IO
  `@on_socketio_event` handlers, are rejected at startup; class-level
  `@use_middlewares` on a gateway never runs and logs a startup warning.
- ❌ Expecting `@post_construct` / `@pre_destruct` to fire per-request
  on a controller — `@controller` defaults to `Scope.SINGLETON` (NestJS
  behaviour), so lifecycle hooks fire once at startup/shutdown. If a
  controller needs per-request construction, add
  `@injectable(scope=Scope.REQUEST)` *below* `@controller` (bottom-up
  application means `@injectable` runs first and wins).
- ❌ Admin tasks as HTTP endpoints. A `POST /api/seed` route (often without a
  guard) lets anyone who reaches the API run it. Write a `@command` on a
  provider and run it with `python -m lauren exec TARGET db:seed`; scripts use
  `async with LaurenFactory.create_context(AppModule) as ctx:`. Never call
  private `_open()` hooks by hand — a context runs the real lifecycle.
- ❌ Resolving `Scope.REQUEST` providers outside a scope. `await
  app.container.resolve(DbSession)` in a script, job or worker silently falls
  back to a transient that is **never torn down** (no `@pre_destruct`, no
  `aclose()`); the 1.x default only warns once per provider. Resolve them
  inside a request, `app.scope()` / `ctx.scope()` or a command, and build test
  apps with `strict_request_scope=True` (it raises `DIScopeViolationError`,
  `reason="no_active_scope"`; the 2.0 default).
- ❌ Expecting `Lauren.on_startup` hooks to run at compile time. They run
  inside `LaurenApp.startup()` after every `@post_construct` hook (before
  hosted services start); `await lauren_app.startup()` only compiles.
- ❌ Writing a blocking sync `@pre_destruct` hook without concern for the
  event loop — sync hooks run in a thread pool via `asyncio.to_thread` so
  they do not block, and they respect the same `timeout=` as async hooks.
  A sync hook that never returns will be cancelled at the timeout boundary.
- ❌ Extracting the current request from DI when you need `route_template`
  or handler metadata — use `ExecutionContext` injection instead. Any
  handler parameter typed as `ExecutionContext` (from `lauren.types`) is
  automatically provided by the dispatch engine at zero cost; no extractor
  marker is needed.
- ❌ Using `Scope.TRANSIENT` with a generator function provider — transient
  instances are not tracked by the container so teardown can never be
  invoked. The container raises `StartupError` at registration time.
- ❌ Expecting a generator provider's teardown to run if the generator yields
  more than once — the convention is exactly one `yield`; the container only
  advances past the first `yield` during setup and again once during teardown.
  Multiple yields are not an error but only the first yielded value is used.
- ❌ Defining a custom route-handler descriptor without `__call__` and expecting
  it to be auto-detected — as of v1.4.1 Lauren supports non-callable descriptors
  that set `__wrapped__` via `functools.update_wrapper`. Prior versions required
  `__call__` (so that `callable(descriptor)` returned `True`).  The only
  requirement now is `__get__` + `__wrapped__`.
- ❌ Expecting route- or controller-level exception handlers to catch an error
  raised by a **global** middleware (or the session engine) — it is raised
  outside any route, so only `global_exception_handlers` apply. An `HTTPError`
  raised by middleware at any level still renders its own status; anything
  unclaimed becomes a logged `500 internal_error`.
- ❌ Defining your own `NotFoundError(HTTPError)` (or `ConflictError`,
  `BadRequestError`, …) — the everyday HTTP errors are built in. Subclass
  `lauren.NotFoundError` to keep the shared status and set a domain `code`
  (`class UserNotFound(NotFoundError): code = "user_not_found"`); a handler for
  `NotFoundError` then claims it too.
- ❌ Returning `False` from an **authentication** guard without
  `deny_error = UnauthorizedError` — the denial is a 403 instead of a 401. For a
  challenge header use a zero-argument factory
  (`deny_error = staticmethod(lambda: UnauthorizedError("…", challenge="Bearer"))`).
  An invalid `deny_error` raises `GuardConfigError` at startup.
- ❌ Hand-building a 401/405/429/503 `Response` to add `WWW-Authenticate`,
  `Allow` or `Retry-After` — raise the error with `challenge=` / `allow=` /
  `retry_after=` (or `headers=`) instead; the headers are sent in every
  `error_format`. An `@exception_handler` that returns its own `Response` does
  **not** inherit `exc.headers`.
- ❌ Enabling `debug=` outside development, or deriving it from an environment
  variable without care — debug pages show source code and request data. They
  are loopback-only by default (not a boundary behind a same-host proxy), the
  factory warns whenever they are on, and a production `Profile` from the typed
  configuration module makes `debug=` a `StartupError`
  (`reason="debug_in_production"`).
- ❌ Assuming dataclass / `TypedDict` bodies are type-checked under the 1.x
  default — `ValidationPolicy(stdlib_models="warn")` only *logs* what the native
  engine would reject and keeps the shallow value. Pass
  `validation=ValidationPolicy(stdlib_models="enforce")` (the 2.0 default). A
  model that uses `lauren.validation` markers or `@validation_config` is always
  enforced; pydantic and msgspec types are always validated by their library.
- ❌ Parsing `detail.errors` strings or `detail.field` of a 422 — read
  `detail.issues` (`[{"loc", "type", "msg", "ctx"?}]`, every failing parameter
  at once). The 1.x keys are removed in 2.0.
- ❌ Calling `TypeCompiler().compile(T)` (or `validate(T, ...)`) on a hot path —
  compiling walks the type; hold one compiler (or use `app.type_compiler`) and
  reuse the `CompiledType`.
- ❌ Expecting `call_handler.handle()` to return the raw handler value (dict,
  Pydantic model, tuple, etc.) inside an interceptor — as of v1.4.2
  `handle()` always returns a coerced `Response`.  Interceptors can safely call
  `.status_code`, `.body`, `.headers`, `.with_header()`, etc. without any
  `isinstance` check.  Code that did `if isinstance(result, dict): result["k"] = v`
  must be updated to parse `result.body` and rebuild via `result.with_body(...)`.
- ❌ Returning an internal model (`UserInDB`, an ORM row) from a route annotated
  with a public one (`-> UserOut`) while `response_validation="off"` (the 1.x
  default) — the runtime object is dumped and its extra fields (`password_hash`)
  leak; `response_model=` alone only documents. Set
  `response_validation="filter"` (app-wide or per route): the value is projected
  onto the declared type, missing required fields become a 500
  `response_validation_error`.
- ❌ Using `Response.created(...)` / `Response.json(...)` and expecting the app's
  `json_encoder` — they use the process-wide default encoder. Return a typed
  result (`Created(user, location=...)`) or a plain value: the dispatcher
  encodes those with the route's encoder.
- ❌ Declaring `status_code=204` (or `Status(204)`) with a body-bearing return
  annotation — RFC 9110 forbids content, startup raises `DecoratorUsageError`
  (`reason="body_not_allowed"`). Annotate `-> None` or `-> NoContent`.
- ❌ Returning SDK clients, settings or arbitrary objects from handlers — the
  `__dict__` fallback ships their public attributes. `json_fallback="warn"` (the
  default) logs it once per type; set `json_fallback="error"` to turn it into a
  500, or declare a response type.

## 9. Testing Playbook

- **Unit** (`tests/unit/`) — import the specific module, call directly.
  Pure-Python, no event loop unless the module is async-only.
- **Integration** (`tests/integration/`) — build a `@module`, call
  `LaurenFactory.create`, drive via `lauren.testing.TestClient` (prefer
  `with TestClient(app) as client:` — startup on enter, shutdown hooks on
  exit, cookie jar on). Swap collaborators with
  `Test.create_testing_module(...).override_provider(...)`.
- **E2E** (`tests/e2e/`) — full-stack multi-backend tests that exercise
  all validator paths (pydantic, dataclass, TypedDict, discriminated unions)
  in a single running app. Mirror integration structure but verify the
  complete request/response cycle without mocking.
- **Property** (`tests/property/`) — Hypothesis-driven tests for validation
  invariants. Require `hypothesis>=6.0` (in `dev` extras). Skip automatically
  when hypothesis is absent (`pytest.importorskip`).
- **WebSocket** — use `lauren.testing.WsTestClient`; the session
  context-manager guarantees the server task is awaited so unhandled
  server-side exceptions surface cleanly into the test.
- **SSE** — return `EventStream` from a handler, drive it through
  `TestClient`: parse a finite body from `client.get(...)`, or read an endless
  stream with `client.stream(...).iter_sse()`.
- **Disconnects** — `disconnect_after=` on any request (or closing a stream)
  simulates a client abort; `receive()` otherwise blocks after the body until
  the response is complete, like a real server.
- **Startup-failure tests** — build the module, `LaurenFactory.create`
  inside `pytest.raises(SomeError)`, assert on `detail` keys too.
- **Regression shape:** every bug fix gets a test in the same file as
  its nearest existing neighbour.
- **Optional-dep tests** — tests that block pydantic/msgspec via
  `sys.modules` manipulation MUST depend explicitly on the session-scoped
  `_preload_lauren` fixture (or inherit it via `autouse=True`) so that
  Lauren's `_PYDANTIC_AVAILABLE` flag is set correctly before any module
  is first imported with an optional dep absent.

→ *See `skills/testing-lauren-apps/` for `TestClient` setup, async test patterns, and mock-provider recipes.*

## 10. Commit Messages

`<scope>: <imperative sentence under 72 chars>`. Scopes: `di`, `asgi`,
`routing`, `ws`, `sse`, `streaming`, `background`, `typing`,
`extractors`, `exceptions`, `reflect`, `tests`, `docs`, `meta`. Example:
`typing: resolve ForwardRef annotations via _typing sub-package`.

## 11. Where to Look First

- `lauren/_asgi/__init__.py` — the request lifecycle, top to bottom.
- `lauren/_di/__init__.py` — provider graph & cycle detection.
- `lauren/_di/custom.py` — `use_value` / `use_class` / `use_factory`
  / `use_existing` recipes, `Token`, `Inject`.
- `lauren/_di/_scope.py` — `finalize_scope` (the one request-scope teardown)
  and `register_framework_value`.
- `lauren/context.py` — `OperationScope`; `LaurenApp.scope()` builds one.
- `lauren/events.py` / `lauren/_events/` — the application event bus:
  `@on_event` (a discovery marker), handler plans and the per-app dispatch
  table (`_plan.py`), `EventBus` / `EventPublisher`, task tracking and the
  shutdown drain (`_bus.py`).
- `lauren/scheduling.py` / `lauren/_scheduling/` — hosted services
  (`_supervisor.py`), the scheduler and `SchedulerRegistry` (`_scheduler.py`),
  `CronExpression` (`_cron.py`), `ManualClock` (`_clock.py`) and the factory
  hooks (`_module.py`: Phase 2c registers the registry, Phase 6a validates,
  `LaurenApp.startup()` / `shutdown()` drive the runtime).
- `lauren/_discovery.py` — Phase 6a discovery: `register_marker`,
  `MarkerSpec`, `DiscoveryIndex`, `DiscoveredMethod`, `CallPlan`.
- `lauren/health.py` / `lauren/_health/` — `HealthModule.for_root`, the
  `@health_check` marker, `compile_checks` (startup validation) and the per-app
  `HealthRuntime` (probe semantics, timeouts, single-flight, cache, details
  policy, `HealthChanged`). The lifecycle state machine itself lives on
  `LaurenApp` (`lifecycle_state`, `begin_draining()`, `_transition`).
- `lauren/_ws_runtime.py` — `WsConnectionContext`, `WsUpgradeRequest`, `CompiledGateway`, `handle_websocket`.
- `lauren/websockets.py` — gateway runtime + `BroadcastGroup`.
- `lauren/reflect/` — full metadata introspection API: `reflect_routes`, `reflect_controller`, `get_all_routes`, `get_controller_metadata`, `propagate_metadata` targets this module's readers.
- `lauren/decorators.py` — `propagate_metadata` (copies `@use_*` metadata between objects).
- `lauren/sse.py` — `EventStream`, `ServerSentEvent`, framing.
- `lauren/streaming.py` — `StreamingResponse[T]`, `Stream`,
  `StreamReader`, content-negotiation logic.
- `lauren/background.py` — `BackgroundTasks`, `TaskHandle`, `_BG_TASKS_ATTR`.
- `lauren/context.py` — `ApplicationContext` (`LaurenFactory.create_context`,
  `app.context()`) and `OperationScope` (`app.scope()`).
- `lauren/commands.py` / `lauren/_commands/` — `@command`, `Opt`,
  `CommandContext`, `CommandResult`; the Phase 6a compiler (`_compile`) and the
  `python -m lauren` runner (`_runner`).
- `lauren/config.py` / `lauren/_config/` — `ConfigModule`, `Secret`, sources,
  the coercion table (`_coerce.py`, the one place string → type conversion
  lives) and `install_config`, the factory's Phase 2b (`_module.py`).
- `lauren/sessions.py` / `lauren/_sessions/` — `Session`, `SessionConfig`,
  `SessionStore`, `InMemorySessionStore`, `SignedCookieSessionStore`, the
  serialiser, and the `_SessionEngine` the factory installs. Native
  `session: Session` injection lives in `_asgi` (`source="session"`).
- `lauren/_signing.py` — the one HMAC signer (`Signer`, `normalize_secrets`):
  key rotation (first secret signs, all verify), constant-time verification,
  and `namespace=` so tokens of one feature never verify in another. Sessions
  sign in the empty namespace (`lauren/_sessions/_signing.py` re-exports it),
  pagination cursors in `"lauren.cursor"`; signed URLs (F10) reuse it.
- `lauren/pagination.py` / `lauren/_pagination/` — `PaginationConfig` and its
  startup resolution (`_config.py`), the paging values and per-parameter
  `PaginationPlan` behind the built-in `"pagination"` extraction source
  (`_params.py`), the signed cursor codec (`_cursor.py`), request snapshots and
  `Link` URLs (`_links.py`), `Page` / `CursorPage` rendering — reached from
  `_coerce_to_response` through one `isinstance(value, PageResponse)` check
  until F01's response protocol lands (`_page.py`) — and the single OpenAPI
  contribution function (`_openapi.py`).
- `lauren/decorators.py` — every public decorator. The `_reject_bare_usage`
  pattern is the model for any new decorator.
- `lauren/exceptions.py` — the full error hierarchy (generated table:
   `docs/reference/errors.md`). Pick the closest existing class before adding a new one.
- `lauren/signals.py` — `SignalBus`, lifecycle events, POSIX signal handlers.
- `lauren/stores.py` / `lauren/_stores/` — `Store` protocol, `MemoryStore` and
  its `TTLMap` engine, `PrefixedStore`, `Lock`, `StoresModule` (Phase 2 hook
  `install_stores`); session adapters in `lauren/_sessions/_adapters.py`;
  contract suite in `lauren/testing/store_contract.py`.
- `lauren/cache.py` / `lauren/_cache/` — `Cache` (single-flight, tags,
  stale-while-revalidate), `CacheModule` (Phase 2d `install_cache`), the
  `@cache_response` / `@idempotent` policies and their interceptors, compiled
  in Phase 5 by `compile_cache_routes`. Response-cache keying and header rules
  live together in `_cache/_response.py`.
- `lauren/testing/` — `TestClient` (one persistent loop per client, lifespan
  via `with`), `AsyncTestClient`, `Test.create_testing_module` overrides,
  cookie jar, `WsTestClient`, opt-in `pytest_plugin`.
- `lauren/serialization.py` — JSON encoders: `StdlibJSONEncoder`,
  `OrjsonEncoder`, `MsgspecEncoder`, `PydanticEncoder`; the `json_fallback`
  policy (`_FallbackPolicy`, applied per app through `_ACTIVE_FALLBACK`).
- `lauren/responses.py` — typed results (`Ok`, `Created`, `Accepted`,
  `NoContent`, `Status`); `lauren/_asgi/_contracts.py` — the per-route
  response table (`describe_responses`) and contract plan
  (`compile_response_plan`); `lauren/_schema/_contract.py` — the
  filter/strict projectors built over F02 nodes.
- `lauren/extractors.py` — typed extractors, pipes, `ExtractionMarker`.
- `lauren/_schema/` — the validation engine: `_compiler.py` (`TypeCompiler`,
  annotations → nodes), `_nodes.py` (validate / project / schema per type kind),
  `_params.py` (per-parameter plans, warn/enforce strategies, 422 aggregation),
  `_backends.py` (pydantic / msgspec + error translation), `_jsonschema.py`.
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
precisely than any English prose. The whole suite must stay green.

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

## 13. Releasing

`dev` is the integration branch and `main` the release branch. The version
comes from the git tag through setuptools-scm, so there is no version string to
edit. Pushing a `v<major>.<minor>.<patch>` tag runs
`.github/workflows/release.yml`, which builds the distributions, runs
`nox -s llms_check`, publishes to PyPI (Trusted Publishing) and creates the
GitHub Release. **The tag is the release** — create it only at the end of the
steps below, and only when the maintainer asks you to release.

1. **Prepare the release on `dev`.**
   - Pick the version: `git fetch --tags`, then `nox -s ver_inc -- --minor`
     (new public API or a behaviour change) or `-- --patch` (fixes only, the
     default) prints the next tag after the latest `v*` tag.
   - `CHANGELOG.md`: rename `## [Unreleased]` to `## [X.Y.Z] - YYYY-MM-DD`
     (the release date) and add a new empty `## [Unreleased]` above it. Check
     the list against `git log v<previous>..dev` so every user-visible change
     is there. At the bottom, point `[Unreleased]` at `compare/vX.Y.Z...HEAD`
     and add the `[X.Y.Z]` link.
   - Bring README.md, `docs/`, `skills/`, `lauren/llms.txt` and
     `lauren/llms-full.txt` up to date with the release: new APIs, changed
     defaults, removed behaviour. Regenerate what is generated:
     `python scripts/sync_doc_facts.py`, and `nox -s docs`, which also rewrites
     `docs/generated-reference/`. Never write the version number into prose.
   - Run the gates: `nox -s tests docs_contract llms_check lint typecheck docs`.
   - Commit to `dev` (`meta: prepare release vX.Y.Z`) and push `dev`.
2. **Open the release PR from `dev` into `main`**
   (`gh pr create --base main --head dev`) and wait until every GitHub Actions
   check on it passes. Fix failures on `dev`; never tag a red build.
3. **After the PR is merged, tag the merge commit on `main` and push the tag:**

   ```bash
   git switch main && git pull --ff-only && git fetch --tags
   nox -s ver_inc -- --minor     # prints the tag and the exact commands below
   git tag -a vX.Y.Z -m "Release vX.Y.Z"
   git push origin vX.Y.Z
   ```

4. **Watch the `release` workflow** (`gh run list --workflow release.yml`,
   then `gh run watch <id>`). When it succeeds the package is on PyPI and the
   GitHub Release exists. If the release contains security fixes, publish the
   GitHub Security Advisory (with its CVE) at the same time, as `SECURITY.md`
   requires.
