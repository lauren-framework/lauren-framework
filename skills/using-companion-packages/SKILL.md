---
name: using-companion-packages
description: Wires the Lauren companion packages into a production Lauren application. Covers CORS and other HTTP middleware from lauren-middlewares, JWT and API-key authentication guards from lauren-guards, and structured logging with request tracing from lauren-logging. Use when adding cross-cutting concerns (auth, CORS, logging) to a Lauren backend.
---

# Using Lauren Companion Packages

> Use `codemap find "SymbolName"` to locate any symbol before reading — it gives
> exact file + line range and is faster than grep.

The framework core stays small on purpose; cross-cutting concerns live in three companion packages:

| Package | Install | What it provides |
|---|---|---|
| `lauren-middlewares` | `pip install lauren-middlewares` | CORS, rate-limit, GZip, security headers, request-id, trusted hosts, HTTPS redirect, body-size limit, timeout |
| `lauren-guards` | `pip install lauren-guards` | `jwt_bearer`, `api_key`, `bearer_token`, `basic_auth`, `oauth2_introspection`, `require_roles`, `require_scopes`, `csrf`, `ip_allowlist` |
| `lauren-logging` | `pip install lauren-logging` | `LoggingModule`, `LoggingConfig` presets, processor pipeline, `InMemoryBackend` for tests |

## CORS

See [cors.md](cors.md) for config options, per-route overrides, and preflight handling.

## Authentication guards

See [auth.md](auth.md) for `jwt_bearer`, `api_key`, and the public-route bypass subclass pattern.

## Structured logging

See [logging.md](logging.md) for `LoggingConfig` presets, request-log middleware, and `Logger` injection.

## Wiring all three together

```python
# main.py
from lauren import Lauren
from app.app_module import AppModule

from lauren_middlewares import CorsMiddleware, RequestIdMiddleware
from lauren_logging import LoggingModule, LoggingConfig

logging_module, _ = LoggingModule.for_root(LoggingConfig.for_development())

app = Lauren(
    AppModule,
    global_middlewares=[
        CorsMiddleware(allow_origins=["https://myapp.com"]),
        RequestIdMiddleware(),          # adds X-Request-ID to every response
    ],
)
```

```python
# app/app_module.py
from lauren import module
from lauren_logging import LoggingModule, LoggingConfig

_logging_module, _ = LoggingModule.for_root(LoggingConfig.for_development())

@module(imports=[_logging_module, ...])
class AppModule: ...
```

Guards are applied per-controller or per-route; they don't go in `global_middlewares`.
