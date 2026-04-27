# OpenAPI Security from Guards

> Annotate guard classes with `@openapi_security(...)` and Lauren will automatically populate the `security` field on every operation they protect — without any changes to your controller code.

## The problem

Lauren's `@use_guards` enforces authentication and authorization at request time.  But the generated OpenAPI document has no idea *which* security scheme a guard represents, so clients and API tooling see routes with no `security` entry even though they are fully protected:

```python
@use_guards(JwtGuard)          # ✔ enforced at runtime
@controller("/users")
class UserController: ...
# → GET /users/  has no "security" in openapi.json  ✘
```

## The solution — `@openapi_security`

Attach `@openapi_security({"SchemeName": [scopes...]})` to the guard class itself.  The decorator stores a small metadata object on the class; the OpenAPI generator picks it up automatically when it processes compiled handlers:

```python
from lauren import openapi_security, use_guards, controller, get, ExecutionContext

@openapi_security({"BearerAuth": []})          # ← add this
class JwtGuard:
    async def can_activate(self, ctx: ExecutionContext) -> bool:
        token = ctx.request.headers.get("Authorization", "")
        return token.startswith("Bearer ")

@use_guards(JwtGuard)
@controller("/users")
class UserController:
    @get("/")
    async def list(self) -> list[dict]:
        return []
```

Generated operation:

```json
{
  "get": {
    "operationId": "list",
    "security": [{"BearerAuth": []}],
    "responses": { "200": { "description": "Success" } }
  }
}
```

## Registering the security scheme

`@openapi_security` references a scheme by name.  You must still declare the scheme's definition in the OpenAPI components by passing `openapi_security_schemes` to `LaurenFactory.create`:

```python
app = await LaurenFactory.create(
    AppModule,
    openapi_security_schemes={
        "BearerAuth": {
            "type": "http",
            "scheme": "bearer",
            "bearerFormat": "JWT",
        },
    },
)
```

Common scheme definitions:

=== "Bearer (JWT)"

    ```python
    {
        "type": "http",
        "scheme": "bearer",
        "bearerFormat": "JWT",
    }
    ```

=== "API key (header)"

    ```python
    {
        "type": "apiKey",
        "in": "header",
        "name": "X-API-Key",
    }
    ```

=== "OAuth2 (authorization code)"

    ```python
    {
        "type": "oauth2",
        "flows": {
            "authorizationCode": {
                "authorizationUrl": "https://auth.example.com/oauth/authorize",
                "tokenUrl": "https://auth.example.com/oauth/token",
                "scopes": {
                    "read:items":  "Read items",
                    "write:items": "Create and update items",
                },
            }
        },
    }
    ```

=== "Basic auth"

    ```python
    {
        "type": "http",
        "scheme": "basic",
    }
    ```

## OR semantics — multiple schemes on one guard

Pass multiple requirement dicts to `@openapi_security` when *any* of the listed schemes is sufficient.  Each dict becomes a separate entry in the operation's `security` array — the OpenAPI "OR" model:

```python
@openapi_security(
    {"BearerAuth": []},   # JWT token, OR …
    {"ApiKey":    []},    # … a service API key
)
class FlexibleAuthGuard:
    async def can_activate(self, ctx: ExecutionContext) -> bool:
        auth  = ctx.request.headers.get("Authorization", "")
        apikey = ctx.request.headers.get("X-API-Key", "")
        return auth.startswith("Bearer ") or bool(apikey)
```

Generated:

```json
"security": [{"BearerAuth": []}, {"ApiKey": []}]
```

## AND semantics — multiple guards

When several guards are listed in `@use_guards`, the generator **merges** their security metadata into a single requirement object (the OpenAPI "AND" model: all schemes must be present simultaneously):

```python
@openapi_security({"BearerAuth": []})
class AuthGuard:
    async def can_activate(self, ctx: ExecutionContext) -> bool: ...

@openapi_security({"TenantHeader": []})
class TenantGuard:
    async def can_activate(self, ctx: ExecutionContext) -> bool: ...

@use_guards(AuthGuard, TenantGuard)
@controller("/tenant-api")
class TenantController: ...
```

Generated:

```json
"security": [{"BearerAuth": [], "TenantHeader": []}]
```

!!! tip "Guards without @openapi_security are ignored"
    If a guard class does not carry `@openapi_security`, it is silently skipped during schema generation.  Its `can_activate` still runs at request time as normal.

## OAuth2 scopes

Pass a non-empty scope list when using OAuth2:

