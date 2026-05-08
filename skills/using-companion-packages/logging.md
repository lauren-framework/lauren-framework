# Structured Logging — lauren-logging

## Install

```bash
pip install lauren-logging
```

## Three presets (cover 99% of cases)

```python
from lauren_logging import LoggingModule, LoggingConfig

# Development: human-readable console output at DEBUG level
dev_module, _ = LoggingModule.for_root(LoggingConfig.for_development())

# Production: JSON structured logs at INFO level to a chosen backend
prod_module, _ = LoggingModule.for_root(
    LoggingConfig.for_production(backend="structlog")  # or "stdlib", "console", "file", "queue"
)

# Testing: in-memory backend you can assert against
test_module, backend = LoggingModule.for_root(*LoggingConfig.for_testing())
# backend.records → list of emitted log records
```

## Wire into the root module

```python
from lauren import module
from lauren_logging import LoggingModule, LoggingConfig

_logging_module, _ = LoggingModule.for_root(LoggingConfig.for_development())

@module(imports=[_logging_module, ...])
class AppModule: ...
```

## Inject Logger into services

```python
from lauren import injectable, Scope
from lauren_logging import Logger

@injectable(scope=Scope.SINGLETON)
class UserService:
    def __init__(self, log: Logger) -> None:
        self._log = log

    async def create_user(self, name: str) -> dict:
        self._log.info("Creating user", name=name)
        ...
```

`Logger` resolves to the singleton configured by `LoggingModule.for_root()`.

## Request logging middleware

```python
from lauren import Lauren
from lauren_logging import RequestLogMiddleware

app = Lauren(AppModule, global_middlewares=[RequestLogMiddleware()])
```

Emits one structured log record per request with method, path, status code, and duration.

## Context binding (request-id propagation)

```python
from lauren_logging import bind_context

@middleware()
@injectable(scope=Scope.SINGLETON)
class RequestContextMiddleware:
    async def dispatch(self, request, call_next):
        request_id = request.headers.get("x-request-id", str(uuid4()))
        with bind_context(request_id=request_id):
            return await call_next(request)
```

All log records emitted within the `bind_context` block carry the bound fields automatically.

## Testing assertions

```python
from lauren_logging import LoggingConfig

def test_user_service_logs(app):
    _, backend = LoggingConfig.for_testing()
    # ... call the service ...
    assert any(r.message == "Creating user" for r in backend.records)
    assert backend.records[0].fields["name"] == "alice"
```
