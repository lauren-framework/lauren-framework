# AGENTS.md — Instructions for Coding Agents

This repository welcomes contributions from AI coding agents (Claude
Code, Cursor, Aider, Codex, Continue, etc.). This file is the
human-readable mirror of `.CLAUDE.md` and is the canonical entry
point consulted by agents that follow the [agents.md][agents-md]
convention.

[agents-md]: https://agents.md

## Quick Start

```bash
pip install -e ".[dev]"

# All tests (3297 pass, ~60s on a modern laptop):
pytest -q

# Single layer:
pytest tests/unit/ -q
pytest tests/integration/ -q
pytest tests/e2e/ -q
pytest tests/property/ -q   # requires hypothesis (included in [dev])

# Single file or pattern:
pytest tests/unit/test_websockets_decorators.py -v
pytest -k "di and not websocket" -q

# Build the docs site (requires docs-requirements.txt):
make docs                    # mkdocs build --strict
make docs-serve              # live-reload at http://localhost:8000

# Build a release wheel:
make build                   # → ./dist/*.whl + ./dist/*.tar.gz
make build-check             # twine check

# Derive the next semantic tag before a release:
nox -s ver_inc -- --minor
nox -s ver_dec -- --patch
```

## What Agents Should Always Do

1. **Run the full test suite before and after every change.** The
   suite runs in ~60s for 3297 tests. A green `pytest -q` is the
   objective acceptance signal for every pull request.
2. **Read `.CLAUDE.md` first.** It contains the design invariants. An
   agent proposal that violates those invariants should be rejected
   even if it passes tests.
3. **Add tests for every new behaviour.** Follow the unit/integration
   split in `tests/`. Name the test after the behaviour, not the
   internal symbol being tested.
4. **Prefer smaller, focused patches.** A feature is a separate PR
   from its documentation; a refactor is a separate PR from its
   consumers.
5. **Update `llms-full.txt`, `llms.txt`, and `README.md`** whenever a
   public name changes. These files are the machine-readable
   reference; agents downstream of yours rely on them.
6. **Update `docs/`** when a feature has a teaching story that goes
   beyond the API surface — the docs site is built with MkDocs
   Material, with `mkdocs.yml` controlling the navigation. Strict
   build (`mkdocs build --strict`) must pass.

## What Agents Should Never Do

- Never call `typing.get_type_hints` in framework code. Use
  `lauren._typing.resolve_type_hints`.
- Never mutate `__dict__` on decorated objects except to set a
  `__lauren_<feature>__` sentinel.
- Never introduce global mutable state. Everything lives on the
  `DIContainer` or the `LaurenApp`.
- Never add a runtime reflection call on the request path.
- Never swallow exceptions silently. Lenient fallbacks are allowed
  only when they surface a typed sentinel (e.g. `ForwardRef`) that
  the caller can detect.
- Never accept a *bare* decorator usage (`@my_decorator` instead of
  `@my_decorator()`) when the decorator takes options — Python
  silently passes the decoratee as the first arg. Reject loudly with
  `DecoratorUsageError`; the helper `_reject_bare_usage` in
  `decorators.py` is the model.
- Never auto-inherit decoration on subclasses. The framework's strict
  inheritance rule is one of its load-bearing invariants. Subclasses
  must explicitly re-decorate to opt in.

## File-by-File Ownership

