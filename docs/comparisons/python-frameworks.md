# Lauren vs FastAPI, Litestar & BlackSheep

> **TL;DR.** FastAPI is the easiest path from zero to demo. Litestar and BlackSheep give you more structure when the demo grows up. Lauren is built specifically for the **enterprise long-tail** — the codebase that's still around in five years, run by a rotating team, audited annually, and deployed across multiple environments. This page is the honest, opinionated breakdown.

## The contenders

| Framework | First released | Spiritual lineage | Sweet spot |
|---|---|---|---|
| **FastAPI** | 2018 | Starlette + Pydantic | Solo apps, ML inference services, prototypes that ship |
| **Litestar** (formerly Starlite) | 2021 | Starlette → opinionated NestJS-lite | Mid-sized apps wanting structure without too much ceremony |
| **BlackSheep** | 2018 | ASP.NET Core | Speed-first apps; teams comfortable with Microsoft-style DI |
| **Lauren** | 2025 | Axum + NestJS + FastAPI | Enterprise services; multi-team codebases; long lifetimes |

All four are ASGI-compatible Python web frameworks supporting Pydantic v2 and OpenAPI. They diverge sharply in how *much structure* they impose and *when* they impose it.

---

## At a glance

| Capability | FastAPI | Litestar | BlackSheep | **Lauren** |
|---|---|---|---|---|
| Routing model | Function-based, decorator-discovered | Class or function controllers | Class controllers | **Class controllers, radix-tree, frozen at startup** |
| DI scopes | One (request via `Depends`) | Singleton/Request/Transient | Singleton/Scoped/Transient | **Singleton/Request/Transient + scope-violation checks** |
| Module system | None (router include) | Router-based | None | **NestJS-style modules with imports/exports** |
| Lifecycle hooks | Lifespan only | `on_startup` / `on_shutdown` | Startup/shutdown events | **`@post_construct`/`@pre_destruct` in topological order** |
| Provider Protocol binding | Manual | Limited | Yes | **Yes + multi-bindings + `list[T]` injection** |
| Custom providers (NestJS-style) | No | Some | Limited | **Yes — `use_value`/`use_class`/`use_factory`/`use_existing`** |
| Subclass-decoration semantics | Implicit | Implicit | Implicit | **Strict opt-in (`MetadataInheritanceError`)** |
| Startup-time graph validation | Partial | Partial | Partial | **Full — fails fast on cycles, scopes, ambiguity, missing providers** |
| Built-in error catalog | No | Limited | Limited | **28 error classes with stable codes** |
| Structured JSON logging | No (BYO) | No (BYO) | Limited | **Built-in (`ConsoleLogger`/`JsonLogger`/`InMemory`/`Null`)** |
| Graceful shutdown phases | Lifespan | Limited | Limited | **4-phase: drain → on_shutdown → @pre_destruct → goodbye** |
| AI-ready docs (`llms.txt`) | No | No | No | **Yes — bundled `llms-full.txt`** |
| Auto-serialization of return values | Yes | Yes | Yes | **Yes — dict, model, list, tuple `(body, status, headers)`, dataclass** |
| OpenAPI 3.1 generation | Yes | Yes | Yes | **Yes** |

The right column isn't a marketing flex. Each row corresponds to a **real source of bugs and outages** in production Python services. Read on.

---

## Ergonomics

> *"What does it feel like to write a route?"*

### FastAPI

Function-first. You sprinkle decorators on free functions and import them into a top-level `app`. `Depends` handles DI; type hints handle parsing.

```python
from fastapi import FastAPI, Depends, HTTPException
app = FastAPI()

def get_repo() -> UserRepo: ...
@app.get("/users/{id}")
async def get_user(id: int, repo: UserRepo = Depends(get_repo)):
    user = repo.get(id)
    if not user: raise HTTPException(404)
    return user
```

