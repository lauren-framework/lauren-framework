---
name: common-patterns
description: Provides copy-paste complete Lauren framework patterns for the most common production scenarios. Covers authenticated CRUD endpoints, health check module, background job with lifecycle hooks, and typed SSE streaming. Use when scaffolding a new feature or when a complete working example is needed rather than API reference.
---

# Common Lauren Patterns

> Use `codemap find "SymbolName"` to locate any symbol before reading — it gives
> exact file + line range and is faster than grep.

Complete, copy-pasteable patterns. Each is a working snippet you can drop into a project with minimal changes.

## Patterns

- **Authenticated CRUD**: controller with JWT guard, Pydantic models, DI service → [auth-protected-crud.md](auth-protected-crud.md)
- **Health check module**: minimal `GET /health` + `GET /ready` endpoint → [health-check.md](health-check.md)
- **Background job**: fire-and-forget task with `@post_construct`/`@pre_destruct` + shutdown signal → [background-job.md](background-job.md)
- **Typed SSE stream**: `StreamingResponse[T]` with Pydantic model, content-negotiation → [typed-sse-stream.md](typed-sse-stream.md)

## Quick reference

### Minimal module + controller

```python
from lauren import module, controller, get, injectable, Scope

@injectable(scope=Scope.SINGLETON)
class GreetService:
    def greet(self, name: str) -> str:
        return f"Hello, {name}!"

@controller("/greet")
class GreetController:
    def __init__(self, svc: GreetService) -> None:
        self._svc = svc

    @get("/{name}")
    async def greet(self, name: str) -> dict:
        return {"message": self._svc.greet(name)}

@module(controllers=[GreetController], providers=[GreetService])
class GreetModule: ...
```

### Request-scoped provider with cleanup

```python
from lauren import injectable, Scope, post_construct, pre_destruct

@injectable(scope=Scope.REQUEST)
class DbSession:
    @post_construct
    async def open(self) -> None:
        self._conn = await pool.acquire()

    @pre_destruct
    async def close(self) -> None:
        await pool.release(self._conn)
```

### Custom provider token

```python
from lauren import Token, use_value, module

Settings = Token[dict]("Settings")

@module(providers=[use_value(provide=Settings, value={"env": "prod"})])
class AppModule: ...

# Inject anywhere:
class MyService:
    def __init__(self, settings: Settings) -> None: ...
```
