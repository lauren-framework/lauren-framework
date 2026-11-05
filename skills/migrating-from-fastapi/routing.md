# Routing — FastAPI vs Lauren

## Path parameters

**FastAPI:**
```python
@router.get("/{user_id}/posts/{post_id}")
async def get_post(user_id: int, post_id: int): ...
```

**Lauren:**
```python
@get("/{user_id}/posts/{post_id}")
async def get_post(self, user_id: int, post_id: int) -> dict: ...
```

Both infer the type from the annotation. Lauren also accepts the explicit form:
```python
from lauren import Path
async def get_post(self, user_id: Path[int], post_id: Path[int]) -> dict: ...
```

## Query parameters

**FastAPI:**
```python
@router.get("/items")
async def list(skip: int = 0, limit: int = 10, q: str | None = None): ...
```

**Lauren:**
```python
from lauren import Query
@get("/items")
async def list(self, skip: int = 0, limit: int = 10, q: Query[str | None] = None): ...
```

`Query[T]` makes intent explicit; plain annotated params are also inferred as query strings when no path segment matches.

## Request body

**FastAPI:**
```python
from pydantic import BaseModel

class CreateItem(BaseModel):
    name: str
    price: float

@router.post("/items")
async def create(item: CreateItem): ...
```

**Lauren:**
```python
from lauren import Json

@post("/items", status_code=201)
async def create(self, item: Json[CreateItem]) -> dict:
    return item.model_dump()
```

`Json[T]` deserializes and validates the body. `status_code=` declares the success status (sent and documented in OpenAPI).

## Response models

**FastAPI:**
```python
@router.get("/users/{id}", response_model=UserOut)
async def get_user(id: int) -> UserOut: ...
```

**Lauren:**
```python
@get("/{id}")
async def get_user(self, id: int) -> UserOut: ...  # return type → OpenAPI schema
```

The return type annotation drives OpenAPI generation (`response_model=` also works and wins). Unlike FastAPI, filtering the output through it is opt-in in 1.x: pass `response_validation="filter"` to `LaurenFactory.create` (or the route) so a `UserInDB` returned from a `-> UserOut` route sends only `UserOut`'s fields; `"strict"` also validates, like FastAPI.

## Status codes and headers

**FastAPI:**
```python
from fastapi import Response

@router.post("/", status_code=201)
async def create(response: Response) -> dict:
    response.headers["X-Id"] = "123"
    return {"created": True}
```

**Lauren:**
```python
from lauren import Created

@post("/")
async def create(self) -> Created[dict]:
    return Created({"created": True}, headers={"X-Id": "123"}, location="/items/123")
```

`status_code=201` on the decorator works too (`-> dict`, `return {...}`). The tuple
forms `(body, status)` / `(body, status, headers)` still work but are invisible to
OpenAPI. For full control return a `Response`.
