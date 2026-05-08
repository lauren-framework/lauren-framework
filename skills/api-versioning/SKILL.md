---
name: api-versioning
description: Implements API versioning in a Lauren application. Use when shipping breaking changes alongside an existing API, or when different clients need different response shapes. Covers URL-prefix versioning, Accept-Version header versioning, and content-negotiation versioning.
---

> Use `codemap find "controller"` to list existing controllers before adding versioned ones.

# API Versioning

Lauren supports three versioning strategies. Choose based on your API contract requirements.

## Strategy 1 — URL prefix versioning (recommended)

The simplest and most cacheable approach. Each version is a separate controller registered under a different prefix.

```python
from __future__ import annotations
from lauren import controller, get, module

@controller("/api/v1/users")
class UsersV1Controller:
    @get("/")
    async def list(self) -> dict:
        return {"version": "v1", "users": [{"id": 1, "name": "Alice"}]}

    @get("/{user_id}")
    async def get_user(self, user_id: int) -> dict:
        return {"version": "v1", "id": user_id, "name": "Alice"}

@controller("/api/v2/users")
class UsersV2Controller:
    @get("/")
    async def list(self) -> dict:
        return {
            "version": "v2",
            "users": [{"id": 1, "name": "Alice", "email": "alice@example.com"}],
        }

    @get("/{user_id}")
    async def get_user(self, user_id: int) -> dict:
        return {"version": "v2", "id": user_id, "name": "Alice", "email": "alice@example.com"}

@module(controllers=[UsersV1Controller, UsersV2Controller])
class VersionedApiModule:
    pass
```

## Strategy 2 — Accept-Version header versioning

Route requests to different handlers based on a custom header. Use a guard or middleware to enforce the header.

```python
from lauren import controller, get, module
from lauren.types import Request

@controller("/api/users")
class UsersController:
    @get("/")
    async def list(self, request: Request) -> dict:
        version = request.headers.get("accept-version", "v1")
        if version == "v2":
            return {"version": "v2", "users": [{"id": 1, "name": "Alice", "email": "alice@example.com"}]}
        return {"version": "v1", "users": [{"id": 1, "name": "Alice"}]}

@module(controllers=[UsersController])
class HeaderVersionedModule:
    pass
```

## Strategy 3 — Content negotiation (Accept header)

Use MIME type versioning for REST purists.

```python
from lauren import controller, get, module
from lauren.types import Request, Response

@controller("/api/users")
class UsersController:
    @get("/")
    async def list(self, request: Request) -> Response:
        accept = request.headers.get("accept", "")
        if "application/vnd.myapp.v2+json" in accept:
            return Response.json(
                {"version": "v2", "users": [{"id": 1, "name": "Alice", "email": "alice@example.com"}]}
            )
        return Response.json({"version": "v1", "users": [{"id": 1, "name": "Alice"}]})

@module(controllers=[UsersController])
class ContentNegotiatedModule:
    pass
```

## Organising versions into sub-modules

For large APIs, put each version in its own module and import them into a root module:

```python
@module(controllers=[UsersV1Controller, OrdersV1Controller])
class V1Module:
    pass

@module(controllers=[UsersV2Controller, OrdersV2Controller])
class V2Module:
    pass

@module(imports=[V1Module, V2Module])
class ApiModule:
    pass
```

## Key points

- URL-prefix versioning is the most explicit and works best with CDN caching and OpenAPI docs (`/api/v1/openapi.json` vs `/api/v2/openapi.json`).
- Header versioning works well for internal APIs where you control clients.
- Content negotiation satisfies strict REST constraints but is harder to test and document.
- When deprecating a version, return a `Deprecation` or `Sunset` response header from a middleware.
