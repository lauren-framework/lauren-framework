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

# All tests (1247 pass, 11 skipped, ~16s on a modern laptop):
pytest -q

# Single layer:
pytest tests/unit/ -q
pytest tests/integration/ -q

# Single file or pattern:
pytest tests/unit/test_websockets_decorators.py -v
pytest -k "di and not websocket" -q

# Build the docs site (requires docs-requirements.txt):
make docs                    # mkdocs build --strict
make docs-serve              # live-reload at http://localhost:8000

# Build a release wheel:
make build                   # → ./dist/*.whl + ./dist/*.tar.gz
make build-check             # twine check
```

## What Agents Should Always Do

1. **Run the full test suite before and after every change.** The
   suite is fast (~16s for 1247 tests). A green `pytest -q` is the
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
| `lauren/_di/__init__.py`          | DI container, provider graph, cycles         |
| `lauren/_di/custom.py`            | `use_value` / `use_class` / `use_factory` /  |
|                                   | `use_existing` / `Token` / `Inject`          |
| `lauren/_routing/__init__.py`     | Radix-tree router                            |
| `lauren/_modules/__init__.py`     | Module graph, imports/exports validation     |
| `lauren/_lifecycle/__init__.py`   | post_construct / pre_destruct scheduler      |
| `lauren/_typing/`                 | ForwardRef / PEP 563 resolver                |
| `lauren/_ws_runtime.py`           | WebSocket dispatch loop (private)            |
| `lauren/decorators.py`            | User-facing decorators only                  |
| `lauren/extractors.py`            | Typed extractors + pipes + custom extractors |
| `lauren/exceptions.py`            | 28-class error hierarchy                     |
| `lauren/streaming.py`             | StreamingResponse[T], Stream, StreamReader   |
| `lauren/sse.py`                   | EventStream, ServerSentEvent, last_event_id  |
| `lauren/websockets.py`            | @ws_controller, @on_message, BroadcastGroup  |
| `lauren/socketio.py`              | Engine.IO/Socket.IO adapter (public)         |
| `lauren/serialization.py`         | JSON encoders (stdlib, orjson, msgspec)      |
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
  / `@middleware` / `@ws_controller` / `@exception_handler` decoration.

## Companion Packages

When fixing a logging, middleware or auth concern, **first check whether
the right home is a companion package**, not core. The framework stays
small on purpose; cross-cutting concerns live next door.

- `lauren-middlewares` — CORS, rate limit, GZip, security headers,
  request id, trusted hosts, request log, HTTPS redirect, body size
  limit, timeout. Each ships as a factory function returning a
  `@middleware`-decorated class.
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

- [ ] `pytest -q` passes (1247+ tests).
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
3. Run `pytest -q` once. If it doesn't pass on a fresh clone, that's a
   bug to report before doing anything else.
4. Read `.CLAUDE.md` rules 3 (strict inheritance) and 8 (28-class
   error catalog). They explain decisions that look strange until you
   know why.

Welcome aboard.