| Path                              | Contract                                     |
| --------------------------------- | -------------------------------------------- |
| `lauren/__init__.py`              | Public API surface + `__all__`               |
| `lauren/_app.py`                  | `Lauren` high-level class                    |
| `lauren/_asgi/__init__.py`        | ASGI adapter, the runtime scheduler          |
| `lauren/_asgi/_openapi.py`        | OpenAPI 3.1 generator                        |
| `lauren/_di/__init__.py`          | DI container, provider graph, cycles; `_GeneratorContextWrapper` for generator provider lifecycle |
| `lauren/_di/custom.py`            | `use_value` / `use_class` / `use_factory` /  |
|                                   | `use_existing` / `Token` / `Inject`          |
| `lauren/_discriminated.py`        | `Discriminated[A\|B,"key"]` — detection, validation, OpenAPI schema |
| `lauren/_encoders/`               | Optional encoder backends (e.g. `pydantic.py`) |
| `lauren/_routing/__init__.py`     | Radix-tree router                            |
| `lauren/_modules/__init__.py`     | Module graph, imports/exports validation     |
| `lauren/_lifecycle/__init__.py`   | post_construct / pre_destruct scheduler; sync hooks run in thread pool via asyncio.to_thread (timeout-protected, event-loop-safe); second pass runs generator provider teardown (code after yield) for SINGLETON scope |
| `lauren/_typing/`                 | ForwardRef / PEP 563 resolver                |
| `lauren/_validation.py`           | Provider-agnostic type detection + validation (pydantic, msgspec, dataclass, TypedDict) |
| `lauren/_ws_runtime.py`           | WebSocket dispatch loop + `WsConnectionContext` / `WsUpgradeRequest` |
| `lauren/reflect/`                 | Full metadata introspection API — static readers, app readers, result types, WS composers |
| `lauren/reflect/_reader.py`       | Static readers: `reflect_controller`, `reflect_routes`, `reflect_exception_handlers`, … |
| `lauren/reflect/_app_reader.py`   | App readers: `get_all_routes`, `get_all_ws_gateways`, `get_route_metadata` |
| `lauren/reflect/_types.py`        | Frozen result types: `ReflectedRoute`, `ReflectedController`, `ReflectedWsGateway`, … |
| `lauren/decorators.py`            | User-facing decorators only                  |
| `lauren/extractors.py`            | Typed extractors + pipes + custom extractors |
| `lauren/exceptions.py`            | 29-class error hierarchy                     |
| `lauren/sessions.py`              | Public session surface (`Session`, `SessionConfig`, stores) |
| `lauren/_sessions/`               | Session engine, stores, HMAC signing, serializer, config |
| `lauren/streaming.py`             | StreamingResponse[T], Stream, StreamReader   |
| `lauren/sse.py`                   | EventStream, ServerSentEvent, last_event_id  |
| `lauren/types.py`                 | Request, Response, State, Headers, Scope, `Discriminated`, … |
| `lauren/websockets.py`            | @ws_controller, @on_message, BroadcastGroup  |
| `lauren/socketio.py`              | Engine.IO/Socket.IO adapter (public)         |
| `lauren/serialization.py`         | JSON encoders: `StdlibJSONEncoder`, `OrjsonEncoder`, `MsgspecEncoder`, `PydanticEncoder`; encoder threaded app-wide; `@use_encoder(enc)` overrides per-route or per-controller (method > controller > app) |
| `lauren/logging.py`               | NestJS-style logger (built-in, separate from |
|                                   | the optional `lauren-logging` companion)     |
| `lauren/signals.py`               | POSIX signal integration                     |
| `lauren/testing.py`               | `TestClient`, `WsTestClient`                 |
| `lauren/docs.py`                  | Programmatic access to llms*.txt             |
| `lauren/llms-full.txt`            | Public-API reference for AI ingestion        |
| `lauren/llms.txt`                 | Short overview (llmstxt.org convention)      |

## Conventions Cheatsheet

- Python 3.11+, PEP 604 unions, `from __future__ import annotations`
  at the top of every module.
- Tests use pytest + `pytest-asyncio`. `asyncio_mode = "auto"` is set
  in `pyproject.toml`.
- Decorators attach metadata with name `__lauren_<thing>__` and return
  the decoratee unchanged. Bare usage is rejected with
  `DecoratorUsageError`.
- Errors inherit from `LaurenError` and ship a `detail` dict with
  machine-parseable keys (`target`, `param`, `token`, ...).
- HTTP-mapped errors render as
  `{"error": {"code": "...", "message": "...", "detail": {...}}}` —
  the envelope is stable across the entire framework.
- Subclasses do NOT inherit `@injectable` / `@controller` / `@module`
  / `@middleware()` / `@ws_controller` / `@exception_handler` decoration.

## Companion Packages

When fixing a logging, middleware or auth concern, **first check whether
the right home is a companion package**, not core. The framework stays
small on purpose; cross-cutting concerns live next door.

- `lauren-middlewares` — CORS, rate limit, GZip, security headers,
  request id, trusted hosts, request log, HTTPS redirect, body size
  limit, timeout. Each ships as a factory function returning a
  `@middleware()`-decorated class.
- `lauren-logging` — Configurable logging module (a NestJS-style
  `LoggerModule.forRoot(...)`). Processor pipeline, contextvars
  binding, request-logging middleware, pluggable backends. Built on
  top of stock `lauren>=1.0` with no framework changes required.
  Three `@classmethod` presets on `LoggingConfig` cover the common
  cases without remembering every knob:
  - `LoggingConfig.for_development()` → `ConsoleBackend` at `DEBUG`.
  - `LoggingConfig.for_production(backend, ...)` → any backend at `INFO`.
  - `LoggingConfig.for_testing()` → `(config, InMemoryBackend)` for assertions.
