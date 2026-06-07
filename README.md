<p align="center">
  <img src="https://raw.githubusercontent.com/lauren-framework/lauren-assets/refs/heads/main/framework/lauren-logo-only.png" width=40%></img>
</p>
<div align="center">
  <h1><i>lauren</i></h1>
</div>
<p align="center">
    <em>lauren framework: a metadata-first Python web framework. NestJS-style modules, Axum-style extractors, FastAPI-class ergonomics &mdash; resolved into an immutable execution graph at startup.</em>
</p>
<p align="center">
<a href="https://github.com/lauren-framework/lauren-framework/actions/workflows/tests.yml?query=branch%3Amain+event%3Apush">
    <img src="https://github.com/lauren-framework/lauren-framework/actions/workflows/tests.yml/badge.svg?branch=main&event=push" alt="Test">
</a>
<a href="https://github.com/lauren-framework/lauren-framework/actions/workflows/lint.yml?query=branch%3Amain+event%3Apush">
    <img src="https://github.com/lauren-framework/lauren-framework/actions/workflows/lint.yml/badge.svg?branch=main&event=push" alt="Lint">
</a>
<a href="https://github.com/lauren-framework/lauren-framework/actions/workflows/codeql.yml?query=branch%3Amain">
    <img src="https://github.com/lauren-framework/lauren-framework/actions/workflows/codeql.yml/badge.svg?branch=main" alt="CodeQL">
</a>
<a href="https://codecov.io/gh/lauren-framework/lauren-framework">
    <img src="https://img.shields.io/codecov/c/github/lauren-framework/lauren-framework?color=%2334D058&label=coverage" alt="Coverage">
</a>
<a href="https://pypi.org/project/lauren">
    <img src="https://img.shields.io/pypi/v/lauren?color=%2334D058&label=pypi%20package" alt="Package version">
</a>
<a href="https://pypi.org/project/lauren">
    <img src="https://img.shields.io/pypi/pyversions/lauren.svg?color=%2334D058" alt="Supported Python versions">
</a>
<a href="https://pypi.org/project/lauren">
    <img src="https://img.shields.io/pypi/dm/lauren.svg?color=%2334D058&label=downloads" alt="Downloads">
</a>
<a href="https://github.com/lauren-framework/lauren-framework/blob/main/LICENSE">
    <img src="https://img.shields.io/github/license/lauren-framework/lauren-framework.svg?color=%2334D058" alt="License">
</a>
<a href="https://github.com/astral-sh/ruff">
    <img src="https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json" alt="Ruff">
</a>
<a href="https://mypy.readthedocs.io/en/stable/">
    <img src="https://img.shields.io/badge/types-mypy-blue.svg" alt="Checked with mypy">
</a>
<a href="https://github.com/j178/prek">
    <img src="https://img.shields.io/badge/pre--commit-prek-FAB040.svg?logo=pre-commit&logoColor=white" alt="prek">
</a>
<a href="https://github.com/lauren-framework/lauren-framework/discussions">
    <img src="https://img.shields.io/github/discussions/lauren-framework/lauren-framework?color=%2334D058&label=discussions" alt="Discussions">
</a>
<a href="https://github.com/lauren-framework/lauren-framework/stargazers">
    <img src="https://img.shields.io/github/stars/lauren-framework/lauren-framework.svg?style=social&label=Star" alt="GitHub Stars">
</a>
</p>

---

**Documentation**: <a href="https://lauren-py.dev" target="_blank">https://lauren-py.dev</a>

**Source Code**: <a href="https://github.com/lauren-framework/lauren-framework" target="_blank">https://github.com/lauren-framework/lauren-framework</a>

---

## For AI Agents & Coding Assistants

### Install all skills in one command

```bash
# Claude Code, Cursor, Copilot, Continue, Codex CLI — auto-detected
npx skills add lauren-framework/lauren-framework
```

This copies all 60+ SKILL.md context packs into your agent's global skills
directory (`~/.claude/skills/`, `~/.cursor/skills/`, etc.).  The next time your
agent opens a Lauren project it has pre-loaded expertise on auth, database,
API patterns, observability, security, and more.