**Pros:** lowest possible barrier; one file gets you running.
**Cons:** as the codebase grows, the `app` object becomes a magnet — every router, every event, every middleware ends up wired against it. Dependency wiring lives in functions returning functions, which scales poorly.

### Litestar

Class controllers and function handlers both supported. DI is through `Provide`. Has the concept of `Plugin` for cross-cutting features.

```python
from litestar import Controller, get

class UserController(Controller):
    path = "/users"
    @get("/{id:int}")
    async def get_user(self, id: int, repo: UserRepo) -> User: ...
```

**Pros:** more structure than FastAPI; familiar to NestJS users.
**Cons:** the line between "this is a feature, this is a plugin, this is a route guard" can blur as the project grows; module boundaries are not first-class.

### BlackSheep

Class controllers via subclassing. Constructor injection is the default. Heavily inspired by ASP.NET Core.

```python
from blacksheep.server.controllers import Controller, get

class UserController(Controller):
    def __init__(self, repo: UserRepo) -> None:
        self.repo = repo
    @get("/users/{id}")
    async def get_user(self, id: int): ...
```

**Pros:** ergonomic for teams coming from C#/.NET; very fast.
**Cons:** less Python-idiomatic for teams that don't have ASP.NET muscle memory; the IoC container is more "framework-driven" than "graph-validated".

### Lauren

Class controllers, **explicitly registered** via modules:

```python
@controller("/users")
class UserController:
    def __init__(self, repo: UserRepo) -> None:
        self.repo = repo
    @get("/{id}")
    async def show(self, id: Path[int]) -> UserOut: ...

@module(controllers=[UserController], providers=[UserRepo])
class AppModule: ...
```

**Pros:** mental model survives growth — every dependency is *declared* in a module; visibility rules make the graph readable; auto-serialization, custom extractors, custom providers, and DI scopes match what you'd want from NestJS in TypeScript.
**Cons:** requires registering classes in `@module(...)` — slightly more boilerplate than FastAPI's "just slap a decorator on a function". This is a deliberate trade.

---

## Developer Experience

### Type-hint accuracy and editor support

| Framework | `py.typed` | mypy-clean public API | IDE go-to-definition |
|---|---|---|---|
| FastAPI | ✅ | Mostly | ✅ |
| Litestar | ✅ | ✅ | ✅ |
| BlackSheep | ✅ | ✅ | ✅ |
| Lauren | ✅ | ✅ | ✅ |

All four are competent here. The differentiator is what the type checker tells you about *errors*:

```python
# Lauren — type checker catches scope violations because Scope is a typed enum,
# and the @injectable signature mirrors the runtime contract. mypy/pyright will
# flag `provides=[NotAProtocol]` and friends.

@injectable(scope=Scope.REQUEST)
class DbSession: ...

@injectable(scope=Scope.SINGLETON)        # mypy: fine
class Bad:
    def __init__(self, s: DbSession): ... # runtime: DIScopeViolationError at boot
```

The type checker won't catch the scope violation, but **`LaurenFactory.create(...)` will — before any traffic flows**. FastAPI/Litestar/BlackSheep will *typically* not catch this until runtime, if ever.

### Failure timing — when do bugs surface?

This is the single biggest difference between Lauren and the others.

| Bug class | FastAPI | Litestar | BlackSheep | **Lauren** |
|---|---|---|---|---|
| Route-path conflict (two handlers, same `(method, path)`) | Last wins, silent | Sometimes errored | Errored | **`RouterConflictError` at startup** |
| DI cycle (A → B → A) | Hangs / recurses at first request | Errored at first resolve | Errored at first resolve | **`CircularDependencyError` at startup** |
| Missing provider (`UserRepo` not registered) | First-request 500 | First-request 500 | First-request 500 | **`MissingProviderError` at startup** |
| Two providers for same Protocol | First-request ambiguity | Sometimes errored | Sometimes errored | **`ProtocolAmbiguityError` at startup** |
| Scope violation (singleton ← request) | Stale-reference bug at runtime | Sometimes errored | Sometimes errored | **`DIScopeViolationError` at startup** |
| Module export violation | N/A | N/A | N/A | **`ModuleExportViolation` at startup** |
| Subclass accidentally registered as controller | Possible | Possible | Possible | **`MetadataInheritanceError` at startup** |
| Forgotten `Depends`/decorator on subclass | Silent | Silent | Silent | **`MetadataInheritanceError` at startup** |

