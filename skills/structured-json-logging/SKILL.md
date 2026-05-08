---
name: structured-json-logging
description: Adds structured JSON logging with per-request correlation IDs via a context-var middleware. Use when you need machine-parseable log lines that can be correlated across microservices or ingested by log aggregation platforms.
---

> Use `codemap find "SymbolName"` to locate any symbol before reading — it gives
> exact file + line range and is faster than grep across the whole repo.

# Structured JSON Logging with Correlation IDs

## Overview

A `ContextVar` holds the current request's correlation ID. `CorrelationIdMiddleware`
sets it from the incoming `X-Correlation-ID` header (or generates a UUID) and
echoes it back in the response. `StructuredLogger` reads the context var when
formatting each log record, producing JSON lines consumable by Datadog, Splunk,
or Elasticsearch.

## Context variable

```python
from contextvars import ContextVar

correlation_id: ContextVar[str] = ContextVar("correlation_id", default="")
```

## JSONFormatter

```python
import json
import logging

class JSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return json.dumps({
            "level": record.levelname,
            "message": record.getMessage(),
            "logger": record.name,
            "correlation_id": correlation_id.get(""),
        })
```

## StructuredLogger (injectable singleton)

```python
from lauren import injectable, Scope

@injectable(scope=Scope.SINGLETON)
class StructuredLogger:
    def __init__(self, name: str = "app"):
        self._logger = logging.getLogger(name)
        if not self._logger.handlers:
            handler = logging.StreamHandler()
            handler.setFormatter(JSONFormatter())
            self._logger.addHandler(handler)
            self._logger.setLevel(logging.DEBUG)

    def info(self, message: str, **kwargs) -> None:
        self._logger.info(message, extra=kwargs)

    def error(self, message: str, **kwargs) -> None:
        self._logger.error(message, extra=kwargs)

    def warning(self, message: str, **kwargs) -> None:
        self._logger.warning(message, extra=kwargs)
```

## Middleware

```python
import uuid
from lauren import middleware, injectable, Scope
from lauren.types import Request, Response

@middleware()
@injectable(scope=Scope.SINGLETON)
class CorrelationIdMiddleware:
    async def dispatch(self, request: Request, call_next) -> Response:
        cid = request.headers.get("x-correlation-id", str(uuid.uuid4()))
        correlation_id.set(cid)
        response = await call_next(request)
        response.headers["x-correlation-id"] = cid
        return response
```

## Controller

```python
from lauren import controller, get, module

@controller("/")
class AppController:
    def __init__(self, log: StructuredLogger) -> None:
        self._log = log

    @get("/hello")
    async def hello(self) -> dict:
        self._log.info("hello endpoint called")
        return {"message": "hello"}

@module(
    controllers=[AppController],
    providers=[StructuredLogger, CorrelationIdMiddleware],
)
class AppModule:
    pass
```

## Wiring with global middleware

```python
app = LaurenFactory.create(
    AppModule,
    global_middlewares=[CorrelationIdMiddleware],
)
```

## Notes

- `correlation_id` is a `ContextVar` — it is isolated per async task and
  thread, so concurrent requests never share state.
- For production use consider `structlog` (see `lauren-logging` companion
  package) which has richer processor pipelines and async-native support.
- Add extra fields to `JSONFormatter.format` (timestamp, service name,
  environment) to match your log schema.
