# Pipes

> A **pipe** is a transform that runs *after* a value is extracted from the request. Pipes validate, coerce, enrich, or replace that value before it reaches the handler. They compose with extractors and field descriptors using the `|` operator and are declared at the parameter level — so the logic stays with the parameter, not scattered across the handler body.

## The mental model

Lauren's extraction pipeline for every handler parameter runs in three ordered stages:

```
HTTP request
    │
    ▼
[1] Extraction        Path[int] → "42" → 42
[2] Field validation  PathField(ge=1) → 42 ≥ 1 ✓
[3] Pipes             pipe(lookup) → User(id=42)   ← your code lives here
    │
    ▼
Handler receives: User(id=42)
```

Extractors decide *where* the value comes from. Field descriptors add *constraints* on the raw value. Pipes decide *what to do with it afterwards* — lookup, enrich, reshape, reformat.

## Quick start

```python
from lauren import controller, get
from lauren.extractors import Path, pipe

def slugify(value: str, ctx) -> str:
    return value.lower().replace(" ", "-")

@controller("/articles")
class ArticleController:
    @get("/{title}")
    async def get(self, title: Path[str] = pipe(slugify)) -> dict:
        return {"slug": title}
```

`GET /articles/Hello World` → handler receives `"hello-world"`.

## Declaring pipes — two equivalent syntaxes

Pipes can be placed in either of two positions for any extractor:

```python
from typing import Annotated
from lauren.extractors import Path, PathField, pipe

# ── Annotated form ────────────────────────────────────────────────
# All metadata lives in the annotation. Preferred when you also
# have a FieldDescriptor or multiple pipes to express in one place.
async def get_a(
    self,
    id: Annotated[Path[int], PathField(ge=1), pipe(lookup)],
): ...

# ── Default form ──────────────────────────────────────────────────
# Uses the | operator to chain. Preferred for short one-pipe cases
# where the annotation would become unwieldy.
async def get_b(
    self,
    id: Path[int] = PathField(ge=1) | pipe(lookup),
): ...
```

Both forms produce the same extraction plan at startup. Use whichever reads better.

## Writing a pipe function

The simplest pipe is a plain Python function. Lauren infers the calling convention from the number of parameters:

### One-argument (value only)

```python
from lauren.extractors import pipe

@pipe()
def uppercase(value: str) -> str:
    return value.upper()

@controller("/x")
class X:
    @get("/{name}")
    async def hello(self, name: Path[str] = pipe(uppercase)) -> dict:
        return {"name": name}
```

### Two-argument (value + context)

