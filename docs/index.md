# Lauren

> **A metadata-first, enterprise-ready Python web framework.**
> Inspired by **Axum** (Rust), **FastAPI**, and **NestJS** — built for ASGI services that have to survive on-call rotations, audits, and growing engineering teams.

<div class="grid cards" markdown>

-   :material-rocket-launch: __Predictable startup, predictable runtime__

    ---

    Every route, dependency, module boundary, and lifecycle hook is resolved into an **immutable execution graph** at startup. The hot path is pure traversal — no reflection, no surprises.

-   :material-shield-check: __Fail-fast configuration__

    ---

    Misconfigurations (cycles, scope violations, missing providers, ambiguous Protocols) are caught **before the first request**. Your CI pipeline detects what your customers would otherwise.

-   :material-puzzle: __Composable by design__

    ---

    Modules with explicit `imports`/`exports`, three DI scopes, Protocol binding, multi-bindings, custom providers, custom extractors, exception handlers, guards, and onion-model middleware. All composable. All typed.

-   :material-chart-bell-curve-cumulative: __Built for enterprise__

    ---

    Structured JSON logging, graceful shutdown with POSIX signal integration, OpenAPI 3.1, deterministic teardown, and a 28-class error catalog with stable error codes.

</div>

---

## In 30 lines

```python
import asyncio
from pydantic import BaseModel
from lauren import (
    LaurenFactory, controller, get, post, module,
    Path, Json, use_guards, ExecutionContext, injectable,
)

class CreateUser(BaseModel):
    name: str
    age: int

@injectable()
class AdminGuard:
    async def can_activate(self, ctx: ExecutionContext) -> bool:
        return ctx.request.headers.get("x-role") == "admin"

@controller("/users", tags=["users"])
class UserController:
    @get("/{id}")
    async def get_user(self, id: Path[int]) -> dict:
        return {"id": id, "found": True}

    @post("/")
    async def create(self, body: Json[CreateUser]):
        return body.model_dump(), 201          # body + status

    @get("/admin")
    @use_guards(AdminGuard)
    async def admin_only(self) -> dict:
        return {"access": "granted"}

@module(controllers=[UserController])
class AppModule:
    pass

async def main():
    app = await LaurenFactory.create(AppModule)
    # `app` is an ASGI callable — serve with uvicorn / hypercorn.
```

---

## Where to next?

<div class="grid cards" markdown>

-   :material-school: [__Getting Started__](getting-started/index.md)

    ---
    Install Lauren, run your first app, and tour its prominent features.

-   :material-cube-outline: [__Core Concepts__](core-concepts/index.md)

    ---
    Modules, controllers, injectables, and the strict inheritance rules that keep the graph honest.

-   :material-tools: [__Guides__](guides/index.md)

    ---
    Declare injectables, write custom extractors, guards, middleware, providers, and exception handlers.

-   :material-scale-balance: [__Comparisons__](comparisons/python-frameworks.md)

    ---
    See how Lauren stacks up against **FastAPI**, **Litestar**, and **BlackSheep** for ergonomics, DX, and enterprise readiness.

</div>

---

## What "enterprise-ready" actually means here

Most Python frameworks claim it. Lauren earns it through four hard guarantees:

| Guarantee | What it means in practice |
|---|---|
| **Validate-at-startup** | Misconfigured DI graphs, route conflicts, circular modules, scope violations and ambiguous Protocols all raise `StartupError` subclasses *before* the app accepts a single connection. |
| **Deterministic shutdown** | A four-phase shutdown (drain → `on_shutdown` → `@pre_destruct` → goodbye) with bounded timeouts, idempotent re-entry, and full structured logging. SIGTERM/SIGINT integration is one function call. |
| **Stable error contract** | A 28-class error catalog. Every HTTP error renders as `{"error": {"code": "...", "message": "...", "detail": {...}}}` with documented codes that downstream services can match against. |
| **Zero runtime reflection** | The dispatch path never calls `inspect`, `get_type_hints`, or any reflective API. Performance is predictable across cold and hot paths because the work is all done once, at boot. |

When the pager goes off at 3 a.m., the framework should be the boring part of the stack. Lauren is built to be exactly that.
