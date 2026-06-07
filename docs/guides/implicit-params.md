# Implicit Parameter Extraction

> Lauren can infer *where* a parameter comes from without an explicit extractor marker — saving you from wrapping every `int` in `Query[int]` and every model in `Json[Model]`.

## How it works

When a handler parameter has no extractor annotation (no `Path[…]`, `Query[…]`, `Json[…]`, etc.) and cannot be resolved through the DI container, Lauren applies these rules **in order**:

| Condition | Promoted to |
|-----------|-------------|
| Parameter name matches a `{segment}` in the URL template | `Path[T]` |
| Annotation is a `Pydantic BaseModel`, `msgspec.Struct`, `@dataclass`, `TypedDict`, or `Discriminated[A\|B, "key"]` (or `Optional[...]`) | `Json[T]` (request body) |
| Annotation is a scalar type (`int`, `str`, `float`, `bool`, `bytes`, `complex`) — or `Optional[scalar]` / `list[scalar]` / `tuple[scalar, ...]` | `Query[T]` (query string) |
| Anything else | `UnresolvableParameterError` at startup |

No runtime cost: promotion happens once at `LaurenFactory.create(...)` time, not on every request.

## Quick example

```python
from pydantic import BaseModel
from lauren import LaurenFactory, controller, get, post, put, delete, module
from typing import Optional


class CreateItem(BaseModel):
    name: str
    price: float


@controller("/items")
class ItemController:
    @get("/{item_id}")
    async def get_item(
        self,
        item_id: int,          # auto Path[int]  — name matches {item_id}
        format: str = "json",  # auto Query[str] — scalar with default
    ) -> dict:
        return {"item_id": item_id, "format": format}

    @post("/")
    async def create(
        self,
        warehouse: str,        # auto Query[str]
        item: CreateItem,      # auto Json[CreateItem]  — Pydantic model
    ) -> dict:
        return {"warehouse": warehouse, "name": item.name}, 201

    @put("/{item_id}")
    async def update(
        self,
        item_id: int,          # auto Path[int]
        item: CreateItem,      # auto Json[CreateItem]
        notify: bool = False,  # auto Query[bool] with default
    ) -> dict:
        return {"item_id": item_id, "notify": notify, "name": item.name}

    @delete("/{item_id}")
    async def delete(
        self,
        item_id: int,          # auto Path[int]
    ) -> dict:
        return {"deleted": item_id}
```

### Calling it

```console
$ curl "http://localhost:8000/items/42?format=yaml"
{"item_id": 42, "format": "yaml"}

$ curl -X POST "http://localhost:8000/items/?warehouse=EU" \
       -H "Content-Type: application/json" \
       -d '{"name": "widget", "price": 9.99}'
{"warehouse": "EU", "name": "widget"}
```

## Scalar query params in detail

These types auto-promote to query parameters:

```python
# Primitives
page: int                 # required — 422 if absent
name: str
ratio: float
active: bool              # "true"/"1"/"yes"/"on" → True
checksum: bytes           # UTF-8 encoded — "abc" → b"abc"
precision: complex        # standard complex literal — "1+2j" → (1+2j)

# Optional (absent → None)
q: Optional[str] = None
limit: Optional[int] = None

# Default values
page: int = 1
page_size: int = 20

# Multi-value (repeat the param: ?tags=a&tags=b)
tags: list[str]
ids: list[int]

# Tuple variant — also multi-value
scores: tuple[float, ...]
```

### Boolean coercion

Lauren accepts case-insensitive strings: `"true"`, `"1"`, `"yes"`, `"on"` → `True`; anything else → `False`.

```console
$ curl "http://localhost:8000/filter?active=True"   # active=True
$ curl "http://localhost:8000/filter?active=1"      # active=True
$ curl "http://localhost:8000/filter?active=false"  # active=False
```

## Structured body types

Any annotation that is a `pydantic.BaseModel`, `msgspec.Struct`, `@dataclass`, `TypedDict`, or `Discriminated[A | B, "key"]` (or `Optional[...]`) is auto-promoted to a JSON body parameter. Pydantic is not required — the framework validates the body using whichever library the type belongs to.