The second argument is a [`PipeContext`](#pipecontext) carrying the request, parameter name, DI container, and more:

```python
from lauren.extractors import pipe, PipeContext
from lauren.exceptions import NotFoundError

@pipe()
async def lookup_user(value: int, ctx: PipeContext):
    repo = await ctx.container.resolve(
        UserRepository,
        request_cache=ctx.request_cache,
    )
    user = await repo.get(value)
    if user is None:
        raise NotFoundError(f"user {value} not found")
    return user
```

Both sync and async functions work — Lauren awaits the result when it's a coroutine.

### Inline (no decorator)

When the function is already defined elsewhere, wrap it on the spot with `pipe(fn)`:

```python
from lauren.extractors import Path, pipe
from myapp.validators import validate_slug

async def get(
    self,
    slug: Path[str] = pipe(validate_slug),
): ...
```

## Writing a pipe class

Class-based pipes define a `transform` method. The `Pipe` base class is optional — it only documents the expected interface.

Lauren infers the calling convention from the method's parameter count (same as function pipes):

- `transform(self, value)` — for simple transforms that don't need the request context.
- `transform(self, value, ctx)` — to access the request, DI container, or parameter metadata.

```python
from lauren import injectable, Scope
from lauren.extractors import Pipe, pipe

@pipe()
@injectable(scope=Scope.SINGLETON)
class SlugNormalizer(Pipe):
    async def transform(self, value: str, ctx) -> str:
        return value.strip().lower().replace(" ", "-")
```

### With DI injection

When a pipe class is registered with `@injectable`, Lauren resolves it through the DI container, so it can receive services as constructor arguments — **exactly like a controller or guard**:

```python
from lauren import injectable, Scope
from lauren.extractors import Pipe, pipe

@pipe()
@injectable(scope=Scope.SINGLETON)
class UserLookup(Pipe):
    def __init__(self, repo: UserRepository) -> None:
        self.repo = repo

    async def transform(self, value: int, ctx) -> User:
        user = await self.repo.get(value)
        if user is None:
            raise NotFoundError(f"user {value} not found")
        return user
```

`UserRepository` must be provided by the controller's module for the DI resolution to succeed.

Both `transform(self, value, ctx)` and `transform(self, value)` are valid on injectable pipes — omit `ctx` when you only need the injected constructor dependencies.

### Without DI injection

If the class isn't registered with the DI container, Lauren instantiates it once (process-wide cache) and reuses that instance. This is fine for stateless pipes — and note that unlike injectable extractors, **Lauren never raises `StartupError` for pipes**; fallback instantiation is always attempted at request time:

```python
@pipe()
class TrimWhitespace(Pipe):
    def transform(self, value: str, ctx) -> str:
        return value.strip()
```

The `ctx` argument can be omitted for simple one-value transforms:

```python
@pipe()
class Uppercase(Pipe):
    def transform(self, value: str) -> str:  # no ctx needed
        return value.upper()
```

## Chaining pipes

Multiple pipes execute in **declaration order** — each receives the output of the previous:

```python
from typing import Annotated
from lauren.extractors import Path, PathField, pipe

@pipe()
def trim(value: str) -> str:
    return value.strip()

@pipe()
def lowercase(value: str) -> str:
    return value.lower()

@pipe()
async def lookup_article(value: str, ctx: PipeContext) -> Article:
    ...

@controller("/articles")
class ArticleController:
    @get("/{slug}")
    async def get(
        self,
        # trim → lowercase → lookup_article, in that order
        slug: Annotated[Path[str], pipe(trim), pipe(lowercase), pipe(lookup_article)],
    ) -> dict:
        ...
```

Equivalently with the default form:

```python
slug: Path[str] = pipe(trim) | pipe(lowercase) | pipe(lookup_article)
```

## `PipeContext`

The context object passed to two-argument pipes:

| Field | Type | Description |
|---|---|---|
| `ctx.request` | `Request` | The live request being processed. |
| `ctx.name` | `str` | Handler parameter name (e.g. `"id"`). |
| `ctx.source` | `str` | Where the value came from: `"path"`, `"query"`, `"json"`, etc. |
| `ctx.inner_type` | `Any` | Python type inside the extractor marker (e.g. `int` for `Path[int]`). |
| `ctx.container` | `DIContainer` | The DI container — resolve any service. |
| `ctx.request_cache` | `dict` | Per-request DI cache; pass to `container.resolve(...)`. |
| `ctx.owning_module` | `type \| None` | Module that declared the controller (for DI visibility). |
| `ctx.field_descriptor` | `FieldDescriptor \| None` | The `PathField` / `QueryField` attached to the parameter, if any. |

### Resolving a service from a pipe

```python
@pipe()
async def enrich(value: int, ctx: PipeContext) -> UserWithProfile:
    svc = await ctx.container.resolve(
        ProfileService,
        request_cache=ctx.request_cache,
        owning_module=ctx.owning_module,
    )
    return await svc.enrich(value)
```

Always pass `request_cache` to avoid creating a second instance of a request-scoped service.

## Error handling

Raise any `HTTPError` subclass from a pipe to short-circuit the request with the matching status:

```python
from lauren.exceptions import NotFoundError, UnprocessableEntityError

@pipe()
async def lookup(value: int, ctx: PipeContext) -> Article:
    article = await ctx.container.resolve(ArticleRepo, ...).get(value)
    if article is None:
        raise NotFoundError("article not found", detail={"id": value})
    return article

@pipe()
def validate_positive(value: int, ctx: PipeContext) -> int:
    if value <= 0:
        raise UnprocessableEntityError(
            f"{ctx.name} must be positive",
            detail={"field": ctx.name, "value": value},
        )
    return value
```

Any unhandled exception from a pipe is wrapped in `ExtractorError` and surfaces as a 422 Unprocessable Entity with the pipe's name in the detail.

## Field descriptors vs pipes

Field descriptors (`PathField`, `QueryField`, …) and pipes solve adjacent problems:

| | Field Descriptor | Pipe |
|---|---|---|
| **Purpose** | Constrain the raw extracted value | Transform / enrich the value |
| **Runs** | After scalar coercion | After field-descriptor validation |
| **Examples** | `ge=1`, `max_length=100`, `pattern=r"^\w+$"` | lookups, normalisation, enrichment |
| **Type change** | No — value stays `int`/`str`/etc. | Yes — pipe may return a completely different type |

Use descriptors for simple in/out range checks and length limits; use pipes when you need logic, async I/O, or a type change.

```python
# PathField validates 1 ≤ id; pipe(lookup) fetches the Article object.
async def get(
    self,
    id: Annotated[Path[int], PathField(ge=1), pipe(lookup)],
) -> dict: ...
```

## Patterns

### Normalise before validation

If you want to validate *after* normalisation, pipe first:

```python
@pipe()
def trim_and_lower(v: str) -> str:
    return v.strip().lower()

@pipe()
def validate_email(v: str, ctx: PipeContext) -> str:
    if "@" not in v:
        raise UnprocessableEntityError(
            "invalid email", detail={"field": ctx.name}
        )
    return v

async def subscribe(
    self,
    email: Query[str] = pipe(trim_and_lower) | pipe(validate_email),
) -> dict: ...
```

### Shared pipe library

Put reusable pipes in a module-level file so every controller can import them:

```python
# app/pipes.py
from lauren.extractors import Pipe, pipe, PipeContext
from lauren.exceptions import NotFoundError

@pipe()
class LookupUser(Pipe):
    def __init__(self, repo: UserRepository) -> None:
        self.repo = repo

    async def transform(self, value: int, ctx: PipeContext) -> User:
        u = await self.repo.get(value)
        if u is None:
            raise NotFoundError("user not found", detail={"id": value})
        return u

@pipe()
def slug(value: str) -> str:
    return value.strip().lower().replace(" ", "-")
```

```python
# app/controllers/users.py
from app.pipes import LookupUser, slug
from lauren.extractors import Path

class UserController:
    @get("/{id}")
    async def get(self, id: Path[int] = pipe(LookupUser)) -> dict:
        ...
```

### Source-aware pipe

A single pipe that behaves differently depending on where the value came from:

```python
@pipe()
def parse_date(value: str, ctx: PipeContext):
    fmt = "%Y-%m-%d" if ctx.source == "path" else "%d/%m/%Y"
    from datetime import datetime
    try:
        return datetime.strptime(value, fmt).date()
    except ValueError as e:
        raise UnprocessableEntityError(str(e), detail={"field": ctx.name})
```

### Optional parameter with a pipe

When the parameter is optional, the pipe only runs if a value was present:

```python
async def search(
    self,
    q: Query[str] | None = None,            # None → pipe never runs
    limit: Query[int] = PathField(ge=1, le=100) | pipe(clamp),
) -> dict: ...
```

## Testing pipes

The `TestClient` is the straightforward option — end-to-end:

```python
from lauren.testing import TestClient

def test_slug_normalisation():
    c = TestClient(app)
    r = c.get("/articles/Hello%20World")
    assert r.status_code == 200
    assert r.json()["slug"] == "hello-world"

def test_invalid_id():
    c = TestClient(app)
    r = c.get("/users/0")
    assert r.status_code == 422
```

For unit-testing a pipe function in isolation, call it directly:

```python
import pytest
from app.pipes import validate_email
from lauren.exceptions import UnprocessableEntityError

def test_validate_email_rejects_no_at():
    with pytest.raises(UnprocessableEntityError):
        validate_email("notanemail", ctx=None)
```

For class-based pipes with DI dependencies, resolve them from a test container or inject a mock:

```python
async def test_lookup_user_not_found():
    repo = MockUserRepository(returns=None)
    p = LookupUser(repo=repo)
    with pytest.raises(NotFoundError):
        await p.transform(999, ctx=None)
```

## Things to avoid

| Don't… | Because… |
|---|---|
| … do I/O in a one-arg pipe (no `ctx`) | Use a two-arg pipe so you can resolve services through the DI container. |
| … store mutable state on a pipe class | Class-based pipes may be shared across requests. Use `ctx.request_cache` or `ctx.request.state` for per-request state. |
| … return a `Response` from a pipe | Pipes produce *values* for the handler. Raise an `HTTPError` instead; the exception handler turns it into a response. |
| … resolve request-scoped services without `request_cache` | Each call creates a fresh instance, defeating the per-request cache. Always pass `request_cache=ctx.request_cache`. |
| … put business logic in a one-off lambda | Lambdas can't be marked with `@pipe()`. Wrap them in a named function or class so the pipe is discoverable and testable. |

## See also

* [Custom Extractors](custom-extractors.md) — for pulling domain values directly from the request without built-in extractors.
* [Core Concepts → Request & Response](../core-concepts/request-response.md) — the `Request` API available via `ctx.request`.
* [Custom Guards](custom-guards.md) — for allow/deny decisions; pipes are about *transforming* a value, not *gating* a request.
* [Custom Exception Handlers](custom-exception-handlers.md) — for shaping error responses raised inside pipes.