| Resource | What it contains |
|---|---|
| [`lauren/llms.txt`](https://raw.githubusercontent.com/lauren-framework/lauren-framework/refs/heads/main/lauren/llms.txt) | 2 KB framework overview — start here |
| [`lauren/llms-full.txt`](https://raw.githubusercontent.com/lauren-framework/lauren-framework/refs/heads/main/lauren/llms-full.txt) | 25 KB complete reference — all APIs, patterns, common errors |
| [`AGENTS.md`](https://github.com/lauren-framework/lauren-framework/blob/main/AGENTS.md) | Agent rules, by-task lookup, file ownership, common errors, definition of done |
| [`CLAUDE.md`](https://github.com/lauren-framework/lauren-framework/blob/main/CLAUDE.md) | Conventions, commands, golden rules, pattern selection guide |
| [`skills/`](https://github.com/lauren-framework/lauren-framework/tree/main/skills/) | 60+ copy-paste skill guides covering every common task |

Full install guide and skills index: [docs/guides/agent-skills.md](https://github.com/lauren-framework/lauren-framework/blob/main/docs/guides/agent-skills.md)

---

lauren is a modern, high-performance Python web framework for building APIs
that need to **fail at startup, not in production**. It is built on these
core ideas:

* **Metadata-first.** Routes, dependency-injection bindings, module
  boundaries, lifecycle hooks, middleware, and guards are declared with
  decorators that *attach metadata*. They never rewrite your functions.
* **Startup-validated, runtime-pure.** Every misuse — circular DI, missing
  module export, malformed extractor, conflicting routes — is rejected
  inside `LaurenFactory.create(...)`, not on the first request.
* **No reflection on the request path.** The whole graph is compiled into
  immutable structures at startup; serving a request is pure traversal.
* **AI-ready by default.** Public surface is mirrored in
  [`llms.txt` / `llms-full.txt`](https://lauren-py.dev/llms.txt) and exported
  in `__all__` — a CI hook keeps the two in lock-step.

## Features

* **Fast** — Zero per-request reflection:
  routes, DI graph, extractors, and middleware are fully compiled at startup.
* **Implicit extractors** — Path params, query strings, and JSON bodies are
  auto-detected from type annotations. Write `id: int`, `q: str`,
  `body: MyModel` without boilerplate unless you need the explicit form.
  `Query[T]` and `Json[T]` support Pydantic models, `msgspec.Struct`,
  Python `dataclass`, and `TypedDict` types.
* **Pydantic-free discriminated unions** — `Discriminated[Cat | Dog, "kind"]`
  routes tagged-union JSON bodies to the correct variant class using only
  stdlib. Works with `@dataclass`, `TypedDict`, `msgspec.Struct`, and
  `pydantic.BaseModel`. OpenAPI emits `oneOf` + `discriminator.mapping`
  automatically.
* **Three-scope DI** — `SINGLETON`, `REQUEST`, and `TRANSIENT` scopes with
  Protocol bindings, multi-bindings (`list[T]`), and NestJS-style custom
  providers (`use_value` / `use_class` / `use_factory` / `use_existing`).
* **Pipes** — Post-extraction value transforms: validate, coerce, or enrich
  parameters before they reach the handler. Function-based, class-based,
  chainable, and DI-aware.
* **Guards, Middleware & Interceptors** — All three attachment points. Guards
  run first (allow/deny); middleware wraps raw request/response bytes; interceptors
  wrap handler execution for timing, caching, and response transforms.
* **WebSockets** — First-class `@ws_controller` gateways with typed validated
  frames, discriminated-union dispatch (`Discriminated[A | B, "key"]`), and
  `BroadcastGroup` rooms.
* **Server-Sent Events** — `EventStream` with keep-alive heartbeats and
  `Last-Event-ID` resumability for AI token-streaming patterns.
* **Typed streaming** — `StreamingResponse[T]` auto-negotiates between SSE,
  NDJSON, and JSON Lines based on the client's `Accept` header.
* **Custom responses & file delivery** — Return your own `Response` subclasses,
  stream downloads with `await Response.file(...)`, or emit XML with
  `Response.xml(...)` while keeping the dispatch pipeline untouched.
* **Background tasks** — `BackgroundTasks` extractor fires work after the
  response is sent. `TaskHandle` exposes cancel/await. Signals notify on
  start, complete, and failure.
* **Static files** — `StaticFilesModule.for_root("/assets", directory="./public")`
  with ETag caching, `Cache-Control`, and path-traversal protection.
* **Socket.IO** — Engine.IO v4 / Socket.IO v5 adapter via `@socketio_controller`.
* **OpenAPI 3.1** — Automatic schema generation from Pydantic models,
  dataclasses, `TypedDict`, and `Discriminated` unions. Swagger UI and ReDoc
  served out of the box.
* **Lifecycle signals** — `SignalBus` with `StartupBegin`, `StartupComplete`,
  `ShutdownBegin`, `RequestReceived`, `RequestComplete`, and more.
* **Standards-based** — Built on [ASGI](https://asgi.readthedocs.io/) and
  [anyio](https://anyio.readthedocs.io/). Pydantic is optional (`pip install
  "lauren[pydantic]"`).

## Requirements

Python **3.11**, **3.12**, **3.13**, and **3.14** are supported. Hard dependencies:

* [anyio](https://anyio.readthedocs.io/) — async backend and thread-pool offload for sync handlers.

Optional extras:

* `pip install "lauren[pydantic]"` — adds `pydantic>=2.0` for Pydantic-backed validation and `PydanticEncoder`.
* `pip install "lauren[msgspec]"` — adds `msgspec>=0.18` for struct-based serialisation.
* `pip install "lauren[full]"` — installs both.

## Installation

```bash
pip install lauren
# with an ASGI server:
pip install "uvicorn[standard]"
# or granian (faster on CPython):
pip install granian
```

## Quick start

```python
from pydantic import BaseModel
from lauren import LaurenFactory, controller, get, post, module, use_guards
from lauren.types import ExecutionContext


class CreateUser(BaseModel):
    name: str
    age: int


class AdminGuard:
    async def can_activate(self, ctx: ExecutionContext) -> bool:
        return ctx.request.headers.get("x-role") == "admin"


@controller("/users", tags=["users"])
class UserController:
    @get("/{id}")
    async def get_user(self, id: int) -> dict:
        return {"id": id, "found": True}

    @post("/")
    async def create(self, body: CreateUser):
        return body.model_dump(), 201

    @get("/admin")
    @use_guards(AdminGuard)
    async def admin_only(self) -> dict:
        return {"access": "granted"}


@module(controllers=[UserController])
class AppModule:
    pass


app = LaurenFactory.create(AppModule, docs_url="/docs")
```

```bash
uvicorn main:app --reload
# → http://127.0.0.1:8000/docs  (Swagger UI)
```

## Dependency injection

```python
from lauren import injectable, post_construct, pre_destruct, Scope

@injectable(scope=Scope.SINGLETON)
class UserRepository:
    @post_construct
    async def warm(self) -> None:
        self.cache: dict[int, str] = {}

    @pre_destruct
    async def flush(self) -> None:
        self.cache.clear()

    async def find(self, user_id: int) -> str | None:
        return self.cache.get(user_id)

    async def upsert(self, user_id: int, name: str) -> None:
        self.cache[user_id] = name


@controller("/users")
class UserController:
    def __init__(self, repo: UserRepository) -> None:
        self.repo = repo

    @get("/{id}")
    async def get_user(self, id: int) -> dict:
        name = await self.repo.find(id)
        if name is None:
            return {"id": id, "found": False}, 404
        return {"id": id, "name": name}


@module(providers=[UserRepository], controllers=[UserController])
class AppModule:
    pass
```

## Modules

Modules are the unit of feature composition. Each module declares what it
provides, what it exports, and what it imports from other modules — similar
to NestJS:

```python
@module(
    imports=[DatabaseModule, AuthModule],
    controllers=[UserController, ProfileController],
    providers=[UserService, EmailService],
    exports=[UserService],
)
class UsersModule:
    pass
```

Circular dependency detection, missing export errors, and scope violations
are all caught at startup — before your first request.

## SSE / streaming

```python
from lauren import EventStream, ServerSentEvent, get

@get("/events")
async def stream(self) -> EventStream:
    async def generate():
        for i in range(10):
            yield ServerSentEvent(data=f"tick {i}", event="tick")
            await asyncio.sleep(1)
        yield ServerSentEvent(data="done", event="close")
    return EventStream(generate(), keep_alive=15.0)
```

For typed, content-negotiated streams (SSE / NDJSON / JSON Lines):

```python
from pydantic import BaseModel
from lauren import StreamingResponse

class Tick(BaseModel):
    seq: int

@get("/ticks")
async def ticks(self) -> StreamingResponse[Tick]:
    async def gen():
        for i in range(100):
            yield Tick(seq=i)
            await asyncio.sleep(0.05)
    return StreamingResponse(gen())
```

## Static files

```python
from lauren.static_files import StaticFilesModule

@module(imports=[StaticFilesModule.for_root("/assets", directory="./public")])
class AppModule:
    pass
```

Or mount any ASGI sub-app:

```python
app = LaurenFactory.create(AppModule)
app.mount("/static", StaticFiles(directory="static"))
```

## Performance

Runtime is **pure traversal of pre-compiled structures** — no
`inspect.signature(...)`, no `get_type_hints(...)`, no `isinstance(...)`
walking on the hot path. The DI graph, route table, extractor bindings, and
middleware pipeline are all resolved once at startup. Each request pays only
the cost of dispatching through the already-compiled graph.

## Optional dependencies

| Package | Purpose |
|---|---|
| `uvicorn` / `hypercorn` / `granian` | ASGI server (none bundled — pick one) |
| `httpx` | Required for `lauren.testing.TestClient` |
| `orjson` | Faster JSON — auto-detected at import time |
| `msgspec` | Alternative fast JSON encoder via `MsgspecEncoder` |
| `python-multipart` | Required for `Form[...]` extractors |

## Companion packages

| Package | Purpose |
|---|---|
| [`lauren-middlewares`](https://github.com/lauren-framework/lauren-middlewares) | CORS, rate-limit, GZip, security headers, request-id, trusted hosts, HTTPS redirect, body-size limit, timeout |
| [`lauren-logging`](https://github.com/lauren-framework/lauren-logging) | Structured logging module with processor pipeline, contextvars binding, pluggable backends (stdlib, structlog, file, fan-out) |
| [`lauren-guards`](https://github.com/lauren-framework/lauren-guards) | Auth guards: JWT bearer, API key, basic auth, OAuth2 introspection, session cookie, RBAC/ABAC, CSRF, IP allowlist |

## Deployment

lauren is a standard ASGI application — deploy exactly like FastAPI or Starlette:

```bash
# Uvicorn
uvicorn main:app --host 0.0.0.0 --port 8000 --workers 4

# Granian (Rust-based, faster on CPython)
granian --interface asgi main:app --host 0.0.0.0 --port 8000

# Hypercorn (HTTP/2 + HTTP/3)
hypercorn main:app --bind 0.0.0.0:8000
```

## Contributing

We welcome contributions of every size, from typo fixes to whole subsystems.
Read first:

1. [`CONTRIBUTING.md`](https://github.com/lauren-framework/lauren-framework/blob/main/CONTRIBUTING.md) — setup, branch & commit conventions, and the quality bar.
2. [`AGENTS.md`](https://github.com/lauren-framework/lauren-framework/blob/main/AGENTS.md) — the design invariants every PR must respect, whether the author is human or an AI agent.

```bash
uv tool install prek      # one-time
prek install              # wires up the git hook
nox                       # lint + tests + typecheck
```

## License

MIT — see [LICENSE](https://github.com/lauren-framework/lauren-framework/blob/main/LICENSE).