Lauren's "validate-everything-at-startup" stance means the boot phase is more thorough — and slightly slower — than the others. But here's the trade: **a startup error is a CI failure; a runtime error is a 3 a.m. page**.

### Auto-serialization of handler returns

All four support flexible return types. Lauren goes further with the `(body, status, headers)` tuple:

```python
# Lauren:
@post("/")
async def create(self, body: Json[CreateUser]):
    return body.model_dump(), 201, {"location": f"/users/{body.id}"}

# FastAPI requires more ceremony:
@app.post("/")
async def create(body: CreateUser, response: Response):
    response.status_code = 201
    response.headers["location"] = f"/users/{body.id}"
    return body
```

It's small, but it adds up across hundreds of handlers.

---

## Enterprise readiness

Now the part Lauren was actually built for.

### 1. Configuration validation

Enterprise services don't get the luxury of "fix it on the next deploy". Misconfigurations have to fail in CI, not in production.

* **FastAPI:** very little startup validation. A missing `Depends` lands on the first request.
* **Litestar / BlackSheep:** more startup checks than FastAPI, but module/visibility violations are not modeled — Litestar's router-include and BlackSheep's namespace approach don't have an `exports=[...]` concept.
* **Lauren:** seven-phase startup pipeline. Phase failures raise specific `StartupError` subclasses (15 documented kinds), each addressable by a single grep.

### 2. Module boundaries for multi-team codebases

The single biggest pain in long-lived FastAPI projects is the explosion of *what's reachable from where*. Without explicit module boundaries:

* Any team can import any service, leading to entangled dependency graphs.
* "Internal" services drift into being public APIs by accident.
* Refactoring a service requires grepping the entire repo for imports.

Lauren's `@module(imports=[...], exports=[...])` model — borrowed unchanged from NestJS — solves exactly this. A provider is reachable iff it is declared here or transitively re-exported. Period.

### 3. Lifecycle determinism

When the SRE team needs your service to drain in 30 seconds before kill-9, they need:

* A deterministic order in which connections close.
* Bounded timeouts on each cleanup hook.
* Logged completion of each phase.
* Idempotent re-entry (in case the orchestrator sends two SIGTERMs).

FastAPI's lifespan protocol gives you start/stop hooks but not topological ordering or per-hook timeouts. Litestar and BlackSheep are similar.

Lauren's four-phase shutdown — **drain → `on_shutdown` callbacks → `@pre_destruct` hooks → goodbye**, with bounded timeouts at each step and full structured logging — is built for the SRE who needs the runbook to read like a checklist.

### 4. Stable error contract

Enterprise consumers (other internal services, audit pipelines, partners) require **stable error codes**. A `404` is not enough — they need to programmatically distinguish a "user not found" from a "tenant not found" from a "billing record not found".

Lauren ships **28 error classes**, every HTTP-mapped one with a documented `code`. Every error renders as:

```json
{"error": {"code": "user_not_found", "message": "...", "detail": {...}}}
```

with the *same* envelope across the entire framework. FastAPI / Litestar / BlackSheep give you `HTTPException` and you build the envelope yourself — which means it's not consistent across your services unless you maintain a shared library.

### 5. Strict inheritance rules

Subclassing for code reuse is essential in big codebases. Subclassing that *silently* turns a helper class into a registered controller is a bug factory.

Lauren is the **only** framework in this comparison that enforces "inheritance does not propagate decorations" — see [Class Inheritance Rules](../core-concepts/inheritance.md). The first time it bites a junior dev's PR, it saves a future security review.