- `lauren-guards` — Authentication and authorization guards. All guard
  classes are decorated with `@injectable(scope=Scope.SINGLETON)` so
  the DI container manages them. Guards are designed to be subclassed
  for application-specific extensions (e.g. a public-route bypass).
  - Authentication guards: `bearer_token`, `jwt_bearer`, `api_key`,
    `basic_auth`, `oauth2_introspection`, `session_cookie`.
  - Authorization guards: `require_authenticated`, `require_roles`,
    `require_scopes`.
  - Cross-cutting: `csrf` (double-submit-cookie), `ip_allowlist`.
  - Password utilities: `BcryptHasher`, `Argon2Hasher`, `generate_token`.
  - Session utilities: `InMemorySessionStore`, `sign_cookie`, `verify_cookie`.
    Note: core now ships first-class sessions (`lauren.sessions`); the
    `session_cookie` guard remains an authentication layer and may, in a
    follow-up, build on core's `SessionStore` Protocol and signing.

## How to Propose Large Changes

For anything that touches more than two modules or changes the public
surface:

1. Open an issue with the title `RFC: <feature>`.
2. Describe the user-facing shape first (code examples).
3. Describe the internal change second.
4. Link every affected file.
5. Mark test scenarios with `- [ ]` checkboxes.
6. Note any docs page (under `docs/`) that needs adding or updating.

Agents should not silently ship RFC-sized patches; always split them.

## Definition of Done

A change is ready for merge when:

- [ ] `pytest -q` passes (3297 tests).
- [ ] New behaviour has at least one test in the matching layer.
- [ ] Public API changes are reflected in `__all__`, `llms.txt`,
      and `llms-full.txt`.
- [ ] User-facing additions have a doc page or section under `docs/`.
- [ ] `mkdocs build --strict` passes (no broken links).
- [ ] Commit messages follow `<scope>: <imperative>` format.
- [ ] No TODO / FIXME / XXX strings remain in the diff.

## A Quick Tour for New Agents

If you've never touched this codebase before, this is the fastest
path to "I understand what I'm doing":

1. Read `lauren/llms-full.txt`. It's the same content the docs site
   surfaces, condensed into ~25 KB you can paste into your context
   window.
2. Skim `tests/integration/`. Each file is a real-world usage pattern
   for one feature. The shapes there are the shapes the framework
   actually supports.
3. Run `pytest -q` once. If it doesn't pass on a fresh clone (3297
   tests), that's a bug to report before doing anything else.
4. Read `.CLAUDE.md` rules 3 (strict inheritance) and 8 (28-class
   error catalog). They explain decisions that look strange until you
   know why.

Welcome aboard.

## By-Task Quick Lookup

| I need to… | Read first | Copy-paste guide |
|---|---|---|
| Add a new route / controller | `lauren/_asgi/__init__.py` | `skills/building-lauren-controllers/` |
| Wire DI / providers / lifecycle | `lauren/_di/__init__.py` | `skills/building-lauren-services/` |
| Add a guard, middleware, or interceptor | `lauren/decorators.py` | `skills/building-lauren-guards/` |
| Add exception handlers | `lauren/decorators.py` | `skills/building-lauren-guards/` §Exception handlers |
| Add WebSocket / SSE / streaming | `lauren/websockets.py`, `lauren/sse.py` | `skills/building-lauren-streaming/` |
| Serve static files | `lauren/_staticfiles.py` | `skills/building-lauren-apps/` §Static files |
| Write unit or integration tests | `tests/integration/test_di.py` | `skills/testing-lauren-apps/` |
| Add a background task | `lauren/background.py` | `skills/building-lauren-background-tasks/` |
| Add session state | `lauren/sessions.py` | `skills/building-lauren-sessions/` |
| Debug a startup error | `lauren/exceptions.py` | **Common Errors** section below |
| Port from FastAPI | `llms-full.txt` §Guards | `skills/migrating-from-fastapi/` |
| Add CORS / auth guards / logging | `AGENTS.md` §Companion Packages | `skills/using-companion-packages/` |
| Copy a production-ready pattern | `tests/integration/` | `skills/common-patterns/` |

## Skills Quick Index

