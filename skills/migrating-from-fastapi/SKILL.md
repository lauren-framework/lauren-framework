---
name: migrating-from-fastapi
description: Ports Python web applications from FastAPI to the Lauren framework. Provides side-by-side equivalents for routing, dependency injection, middleware, lifespan hooks, and error handling. Use when converting FastAPI code to Lauren or when a user is familiar with FastAPI and needs Lauren equivalents.
---

# Migrating from FastAPI to Lauren

> Use `codemap find "SymbolName"` to locate any symbol before reading — it gives
> exact file + line range and is faster than grep.

FastAPI and Lauren share the "type-annotations-first" philosophy but differ in architecture. Lauren is module-scoped (NestJS-style) rather than app-scoped; the DI container owns provider lifetimes rather than `Depends()` chains.

## Quick comparison

| FastAPI | Lauren | Notes |
|---|---|---|
| `FastAPI()` | `LaurenFactory.create(AppModule)` | App is built from a root `@module`, not an instance |
| `APIRouter` | `@module` + `@controller` | Modules own controllers; controllers own routes |
| `@app.get("/")` | `@get("/")` on a `@controller` method | Same HTTP verbs, same path syntax |
| `Depends(fn)` | Constructor injection via `@injectable` | Services declared as class attributes / constructor params |
| `@app.middleware("http")` | `@middleware()` class with `dispatch(req, call_next)` | Class-based, DI-aware |
| `@app.on_event("startup")` | `@post_construct` on an `@injectable` | Lifecycle tied to individual services, not the whole app |
| `HTTPException(status_code=404)` | `raise NotFoundError("msg")` | built-in HTTP errors (`BadRequestError`, `NotFoundError`, `ConflictError`, `TooManyRequestsError`, …) importable from `lauren` |
| `HTTPException(401, headers={"WWW-Authenticate": "Bearer"})` | `raise UnauthorizedError("msg", challenge="Bearer")` | every error takes `headers=`; `retry_after=` on 429/503/504 |
| `Annotated[str, Query()]` | `q: Query[str]` (or bare `q: str`) | Extractors are type aliases, not annotations |
| `Annotated[int, Query(le=100)]` / `Field(ge=13)` | `size: Annotated[int, Le(100)] = 10`, `age: Annotated[int, Ge(13)]` | Markers from `lauren.validation`; annotated-types objects work too |
| `RequestValidationError` (422 `detail: [{loc, msg, type, input, ctx}]`) | `ExtractorError` (422 `error.detail.issues: [{loc, type, msg, ctx}]`) | Same `loc` roots (`["query", "page"]`, `["body", "age"]`); every failing parameter in one response |
| `response_model=` | Return the Pydantic model directly | Auto-serialized; declare return type for OpenAPI |
| `Depends(get_db)` for request-scoped deps | `@injectable(scope=Scope.REQUEST)` | Scope is on the provider class, not the injection site |

## Routing side-by-side

**FastAPI:**
```python
from fastapi import FastAPI, APIRouter

app = FastAPI()
router = APIRouter(prefix="/users")

@router.get("/{id}")
async def get_user(id: int):
    return {"id": id}

app.include_router(router)
```

**Lauren:**
```python
from lauren import module, controller, get

@controller("/users")
class UserController:
    @get("/{id}")
    async def get_user(self, id: int) -> dict:
        return {"id": id}

@module(controllers=[UserController])
class UsersModule: ...
```

See [routing.md](routing.md) for path params, query params, body models, and response models.

## Dependency injection side-by-side

**FastAPI:**
```python
from fastapi import Depends

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

@app.get("/items")
async def list_items(db=Depends(get_db)):
    return db.query(Item).all()
```

**Lauren:**
```python
from lauren import injectable, Scope

@injectable(scope=Scope.REQUEST)
class Database:
    def __init__(self) -> None:
        self._db = SessionLocal()

    @pre_destruct
    async def close(self) -> None:
        self._db.close()

@controller("/items")
class ItemController:
    def __init__(self, db: Database) -> None:
        self._db = db

    @get("/")
    async def list_items(self) -> list:
        return self._db.query(Item).all()
```

See [di.md](di.md) for the full DI scope model and custom providers.

## Middleware side-by-side

**FastAPI:**
```python
from starlette.middleware.base import BaseHTTPMiddleware

class TimingMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        start = time.monotonic()
        response = await call_next(request)
        response.headers["X-Time"] = str(time.monotonic() - start)
        return response

app.add_middleware(TimingMiddleware)
```

**Lauren:**
```python
from lauren import middleware, injectable, Scope
from lauren.types import Request, Response

@middleware()
@injectable(scope=Scope.SINGLETON)
class TimingMiddleware:
    async def dispatch(self, request: Request, call_next) -> Response:
        start = time.monotonic()
        response = await call_next(request)
        # Responses are immutable: with_header() returns a new one.
        return response.with_header("x-time", str(time.monotonic() - start))
```

Pass to `LaurenFactory.create(AppModule, global_middlewares=[TimingMiddleware])`
(or `Lauren(global_middlewares=[TimingMiddleware])`).

See [middleware.md](middleware.md) for the full middleware + interceptor model.

## Error handling side-by-side

**FastAPI:**
```python
from fastapi import HTTPException

@app.get("/item/{id}")
async def get_item(id: int):
    if not found:
        raise HTTPException(status_code=404, detail="Item not found")
```

**Lauren:**
```python
from lauren import NotFoundError, TooManyRequestsError, UnauthorizedError

class ItemNotFound(NotFoundError):     # optional: a domain code, same 404
    code = "item_not_found"

@get("/{id}")
async def get_item(self, id: int) -> dict:
    if not found:
        raise ItemNotFound("Item not found")  # → 404 JSON response
    if over_quota:
        raise TooManyRequestsError("slow down", retry_after=30)          # Retry-After: 30
    if not token:
        raise UnauthorizedError("missing token", challenge="Bearer")     # WWW-Authenticate
```

Built-in HTTP-mapped classes: `BadRequestError` (400), `UnauthorizedError`
(401), `ForbiddenError` (403), `NotFoundError` (404; `RouteNotFoundError` for
unmatched routes), `MethodNotAllowedError` (405), `NotAcceptableError` (406),
`ConflictError` (409), `GoneError` (410), `PreconditionFailedError` (412),
`RequestBodyTooLarge` (413), `UnsupportedMediaTypeError` (415),
`ExtractorError` / `ExtractorFieldError` (422), `TooManyRequestsError` (429),
`ServiceUnavailableError` (503) and `RequestTimeoutError` (504) — full generated
table in `docs/reference/errors.md`. FastAPI's `HTTPException(status_code=...,
headers=...)` maps to the matching class with `headers=` (or
`HTTPError("...", status_code=418, code="teapot")` for a one-off status).
`error_format="rfc9457"` emits RFC 9457 problem details.

**Validation errors.** FastAPI's `RequestValidationError` body
`{"detail": [{"loc", "msg", "type", "input", "ctx"}]}` becomes
`{"error": {"code": "extractor_error", "message": ..., "detail": {"issues": [{"loc", "type", "msg", "ctx"}]}}}`:
the same `loc` / `type` vocabulary (pydantic errors are translated field for
field), with `input` echoed only when `debug` is on. Point a FastAPI client at
`error.detail.issues` instead of `detail`. Dataclass and `TypedDict` bodies are
validated natively too — pass `validation=ValidationPolicy(stdlib_models="enforce")`
to `LaurenFactory.create` to reject bad ones (1.x default `"warn"` only logs).
