---
name: building-lauren-controllers
description: Writes Lauren controllers with route handlers, typed extractors, and auto-serialization. Covers @controller, @get/@post/@put/@patch/@delete, Path/Query/Header/Cookie/Json/Form/Bytes extractors, field descriptors, pipes, and all return-type patterns. Use when adding routes, extractors, or HTTP handlers to a Lauren app.
---

> Use `codemap find "SymbolName"` to locate any symbol before reading — it gives
> exact file + line range and is faster than grep across the whole repo.


# Lauren Controllers

## Controller skeleton

```python
from lauren import controller, get, post, put, delete, patch
from lauren.types import ExecutionContext, Path, Query, Json

@controller("/users", tags=["users"])
class UsersController:
    def __init__(self, users_service: UsersService) -> None:
        self._svc = users_service

    @get("/")
    async def find_all(self, page: Query[int] = 1, limit: Query[int] = 20) -> list[UserDto]:
        return await self._svc.find_all(page, limit)

    @get("/{id}")
    async def find_one(self, id: Path[int]) -> UserDto:
        return await self._svc.find_one(id)

    @post("/")
    async def create(self, body: Json[CreateUserDto]) -> UserDto:
        user = await self._svc.create(body)
        return user, 201   # tuple: (data, status_code)

    @put("/{id}")
    async def update(self, id: Path[int], body: Json[UpdateUserDto]) -> UserDto:
        return await self._svc.update(id, body)

    @delete("/{id}")
    async def remove(self, id: Path[int]) -> None:
        await self._svc.remove(id)
        # return None → 204 No Content
```

- `@controller` makes the class `REQUEST`-scoped automatically — DI injects services per request.
- `@use_guards`, `@use_interceptors`, `@use_middlewares` can decorate either the class or individual methods.

## Extractors at a glance

| Annotation | Source | Notes |
|---|---|---|
| `Path[T]` | URL segment `{name}` | Auto-applied when param name matches segment |
| `Query[T]` | `?key=value` | `list[str]` collects multi-value |
| `Header[T]` | request header | case-insensitive |
| `Cookie[T]` | cookie jar | |
| `Json[T]` | JSON body | T must be `BaseModel`; 422 on validation error |
| `Form[T]` | form-urlencoded or multipart | |
| `Bytes` | raw body `bytes` | |
| `Depends[T]` | DI container | resolves `T` from the container |
| `State` | `request.state` | reads per-request mutable state |

**Implicit promotion** — bare parameters (no extractor marker) are auto-promoted:
- Name matches a `{segment}` → `Path[T]`
- Annotation is a `BaseModel` → `Json[T]`
- Annotation is `int/str/float/bool` → `Query[T]`

See [extractors.md](extractors.md) for field descriptors, pipes, and upload files.

## Auto-serialization return types

```python
return {"ok": True}             # → JSON 200
return "plain text"             # → text/plain 200
return None                     # → 204 No Content
return body, 201                # → JSON 201 (tuple shorthand)
return Response.json(data, status=202)          # explicit Response
return Response.redirect("/new-url", status=301)
```

Pydantic models are serialized via `.model_dump()`. Dataclasses work too.

## Sync handlers

Sync handlers run in a thread pool automatically (no event-loop blocking):

```python
@get("/slow")
def compute(self) -> dict:      # note: def, not async def
    import time
    time.sleep(1)               # safe — offloaded via anyio.to_thread
    return {"done": True}
```

## Exception handling in handlers

Raise Lauren's typed HTTP errors — they serialize to the standard envelope:

```python
from lauren.exceptions import UnauthorizedError, ForbiddenError, RouteNotFoundError

@get("/{id}")
async def find_one(self, id: Path[int]) -> UserDto:
    user = await self._svc.find_one(id)
    if user is None:
        raise RouteNotFoundError(f"User {id} not found")
    return user
```

See `lauren.exceptions` for the full 28-class catalog.