### Pydantic models

```python
class Address(BaseModel):
    city: str
    country: str

class CreateUser(BaseModel):
    name: str
    email: str
    address: Address        # nested models work

@controller("/users")
class UserController:
    @post("/")
    async def create(self, user: CreateUser) -> dict:
        return {"name": user.name, "city": user.address.city}, 201
```

```console
$ curl -X POST "http://localhost:8000/users/" \
       -H "Content-Type: application/json" \
       -d '{"name": "Alice", "email": "a@example.com", "address": {"city": "Paris", "country": "FR"}}'
{"name": "Alice", "city": "Paris"}
```

### Optional body

```python
from typing import Optional
from lauren import controller, patch

class PatchUser(BaseModel):
    name: str | None = None
    email: str | None = None

@controller("/users")
class UserController:
    @patch("/{user_id}")
    async def update(self, user_id: int, body: Optional[PatchUser] = None) -> dict:
        if body is None:
            return {"user_id": user_id, "changed": False}
        return {"user_id": user_id, "name": body.name}
```

When no body is sent and the default is `None`, the parameter resolves to `None`. When a body **is** required (no default), an empty request body returns `422`.

## Mixing sources

All three auto-detected sources can appear in one handler:

```python
class OrderBody(BaseModel):
    product_id: int
    quantity: int

@post("/{customer_id}/orders")
async def place_order(
    self,
    customer_id: int,       # Path   — name matches {customer_id}
    order: OrderBody,       # Body   — Pydantic model (no default, before defaults)
    priority: str = "low",  # Query  — scalar with default
    dry_run: bool = False,  # Query  — bool with default
) -> dict:
    return {
        "customer": customer_id,
        "priority": priority,
        "dry_run": dry_run,
        "product": order.product_id,
    }
```

```console
$ curl -X POST "http://localhost:8000/1/orders?priority=high&dry_run=true" \
       -H "Content-Type: application/json" \
       -d '{"product_id": 7, "quantity": 3}'
{"customer": 1, "priority": "high", "dry_run": true, "product": 7}
```

## Extracting a model from the query string

Use `Query[Model]` to collect multiple query-string fields into a single typed
object.  Lauren supports **Pydantic models**, **`msgspec.Struct`** subclasses,
and **Python dataclasses** — all work identically.  Fields are collected by
name and coerced to the declared types (`"5"` → `5` for `int`, etc.).

```python
from lauren import Query

# Pydantic model
class Filters(BaseModel):
    active: bool = True
    min_price: float = 0.0
    tags: list[str] = []

# msgspec.Struct (same ergonomics)
import msgspec

class PageParams(msgspec.Struct):
    page: int
    size: int = 20

# Python dataclass (same ergonomics)
from dataclasses import dataclass, field

@dataclass
class SearchParams:
    q: str
    limit: int = 10


@get("/")
async def list_items(self, f: Query[Filters]) -> dict:
    return {"active": f.active, "min_price": f.min_price}

@get("/users")
async def get_users(self, params: Query[PageParams]) -> dict:
    return {"page": params.page, "size": params.size}

@get("/search")
async def search(self, params: Query[SearchParams]) -> dict:
    return {"q": params.q, "limit": params.limit}
```

```console
$ curl "http://localhost:8000/?active=false&min_price=5.0"
{"active": false, "min_price": 5.0}

$ curl "http://localhost:8000/users?page=2&size=50"
{"page": 2, "size": 50}
```

!!! tip "Implicit body promotion"
    A **bare** parameter annotated as a `msgspec.Struct` or dataclass (without
    any extractor marker) is automatically promoted to a **JSON body**
    parameter, exactly like a bare Pydantic model:

    ```python
    @post("/users")
    async def create_user(self, body: PageParams) -> dict:
        # body is a PageParams instance populated from the request body
        return {"page": body.page, "size": body.size}
    ```

## Explicit markers still work

Implicit promotion does not remove explicit markers — you can always be explicit when the auto-detection would pick the wrong source:

```python
from lauren import Path, Query, Json, Header

@post("/{item_id}")
async def update(
    self,
    item_id: Path[int],       # explicit path — same as implicit here
    page: Query[int] = 1,     # explicit query
    body: Json[CreateItem],   # explicit body
    token: Header[str],       # explicit header — can't be implicit
) -> dict: ...
```