| Task | Skill directory |
|---|---|
| Project layout, `LaurenFactory`, root module | `skills/building-lauren-apps/` |
| Route handlers, extractors, pipes, serialization | `skills/building-lauren-controllers/` |
| DI scopes, custom providers, lifecycle hooks | `skills/building-lauren-services/` |
| Guards, interceptors, middleware | `skills/building-lauren-guards/` |
| SSE, WebSocket gateways, `StreamingResponse` | `skills/building-lauren-streaming/` |
| `BackgroundTasks`, `TaskHandle` | `skills/building-lauren-background-tasks/` |
| Sessions: signed cookies, `Session` injection, pluggable store | `skills/building-lauren-sessions/` |
| `TestClient`, async tests, mock providers | `skills/testing-lauren-apps/` |
| FastAPI → lauren side-by-side equivalents | `skills/migrating-from-fastapi/` |
| CORS, auth guards, structured logging | `skills/using-companion-packages/` |
| Copy-paste: CRUD, health check, background job, SSE | `skills/common-patterns/` |
| Build a first-party or third-party companion package | `skills/building-companion-packages/` |

Full index: [`skills/README.md`](skills/README.md)

## Docs Map

| Concept | Most relevant file |
|---|---|
| Dependency injection deep-dive | `docs/guides/dependency-injection.md` |
| Custom extractor plugin API | `docs/guides/custom-extractors.md` |
| Guard vs middleware vs interceptor | `docs/concepts/extractors-vs-dependencies-vs-guards-vs-middlewares.md` |
| Strict inheritance rules (why subclasses must re-decorate) | `docs/core-concepts/inheritance.md` |
| WebSocket patterns | `docs/guides/websockets.md` |
| Guards on WebSocket gateways | `docs/guides/custom-guards.md` §WebSocket gateways |
| Reflect module — full introspection API | `docs/reference/reflect.md` |
| Copying decorator metadata between objects | `@propagate_metadata` in `lauren.decorators`; `docs/guides/propagate-metadata.md` |
| Enumerating all HTTP routes / WS gateways at runtime | `get_all_routes`, `get_all_ws_gateways` in `lauren.reflect` |
| SSE / streaming | `docs/guides/sse.md` |
| Session management | `docs/guides/sessions.md` |
| Custom response subclasses and response factories | `docs/guides/custom-responses.md` / `docs/guides/file-responses.md` |
| Testing playbook | `skills/testing-lauren-apps/SKILL.md` |
| Release / versioning process | `docs/development/release.md` / `docs/development/versioning.md` |

## Common Startup Errors

| Error class | Most common cause | Fix |
|---|---|---|
| `MetadataInheritanceError` | Subclass of `@controller` / `@injectable` / `@module` not re-decorated | Re-apply the decorator on the subclass |
| `ModuleExportViolation` | Provider injected across module boundary but not listed in `exports=[]` | Add the type to `exports=` in the owning module |
| `CircularDependencyError` | A → B → A in the DI graph | Break with `use_factory` or restructure modules |
| `DecoratorUsageError` | `@middleware` / `@injectable` used bare without `()` | Change to `@middleware()` / `@injectable()` |
| `DuplicateRouteError` | Two handlers registered on the same method + path | Rename one route |
| `UnresolvableProviderError` | Type not registered anywhere, or owning module not imported | Import the owning module in the consumer module |
| `SessionConfigError` | Sessions misconfigured, or `Session` injected with sessions disabled | Pass `sessions=SessionConfig(...)` / fix the unsafe cookie config |
| `StartupError` (generic) | Missing required parameter in constructor injection | Add the type as a `@module` provider or import its module |

## Interceptor `CallHandler.handle()` contract (v1.4.2+)

`call_handler.handle()` inside an interceptor **always returns a `Response`** — the raw handler return value (dict, Pydantic model, tuple, etc.) is coerced before interceptors see it.

```python
# Correct — no isinstance guard needed:
@interceptor()
class TimingInterceptor:
    async def intercept(self, ctx, ch: CallHandler) -> Response:
        t0 = time.monotonic()
        result = await ch.handle()          # Response, always
        return result.with_header("x-ms", f"{(time.monotonic()-t0)*1000:.0f}")

# Wrong — result is never a dict:
@interceptor()
class Bad:
    async def intercept(self, ctx, ch):
        result = await ch.handle()
        if isinstance(result, dict):        # ← never True
            result["key"] = "value"
        return result
```

To modify JSON body content, parse and rebuild:

```python
import json
result = await ch.handle()
data = json.loads(result.body)
data["key"] = "value"
return result.with_body(json.dumps(data).encode())
```