### 6. Structured logging out of the box

FastAPI gives you `logging` and a vague suggestion. Litestar and BlackSheep ship some structured logging, but the per-request trace format isn't standardized.

Lauren ships:

* `ConsoleLogger` — coloured, human-readable, TTY-aware.
* `JsonLogger` — one-line JSON for production aggregators (Splunk, Datadog, OpenObserve).
* `NullLogger` and `InMemoryLogger` for tests.
* Per-request traces auto-leveled by status: `DEBUG` 2xx/3xx, `WARN` 4xx, `ERROR` 5xx.
* Per-phase startup events (factory entry, module graph, DI compile, route registration, lifecycle, ready).
* Shutdown phase events (drain, callbacks, hooks, goodbye).

Configure once, shipping immediately to your aggregator with the schema your existing dashboards already expect.

### 7. AI-ready docs

This one isn't an enterprise requirement strictly — but every enterprise team uses Copilot / Cursor / Claude Code now, and giving the model the *right* mental model matters.

Lauren ships an `llms.txt` and `llms-full.txt` (~25 KB) at the package root. Paste them into your AI assistant's context and you get idiomatic Lauren on the first try, not the second or third. None of the other three frameworks ship machine-ingestible docs in the [llmstxt.org](https://llmstxt.org) format.

---

## When NOT to pick Lauren

We're going to be honest. Lauren is **not** the right pick for:

* **A 50-line script.** Use FastAPI. The decorator-on-function approach is genuinely faster for prototypes.
* **A team that wants implicit, "just works" magic.** Lauren is opinionated about explicitness. If `MetadataInheritanceError: register me explicitly` reads as friction rather than a feature, FastAPI or Litestar will feel friendlier.
* **A team that doesn't believe in IoC.** Lauren is built around DI. If your team has "we just import what we need" as a guideline, the boilerplate of `@module(providers=[...])` will feel arbitrary.

## When Lauren is the obvious answer

* **Multi-team services** where module boundaries matter more than minimal boilerplate.
* **Long-lived services** where "what depends on what" needs to be visible to every reviewer.
* **Audit-heavy environments** that need stable error codes, structured logs, and deterministic shutdown.
* **Migrations from NestJS / Axum** that want the same mental model in Python.
* **FastAPI codebases** that have outgrown the function-and-Depends approach and need DI scopes, lifecycle hooks, and module imports.

---

## A side-by-side: same feature, four frameworks

> Implement a `/users/{id}` endpoint with a repository injected, a 404 envelope, and a logger that records every miss.

=== "Lauren"

    ```python
    from lauren import (
        LaurenFactory, controller, get, module, injectable, Path,
    )
    from lauren.exceptions import HTTPError
    from lauren.logging import Logger

    class UserNotFound(HTTPError):
        status_code = 404
        code = "user_not_found"

    @injectable()
    class UserRepo:
        def get(self, id: int): ...

    @controller("/users")
    class UserController:
        def __init__(self, repo: UserRepo, log: Logger) -> None:
            self.repo, self.log = repo, log

        @get("/{id}")
        async def show(self, id: Path[int]) -> dict:
            user = self.repo.get(id)
            if user is None:
                self.log.warn(f"user {id} not found")
                raise UserNotFound("user does not exist", detail={"id": id})
            return {"id": user.id, "name": user.name}

    @module(controllers=[UserController], providers=[UserRepo])
    class AppModule: ...

    app = LaurenFactory.create(AppModule)
    ```

=== "FastAPI"

    ```python
    from fastapi import FastAPI, Depends, HTTPException
    import logging

    app = FastAPI()
    log = logging.getLogger("app")

    class UserRepo:
        def get(self, id: int): ...

    def get_repo() -> UserRepo:
        return UserRepo()  # how do we share an instance? bring a Container...

    @app.get("/users/{id}")
    async def show(id: int, repo: UserRepo = Depends(get_repo)) -> dict:
        user = repo.get(id)
        if user is None:
            log.warning("user %s not found", id)
            raise HTTPException(
                status_code=404,
                detail={"code": "user_not_found", "id": id},
            )
        return {"id": user.id, "name": user.name}
    ```