Explicit markers override implicit promotion on a per-parameter basis. Mixing explicit and implicit in the same handler is fine:

```python
@post("/{item_id}")
async def update(
    self,
    item_id: int,             # implicit path
    token: Header[str],       # explicit (no implicit equivalent)
    body: CreateItem,         # implicit body  (no default — before defaults)
    q: str = "",              # implicit query (default — after required params)
) -> dict: ...
```

## What does NOT auto-promote

| Type | Reason | Use instead |
|---|---|---|
| Unannotated parameter (`def h(self, x):`) | No type to coerce to | Annotate explicitly: `x: str` |
| `list[MyService]` | The element type is not a scalar — only `list[scalar]` (e.g. `list[int]`, `list[str]`) auto-promotes | Use `@injectable(multi=True)` + DI for multi-binding, or `Json[list[MyService]]` for a JSON array |
| Custom class (non-Pydantic, non-struct, non-dataclass, non-TypedDict, non-Discriminated) | Ambiguous: DI token or body? | `@injectable` + auto-DI, or `Json[MyClass]`, or a custom extractor |
| `Header[str]`, `Cookie[str]` | Can only come from headers/cookies | Always explicit |

## DI still runs first

Auto-promotion only activates when DI lookup fails. If a type is registered as a provider, it is still injected via DI regardless of whether it's a Pydantic model:

```python
@injectable()
class Settings(BaseModel):   # a Pydantic model that IS a DI provider
    debug: bool = False

@controller("/")
class C:
    @get("/")
    async def h(self, s: Settings) -> dict:  # DI wins — NOT promoted to body
        return {"debug": s.debug}

@module(controllers=[C], providers=[Settings])
class AppModule: ...
```

## Summary

| Pattern | Source |
|---|---|
| `id: int` where `{id}` in template | Path |
| `q: str` | Query |
| `page: int = 1` | Query with default |
| `flag: bool = False` | Query bool |
| `ids: list[int]` | Query multi-value |
| `item: CreateItem` (BaseModel) | JSON body |
| `item: Optional[CreateItem] = None` | JSON body, nullable |
| `body: MyStruct` (msgspec.Struct) | JSON body |
| `body: MyDC` (dataclass) | JSON body |
| `body: MyTD` (TypedDict) | JSON body |
| `body: Discriminated[A \| B, "key"]` | JSON body, discriminated union |
| `f: Query[Filters]` (Pydantic, explicit) | Query fields from model |
| `params: Query[PageParams]` (msgspec.Struct, explicit) | Query fields from struct |
| `params: Query[PageParams]` (dataclass, explicit) | Query fields from dataclass |
| `token: Header[str]` (explicit) | Header |
| `s: MyService` (DI registered) | DI injection |

## Discriminated unions

`Discriminated[A | B, "key"]` is a pydantic-free tagged-union type. Declare it
exactly like any other structured body — explicit or auto-promoted:

```python
from dataclasses import dataclass
from typing import Literal
from lauren import controller, post, Json, Discriminated


@dataclass
class Cat:
    kind: Literal["cat"] = "cat"
    name: str = ""


@dataclass
class Dog:
    kind: Literal["dog"] = "dog"
    name: str = ""


Animal = Discriminated[Cat | Dog, "kind"]


@controller("/animals")
class AnimalController:
    @post("/")
    async def create(self, body: Animal) -> dict:   # auto-promoted, no Json[…] needed
        return {"type": type(body).__name__, "name": body.name}

    @post("/explicit")
    async def create_explicit(self, body: Json[Animal]) -> dict:
        return {"type": type(body).__name__, "name": body.name}
```

The framework reads the `kind` field from the request JSON, dispatches to the
matching variant class, and validates the remaining fields. Missing or unknown
discriminator values return `422`. OpenAPI emits `oneOf` + `discriminator.mapping`
automatically.

Works with `@dataclass`, `TypedDict`, `msgspec.Struct`, and `pydantic.BaseModel`
variants. Pydantic does not need to be installed.
