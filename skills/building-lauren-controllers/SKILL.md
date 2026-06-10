---
name: building-lauren-controllers
description: Writes Lauren controllers with route handlers, typed extractors, and auto-serialization. Covers @controller, @get/@post/@put/@patch/@delete, Path/Query/Header/Cookie/Json/Form/Bytes extractors, field descriptors, pipes, all return-type patterns, propagate_metadata, and reflect_routes/get_all_routes. Use when adding routes, extractors, or HTTP handlers to a Lauren app.
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
| `Json[T]` | JSON body | Supports Pydantic models, `msgspec.Struct`, Python dataclasses, `TypedDict`, and `Discriminated[A\|B,"key"]`; 422 on validation error |
| `Form[T]` | form-urlencoded or multipart | |
| `Bytes` | raw body `bytes` | Buffers entire body |
| `ByteStream` | streaming body chunks | Zero-copy async iterator — no intermediate `bytes` join |
| `UploadFile` | multipart file upload | `.filename`, `.content_type`, `.content` (bytes); `list[UploadFile]` for multiple files |
| `Depends[T]` | DI container | resolves `T` from the container |
| `State` | `request.state` | reads per-request mutable state |

**Implicit promotion** — bare parameters (no extractor marker) are auto-promoted:
- Name matches a `{segment}` → `Path[T]`
- Annotation is a Pydantic model, `msgspec.Struct`, dataclass, `TypedDict`, or `Discriminated[A|B,"key"]` → `Json[T]`
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

Pydantic models, dataclasses, and `msgspec.Struct` values serialize out of the box.

## Choosing the JSON encoder

```python
from lauren import use_encoder
from lauren.serialization import PydanticEncoder

@controller("/audit")
@use_encoder(PydanticEncoder())
class AuditController:
    @get("/latest")
    async def latest(self) -> AuditEvent:
        return await self._svc.latest()
```

- App-wide: `LaurenFactory.create(..., json_encoder=...)`
- Controller/route override: `@use_encoder(...)`
- The active encoder also powers structured error responses and SSE payloads returned from HTTP handlers

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

---

## Enumerating routes at runtime

After startup, `get_all_routes(app)` returns every compiled HTTP route:

```python
from lauren.reflect import get_all_routes, get_route_metadata

for route in get_all_routes(app):
    print(route.method, route.full_path, route.handler.__name__)

route = get_route_metadata(app, "GET", "/users/{id}")
if route:
    print(route.guards, route.tags)
```

To enumerate routes on a controller class *before* startup:

```python
from lauren.reflect import reflect_routes, get_controller_metadata

for r in reflect_routes(UserController):
    print(r.method, r.full_path)   # GET /users/{id}

meta = get_controller_metadata(UserController)
if meta:
    print(meta.guards, meta.routes)
```

## Sharing metadata across controllers

`@propagate_metadata(source)` copies all `@use_*` annotations from a source
class or function to the decorated target (see
`skills/building-lauren-guards/SKILL.md` for full details).