=== "Litestar"

    ```python
    from litestar import Controller, get, Litestar, Provide
    from litestar.exceptions import HTTPException
    import logging

    log = logging.getLogger("app")

    class UserRepo:
        def get(self, id: int): ...

    class UserController(Controller):
        path = "/users"
        dependencies = {"repo": Provide(UserRepo)}

        @get("/{id:int}")
        async def show(self, id: int, repo: UserRepo) -> dict:
            user = repo.get(id)
            if user is None:
                log.warning("user %s not found", id)
                raise HTTPException(status_code=404, detail="user_not_found")
            return {"id": user.id, "name": user.name}

    app = Litestar(route_handlers=[UserController])
    ```

=== "BlackSheep"

    ```python
    from blacksheep.server import Application
    from blacksheep.server.controllers import Controller, get
    import logging

    log = logging.getLogger("app")

    class UserRepo:
        def get(self, id: int): ...

    class UserController(Controller):
        def __init__(self, repo: UserRepo) -> None:
            self.repo = repo

        @get("/users/{id}")
        async def show(self, id: int):
            user = self.repo.get(id)
            if user is None:
                log.warning("user %s not found", id)
                return self.json({"code": "user_not_found", "id": id}, status=404)
            return {"id": user.id, "name": user.name}

    app = Application()
    app.services.add_singleton(UserRepo)
    ```

The character counts are similar. The **structural differences** are what matter at scale: Lauren's `@module(...)` makes visibility explicit; the typed `Path[int]` says it without an extra path-syntax DSL; the `HTTPError` subclass renders the structured envelope automatically with no per-handler shape repetition.

---

## Final scorecard

A subjective summary, with the same trade-offs we'd give a colleague choosing a stack:

| Criterion | FastAPI | Litestar | BlackSheep | **Lauren** |
|---|---|---|---|---|
| Time-to-hello-world | 🟢 5 min | 🟢 10 min | 🟢 10 min | 🟡 15 min |
| Ergonomics for solo devs | 🟢🟢 | 🟢 | 🟢 | 🟡 |
| Ergonomics for 5-person teams | 🟢 | 🟢 | 🟢 | 🟢🟢 |
| Ergonomics for 50-person teams | 🟡 | 🟢 | 🟢 | 🟢🟢 |
| Type safety end-to-end | 🟢 | 🟢🟢 | 🟢 | 🟢🟢 |
| Startup-time validation | 🟡 | 🟢 | 🟢 | 🟢🟢 |
| Production logging out of the box | 🔴 | 🟡 | 🟡 | 🟢🟢 |
| Graceful shutdown semantics | 🟡 | 🟡 | 🟢 | 🟢🟢 |
| Stable error contract | 🔴 | 🟡 | 🟡 | 🟢🟢 |
| Multi-team module discipline | 🔴 | 🟡 | 🟡 | 🟢🟢 |
| Audit-friendliness | 🟡 | 🟢 | 🟢 | 🟢🟢 |
| Raw runtime performance | 🟢 | 🟢 | 🟢🟢 | 🟢🟢 |
| Ecosystem & docs (today) | 🟢🟢 | 🟢 | 🟢 | 🟡 |

If your axis is *"how fast can I ship a prototype?"*, FastAPI wins.
If your axis is *"will this app survive the next five on-call rotations?"*, Lauren wins.

That's the honest pitch.

---

## See also

* [Why Lauren?](../getting-started/why-lauren.md) — the design philosophy in full.
* [Prominent Features](../getting-started/features.md) — every flagship feature, with examples.
* [Core Concepts](../core-concepts/index.md) — the mental model the comparison is built on.
