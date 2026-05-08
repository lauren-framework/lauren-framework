# CORS — lauren-middlewares

## Install

```bash
pip install lauren-middlewares
```

## Basic setup

```python
from lauren import Lauren
from app.app_module import AppModule
from lauren_middlewares import CorsMiddleware

app = Lauren(
    AppModule,
    global_middlewares=[
        CorsMiddleware(
            allow_origins=["https://myapp.com"],
            allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
            allow_headers=["Authorization", "Content-Type"],
            allow_credentials=True,
            max_age=600,
        )
    ],
)
```

## Development (allow everything)

```python
CorsMiddleware(allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
```

## Other middlewares available

| Middleware | Purpose |
|---|---|
| `CorsMiddleware` | CORS preflight + response headers |
| `RequestIdMiddleware` | Adds `X-Request-ID` header |
| `SecurityHeadersMiddleware` | CSP, HSTS, X-Frame-Options |
| `TrustedHostMiddleware` | Blocks requests with invalid `Host` headers |
| `GZipMiddleware` | Compresses responses above a size threshold |
| `RateLimitMiddleware` | Token-bucket rate limiting per IP or user |
| `BodySizeLimitMiddleware` | Rejects oversized request bodies (413) |
| `TimeoutMiddleware` | Returns 504 if handler exceeds time limit |
| `HttpsRedirectMiddleware` | Redirects HTTP → HTTPS |
| `RequestLogMiddleware` | Structured request/response logging |

```python
from lauren_middlewares import (
    CorsMiddleware, RequestIdMiddleware, SecurityHeadersMiddleware,
    GZipMiddleware, RateLimitMiddleware,
)

app = Lauren(AppModule, global_middlewares=[
    CorsMiddleware(allow_origins=["*"]),
    RequestIdMiddleware(),
    SecurityHeadersMiddleware(),
    GZipMiddleware(minimum_size=1000),
    RateLimitMiddleware(rate=100, per=60),   # 100 requests per minute
])
```

Middlewares execute in the order listed — outermost first on request, innermost first on response.
