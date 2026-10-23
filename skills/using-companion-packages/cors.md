# CORS — lauren-middlewares

## Install

```bash
pip install lauren-middlewares
```

## Basic setup

```python
from lauren import LaurenFactory
from app.app_module import AppModule
from lauren_middlewares import cors

# cors(...) returns a configured middleware *class* — pass the class itself.
app = LaurenFactory.create(
    AppModule,
    global_middlewares=[
        cors(
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
cors(allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
```

## Other middlewares available

Each middleware ships as a factory returning a configured class, plus a
default-configured class of the same name (`CorsMiddleware = cors()`):

| Factory (default class) | Purpose |
|---|---|
| `cors(...)` (`CorsMiddleware`) | CORS preflight + response headers |
| `request_id(...)` (`RequestIdMiddleware`) | Adds `X-Request-ID` header |
| `security_headers(...)` (`SecurityHeadersMiddleware`) | CSP, HSTS, X-Frame-Options |
| `trusted_hosts(...)` (`TrustedHostsMiddleware`) | Blocks requests with invalid `Host` headers |
| `gzip(...)` (`GZipMiddleware`) | Compresses responses above a size threshold |
| `rate_limit(...)` (`RateLimitMiddleware`) | Token-bucket rate limiting per IP or user |
| `body_size_limit(...)` (`BodySizeLimitMiddleware`) | Rejects oversized request bodies (413) |
| `timeout(...)` (`TimeoutMiddleware`) | Returns 504 if handler exceeds time limit |
| `https_redirect(...)` (`HttpsRedirectMiddleware`) | Redirects HTTP → HTTPS |
| `request_log(...)` (`RequestLogMiddleware`) | Structured request/response logging |

```python
from lauren import LaurenFactory
from lauren_middlewares import cors, gzip, rate_limit, request_id, security_headers

# Each factory returns a configured middleware *class* (pass classes, not instances).
app = LaurenFactory.create(AppModule, global_middlewares=[
    cors(allow_origins=["*"]),
    request_id(),
    security_headers(),
    gzip(minimum_size=1000),
    rate_limit(per_minute=100),              # 100 requests per minute
])
```

Middlewares execute in the order listed — outermost first on request, innermost first on response.
