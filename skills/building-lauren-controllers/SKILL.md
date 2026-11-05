---
name: building-lauren-controllers
description: Writes Lauren controllers with route handlers, typed extractors, and auto-serialization. Covers @controller, @get/@post/@put/@patch/@delete, Path/Query/Header/Cookie/Json/Form/Bytes extractors, field descriptors, pipes, all return-type patterns, propagate_metadata, and reflect_routes/get_all_routes. Use when adding routes, extractors, or HTTP handlers to a Lauren app.
---

> Use `codemap find "SymbolName"` to locate any symbol before reading — it gives
> exact file + line range and is faster than grep across the whole repo.


# Lauren Controllers

## Controller skeleton

```python
from lauren import NotFoundError, controller, get, post, put, delete, patch, raises
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

    @post("/", status_code=201)      # declared success status (sent + documented)
    async def create(self, body: Json[CreateUserDto]) -> UserDto:
        return await self._svc.create(body)

    @put("/{id}")
    async def update(self, id: Path[int], body: Json[UpdateUserDto]) -> UserDto:
        return await self._svc.update(id, body)

    @delete("/{id}")
    @raises(NotFoundError)          # documents the 404 in OpenAPI
    async def remove(self, id: Path[int]) -> None:
        await self._svc.remove(id)
        # return None → 204 No Content (documented as 204)
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

## Validation and the 422 payload

Stdlib models are validated natively (no pydantic needed); constraints ride in
`Annotated` metadata from `lauren.validation`:

```python fragment
from dataclasses import dataclass
from typing import Annotated
from lauren.validation import Ge, MaxLen, Pattern, ValidationPolicy, validation_config

@validation_config(extra="forbid")          # unknown keys → extra_forbidden
@dataclass
class CreateUser:
    email: Annotated[str, Pattern(r"@"), MaxLen(254)]
    age: Annotated[int, Ge(13)]

app = LaurenFactory.create(AppModule, validation=ValidationPolicy(stdlib_models="enforce"))
```

Every validation 422 lists **all** failing parameters in
`error.detail.issues`: `[{"loc": ["query", "page"], "type": "int_parsing",
"msg": "...", "ctx"?: {...}}, ...]` — `loc` starts with `path` / `query` /
`header` / `cookie` / `body` / `form`. Clients should read `issues`, not the
1.x `detail.errors` strings. In 1.x the default `stdlib_models="warn"` only
logs dataclass/TypedDict failures; a model that uses markers or
`@validation_config` is always enforced. Full guide: `docs/guides/validation.md`.

## Auto-serialization return types

```python fragment
return {"ok": True}             # → JSON 200
return "plain text"             # → text/plain 200
return None                     # → 204 No Content
return Created(user, location=f"/users/{user.id}")   # → JSON 201 + Location (typed result)
return body, 201                # → JSON 201 (tuple shorthand — not documented in OpenAPI)
return Response.json(data, status=202)          # explicit Response
return Response.redirect("/new-url", status=301)
```

Pydantic models, dataclasses, and `msgspec.Struct` values serialize out of the box.

## Response contracts — status codes and output shape

- Declare the success status with `@post("/", status_code=201)` or a typed
  result in the annotation: `-> Created[UserDto]`, `-> Ok[UserDto] | Created[UserDto]`,
  `-> NoContent`, `-> Annotated[Chunk, Status(206)]` (`from lauren import Ok, Created,
  Accepted, NoContent, Status`). Contradictions (204 with a body type,
  `status_code=200` on `-> Created[T]`) fail at startup.
- `LaurenFactory.create(..., response_validation="filter")` (or per route
  `@get(..., response_validation="filter")`) projects the returned value onto the
  annotated type: return the entity (`UserInDB`, an ORM row), annotate the DTO
  (`-> UserDto`), and only the DTO's fields leave. `"strict"` also validates;
  a value that does not fit is a 500 `response_validation_error`.
- `@raises(NotFoundError, ConflictError)` documents error responses.
- Prefer typed results over `Response.created(...)`: they use the route's encoder.

Full guide: `docs/guides/response-contracts.md`.
## Pagination

List routes take a paging parameter and return a page — no hand-rolled
`limit=min(100, max(1, limit))` clamps or ad-hoc envelopes:

```python fragment
from typing import Annotated

from lauren import CursorPage, CursorPagination, OffsetPagination, Page, PaginationConfig

@get("/")
async def list_users(self, paging: OffsetPagination) -> Page[UserOut]:      # ?limit=&offset=
    rows, total = await self.repo.list(limit=paging.limit, offset=paging.offset)
    return Page.of(rows, paging, total=total)                               # or has_next=...

@get("/export")
async def export(self, paging: Annotated[OffsetPagination, PaginationConfig(max_limit=1000)]) -> Page[UserOut]:
    rows = await self.repo.list(limit=paging.limit + 1, offset=paging.offset)
    return Page.from_rows(rows, paging)                                     # probe row → has_next

@get("/events")
async def events(self, paging: CursorPagination[EventKey]) -> CursorPage[EventOut]:
    rows = await self.repo.page(paging.cursor, paging.direction, paging.limit + 1)
    return CursorPage.from_rows(rows, paging, key=lambda e: EventKey(e.created_at, e.id))
```

- Bad values are a 422 on `["query", "limit"]`; the maximum is a hard cap (default 100).
- The envelope (`"items"` / `"data"` / `"bare"`), `meta` key style, `Link` / `X-Total-Count`
  headers and the cursor secret are app-wide: `LaurenFactory.create(..., pagination=PaginationConfig(...))`.
- `CursorPagination` needs `PaginationConfig(cursor_secret=...)` (startup error otherwise);
  a tampered cursor is a 400 `invalid_cursor`. `PageNumberPagination` reads `?page=&per_page=`.
- `paginate_sequence(items, paging)` paginates an in-memory list.

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

Raise Lauren's built-in HTTP errors — they serialize to the standard envelope.
Subclass one for a domain `code`; never re-declare `NotFoundError` & co.:

```python
from lauren import ConflictError, NotFoundError, TooManyRequestsError

class UserNotFound(NotFoundError):          # 404, code "user_not_found"
    code = "user_not_found"

@get("/{id}")
async def find_one(self, id: Path[int]) -> UserDto:
    user = await self._svc.find_one(id)
    if user is None:
        raise UserNotFound(f"User {id} not found", detail={"id": id})
    return user

@post("/")
async def create(self, body: Json[CreateUser]) -> UserDto:
    if await self._svc.email_taken(body.email):
        raise ConflictError("email already registered", code="email_taken")   # instance code override
    if await self._svc.over_quota():
        raise TooManyRequestsError("slow down", retry_after=30)                # Retry-After: 30
    return await self._svc.create(body)
```

Built in: `BadRequestError` 400, `UnauthorizedError` 401 (`challenge=` →
`WWW-Authenticate`), `ForbiddenError` 403, `NotFoundError` 404,
`MethodNotAllowedError` 405 (`allow=` → `Allow`), `NotAcceptableError` 406,
`ConflictError` 409, `GoneError` 410, `PreconditionFailedError` 412,
`UnsupportedMediaTypeError` 415, `TooManyRequestsError` 429,
`ServiceUnavailableError` 503, `RequestTimeoutError` 504 (`retry_after=` on the
last three). Every error accepts `headers=`. `RouteNotFoundError` is reserved for
routing misses. See `docs/reference/errors.md` (generated from
`lauren.exceptions`) for the full catalog, and `error_format="rfc9457"` for RFC
9457 problem details.

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
