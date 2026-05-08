# Authentication Guards — lauren-guards

## Install

```bash
pip install lauren-guards
```

All guards are `@injectable(scope=Scope.SINGLETON)` and implement `CanActivate`. Apply with `@use_guards(...)`.

## JWT Bearer

```python
from lauren import controller, get, use_guards
from lauren_guards import JwtBearerGuard

@use_guards(JwtBearerGuard)
@controller("/api/protected")
class ProtectedController:
    @get("/me")
    async def me(self, exec_ctx) -> dict:
        return {"user": exec_ctx.request.state.user}
```

Configure via environment:
```bash
JWT_SECRET=your-secret
JWT_ALGORITHM=HS256      # default
JWT_AUDIENCE=myapp       # optional
JWT_ISSUER=myapp.com     # optional
```

On success, the guard sets `request.state.user` (decoded JWT payload dict) and `request.state.user_id` (the `sub` claim).

## API Key

```python
from lauren_guards import ApiKeyGuard

@use_guards(ApiKeyGuard)
@controller("/api/internal")
class InternalController: ...
```

```bash
API_KEY=my-secret-api-key
API_KEY_HEADER=X-API-Key   # default
```

## Public-route bypass pattern

All guards are designed to be subclassed. The recommended pattern for mixed public/protected routes:

```python
from lauren_guards import JwtBearerGuard
from lauren import injectable, Scope, set_metadata

PUBLIC_ROUTE = "public_route"

def public():
    """Decorator that marks a route as publicly accessible."""
    return set_metadata(PUBLIC_ROUTE, True)

@injectable(scope=Scope.SINGLETON)
class OptionalAuthGuard(JwtBearerGuard):
    async def can_activate(self, ctx) -> bool:
        if ctx.get_handler_metadata(PUBLIC_ROUTE):
            return True           # skip auth for public routes
        return await super().can_activate(ctx)
```

```python
@use_guards(OptionalAuthGuard)
@controller("/api")
class MyController:
    @public()
    @get("/status")
    async def status(self) -> dict:
        return {"ok": True}

    @get("/profile")
    async def profile(self, exec_ctx) -> dict:
        return {"user": exec_ctx.request.state.user}
```

## Authorization guards

```python
from lauren_guards import require_roles, require_scopes

@use_guards(JwtBearerGuard, require_roles("admin"))
@delete("/{id}")
async def delete(self, id: int) -> None: ...

@use_guards(JwtBearerGuard, require_scopes("read:reports"))
@get("/reports")
async def reports(self) -> list: ...
```

`require_roles` and `require_scopes` read from `request.state.user` set by the auth guard upstream.

## Available guards

| Guard | Purpose |
|---|---|
| `JwtBearerGuard` | Verifies `Authorization: Bearer <jwt>` |
| `ApiKeyGuard` | Verifies `X-API-Key` header or `?api_key=` query |
| `BearerTokenGuard` | Verifies opaque bearer tokens via a lookup function |
| `BasicAuthGuard` | Verifies HTTP Basic credentials |
| `OAuth2IntrospectionGuard` | Introspects tokens via OAuth2 introspection endpoint |
| `SessionCookieGuard` | Verifies signed session cookies |
| `require_authenticated` | Requires any `user_id` in `request.state` |
| `require_roles(*roles)` | Requires all listed roles in JWT claims |
| `require_scopes(*scopes)` | Requires all listed OAuth scopes in JWT claims |
| `CsrfGuard` | Double-submit-cookie CSRF protection |
| `IpAllowlistGuard` | Blocks requests from non-allowlisted IPs |