```python
@openapi_security({"OAuth2": ["read:items", "write:items"]})
class OAuth2Guard:
    async def can_activate(self, ctx: ExecutionContext) -> bool:
        scopes = ctx.request.state.get("oauth_scopes", [])
        return "read:items" in scopes
```

Generated:

```json
"security": [{"OAuth2": ["read:items", "write:items"]}]
```

## Explicit override

If `@controller` already declares `security=[...]` directly, that value **always** takes precedence and guard-derived security is ignored for that controller:

```python
@openapi_security({"BearerAuth": []})
class JwtGuard: ...

@use_guards(JwtGuard)
@controller("/legacy", security=[{"BasicAuth": []}])   # ← wins
class LegacyController: ...
# → security: [{"BasicAuth": []}]
```

## Controller-level vs route-level guards

Guards attached at the **controller** level apply to every route on the controller.  Route-level guards apply only to their own route and are **combined** (AND) with any controller-level guards:

```python
@openapi_security({"BearerAuth": []})
class AuthGuard: ...

@openapi_security({"OtpCode": []})
class OtpGuard: ...

@use_guards(AuthGuard)          # applies to all routes
@controller("/sensitive")
class SensitiveController:
    @get("/list")               # security: [{"BearerAuth": []}]
    async def list(self) -> list[dict]: ...

    @get("/delete")
    @use_guards(OtpGuard)       # security: [{"BearerAuth": [], "OtpCode": []}]
    async def delete(self) -> dict: ...
```

## Precedence summary

| Source | Wins over |
|---|---|
| `@controller(security=[...])` | Everything — highest priority |
| Guard-derived (via `@openapi_security`) | Operations with no explicit security |
| No guard / no `@openapi_security` | Operation has no `security` field |

## Error handling

| Mistake | Exception | Message |
|---|---|---|
| `@openapi_security` bare (no parens) | `GuardConfigError` | *"must be used with parentheses"* |
| `@openapi_security()` empty | `GuardConfigError` | *"at least one security requirement dict"* |
| Non-dict argument | `GuardConfigError` | *"must be dicts mapping a scheme name"* |
| Applied to a function | `GuardConfigError` | *"must decorate a class"* |

## Full working example

```python
# guards.py
from lauren import openapi_security, ExecutionContext

@openapi_security({"BearerAuth": []})
class JwtGuard:
    async def can_activate(self, ctx: ExecutionContext) -> bool:
        auth = ctx.request.headers.get("Authorization", "")
        return auth.startswith("Bearer ")

@openapi_security({"ApiKey": []})
class ApiKeyGuard:
    async def can_activate(self, ctx: ExecutionContext) -> bool:
        return bool(ctx.request.headers.get("X-API-Key"))
```

```python
# items/controller.py
from lauren import controller, get, post, Json, use_guards
from .guards import JwtGuard, ApiKeyGuard
from pydantic import BaseModel

class Item(BaseModel):
    id: int
    name: str

class CreateItem(BaseModel):
    name: str

@use_guards(JwtGuard)
@controller("/items", tags=["items"])
class ItemsController:
    @get("/", response_model=list[Item])
    async def list_items(self) -> list[Item]:
        return [Item(id=1, name="widget")]

    @post("/", response_model=Item)
    @use_guards(ApiKeyGuard)        # AND: JWT + API key required
    async def create_item(self, body: Json[CreateItem]) -> Item:
        return Item(id=2, name=body.name)
```

```python
# main.py
import asyncio
from lauren import LaurenFactory, module
from items.controller import ItemsController

@module(controllers=[ItemsController])
class AppModule: ...

app = asyncio.run(
    LaurenFactory.create(
        AppModule,
        openapi_url="/openapi.json",
        docs_url="/docs",
        openapi_security_schemes={
            "BearerAuth": {
                "type": "http",
                "scheme": "bearer",
                "bearerFormat": "JWT",
            },
            "ApiKey": {
                "type": "apiKey",
                "in": "header",
                "name": "X-API-Key",
            },
        },
    )
)
```

The generated OpenAPI document will include:

```json
{
  "paths": {
    "/items/": {
      "get": {
        "tags": ["items"],
        "security": [{"BearerAuth": []}]
      },
      "post": {
        "tags": ["items"],
        "security": [{"BearerAuth": [], "ApiKey": []}]
      }
    }
  },
  "components": {
    "securitySchemes": {
      "BearerAuth": { "type": "http", "scheme": "bearer", "bearerFormat": "JWT" },
      "ApiKey":     { "type": "apiKey", "in": "header", "name": "X-API-Key" }
    }
  }
}
```
