---
name: security-headers-cors
description: Adds HTTP security headers (CSP, HSTS, X-Frame-Options, etc.) and a configurable CORS policy to a Lauren application using custom middleware. Use when hardening a production API against clickjacking, MIME sniffing, XSS, and cross-origin abuse.
---

> Use `codemap find "SecurityHeadersMiddleware"` to locate any existing security middleware before adding new ones.

# Security Headers & CORS Policy Configuration

Two middleware classes cover the most common hardening requirements:

| Middleware | Purpose |
|---|---|
| `SecurityHeadersMiddleware` | Injects OWASP-recommended response headers on every reply. |
| `SimpleCorsMiddleware` | Validates the `Origin` header and injects `Access-Control-*` headers. |

## Default security headers

```python
DEFAULT_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "X-XSS-Protection": "1; mode=block",
    "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
    "Content-Security-Policy": "default-src 'self'; script-src 'self'; object-src 'none'",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Permissions-Policy": "geolocation=(), microphone=(), camera=()",
}
```

## SecurityHeadersMiddleware

```python
from __future__ import annotations

from lauren import middleware, injectable, Scope
from lauren.types import Request, Response

@middleware()
@injectable(scope=Scope.SINGLETON)
class SecurityHeadersMiddleware:
    """Appends security headers to every outgoing response."""

    def __init__(self, headers: dict | None = None) -> None:
        self._headers = headers or DEFAULT_SECURITY_HEADERS

    async def dispatch(self, request: Request, call_next) -> Response:
        response = await call_next(request)
        for key, value in self._headers.items():
            response = response.with_header(key, value)
        return response
```

## SimpleCorsMiddleware

```python
@middleware()
@injectable(scope=Scope.SINGLETON)
class SimpleCorsMiddleware:
    """Basic CORS policy: validates Origin, adds Access-Control-* headers."""

    def __init__(
        self,
        allow_origins: list[str] | None = None,
        allow_methods: list[str] | None = None,
        allow_headers: list[str] | None = None,
    ) -> None:
        self._allow_origins = set(allow_origins or ["*"])
        self._allow_methods = allow_methods or ["GET", "POST", "PUT", "DELETE", "OPTIONS"]
        self._allow_headers = allow_headers or ["Content-Type", "Authorization"]

    async def dispatch(self, request: Request, call_next) -> Response:
        origin = request.headers.get("origin", "")
        if "*" in self._allow_origins or origin in self._allow_origins:
            response = await call_next(request)
            cors_origin = origin if origin else "*"
            response = response.with_header("Access-Control-Allow-Origin", cors_origin)
            response = response.with_header(
                "Access-Control-Allow-Methods", ", ".join(self._allow_methods)
            )
            response = response.with_header(
                "Access-Control-Allow-Headers", ", ".join(self._allow_headers)
            )
            return response
        return await call_next(request)
```

## Module wiring

```python
from lauren import module, controller, get, LaurenFactory

@controller("/api")
class ApiController:
    @get("/data")
    async def data(self) -> dict:
        return {"hello": "world"}

@module(controllers=[ApiController], providers=[SecurityHeadersMiddleware, SimpleCorsMiddleware])
class AppModule:
    pass

app = LaurenFactory.create(
    AppModule,
    global_middlewares=[SecurityHeadersMiddleware, SimpleCorsMiddleware],
)
```

## Custom CSP per environment

Pass `headers=` to override defaults. Use `use_value` / `use_factory` to wire environment-specific configuration:

```python
from lauren._di.custom import use_factory

def make_security_headers() -> SecurityHeadersMiddleware:
    csp = os.environ.get("CSP_POLICY", "default-src 'self'")
    return SecurityHeadersMiddleware(headers={
        **DEFAULT_SECURITY_HEADERS,
        "Content-Security-Policy": csp,
    })
```

## Production CORS with specific origins

```python
CORS_ALLOW_ORIGINS = [
    "https://app.example.com",
    "https://admin.example.com",
]

app = LaurenFactory.create(
    AppModule,
    global_middlewares=[SecurityHeadersMiddleware, SimpleCorsMiddleware],
    # Pass specific origins via global_providers + use_factory or subclass SimpleCorsMiddleware
)
```

For full CORS support (preflight, credentials, max-age, vary), use the `CorsMiddleware` from the `lauren-middlewares` companion package.

## Testing

```python
def test_security_headers_present():
    client = build_client()
    r = client.get("/api/data")
    assert r.header("x-frame-options") == "DENY"
    assert r.header("x-content-type-options") == "nosniff"

def test_cors_allow_origin_added():
    client = build_client()
    r = client.get("/api/data", headers={"origin": "https://app.example.com"})
    assert r.header("access-control-allow-origin") == "https://app.example.com"
```
