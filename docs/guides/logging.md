# Logging

> Lauren ships a complete, production-ready logger system in `lauren.logging`. It integrates seamlessly with the DI container, coexists with Python's `logging` module, and is swappable with zero framework changes.

---

## Quick orientation

There are **two separate logging paths** in Lauren, and understanding both is important:

| Path | What it controls | How to configure |
|---|---|---|
| **Framework logger** | Startup phases, route mapping, lifecycle hooks — events emitted by the framework itself | `LaurenFactory.create(..., logger=...)` |
| **Injectable logger** | Log calls inside your own services, controllers, and middleware | `global_providers=[...]` or a module provider |

Most production apps configure both. If you only configure the framework logger, your services cannot inject a `Logger`. If you only register an injectable logger, the framework startup output stays silent (`NullLogger` is the default).

---

## Built-in loggers

All four built-in loggers are `@injectable(scope=Scope.SINGLETON, provides=(Logger,))`, so passing any of them by class to `global_providers` is all you need to make them DI-available.

### `ConsoleLogger` — development

Human-readable, ANSI-coloured output. Colours auto-enable when stdout is a TTY; set `NO_COLOR=1` or `TERM=dumb` to disable them in CI.

```
[Lauren] 18:22:01.123  INFO  [RouterExplorer]  Mapped {GET /api/users}
[Lauren] 18:22:01.124  INFO  [Lifecycle]       Running @post_construct hooks
[Lauren] 18:22:01.891  WARN  [OrderService]    Retry 2/3 order=order-42
```

### `JsonLogger` — production

One JSON object per line. Every `extra` kwarg is merged into the top-level object so log aggregators (Loki, Datadog, CloudWatch, ELK) can index them directly.

```json
{"ts":"2026-04-24T18:22:01.123456+00:00","level":"info","logger":"Lauren","message":"Mapped route","pid":12345,"context":"RouterExplorer","method":"GET","path":"/api/users"}
```

### `NullLogger` — silence

Discards everything. Useful in tests when you do not want any log noise. Default `level` is `SILENT`.

### `InMemoryLogger` — testing

Accumulates `LogRecord` objects in a list. Exposes `.messages()`, `.contexts()`, and `.records` for assertions.

---

## Log levels

`LogLevel` values map to Python's `logging` numeric space so the two systems stay comparable:

| Lauren | Value | Python equivalent |
|---|---|---|
| `LogLevel.DEBUG` | 10 | `logging.DEBUG` |
| `LogLevel.VERBOSE` | 15 | *(between DEBUG and INFO)* |
| `LogLevel.INFO` | 20 | `logging.INFO` |
| `LogLevel.WARN` | 30 | `logging.WARNING` |
| `LogLevel.ERROR` | 40 | `logging.ERROR` |
| `LogLevel.SILENT` | 100 | *(above CRITICAL)* |

Pass a level as a `LogLevel` enum, an `int`, or a string (`"DEBUG"`, `"warn"`, `"WARNING"`):

```python
from lauren.logging import ConsoleLogger, LogLevel

# All three forms are equivalent
ConsoleLogger(level=LogLevel.DEBUG)
ConsoleLogger(level=10)
ConsoleLogger(level="debug")
```

`LogLevel.VERBOSE` has no stdlib counterpart — it is finer-grained than `DEBUG` and models NestJS's `verbose()` tier.

---

## `default_logger()` — env-var–driven factory

`default_logger()` reads `LAUREN_LOG_LEVEL` and `LAUREN_LOG_FORMAT` from the environment so you can change log output without touching code.

```python
from lauren.logging import default_logger

# format="auto": ConsoleLogger when stdout is a TTY, JsonLogger otherwise.
logger = default_logger()

# Explicit formats
logger = default_logger(format="json", level="warn")
logger = default_logger(format="console", level=LogLevel.DEBUG)
logger = default_logger(format="silent")
```

Environment variables:

| Variable | Accepted values | Default |
|---|---|---|
| `LAUREN_LOG_LEVEL` | `DEBUG`, `VERBOSE`, `INFO`, `WARN`, `ERROR`, `SILENT` | `INFO` |
| `LAUREN_LOG_FORMAT` | `auto`, `console`, `json`, `silent` | `auto` |

`format="auto"` resolves to `ConsoleLogger` when `stdout` is a TTY, and to `JsonLogger` otherwise — which means you get colour in a terminal and machine-parseable lines in a container without any explicit switch.

---

## Wiring the framework logger

Pass a logger to `LaurenFactory.create` with `logger=`. This controls what the framework itself emits during startup and runtime:

```python
from lauren import LaurenFactory
from lauren.logging import default_logger
from app.modules import AppModule

app = LaurenFactory.create(
    AppModule,
    logger=default_logger(),  # INFO+, format inferred from TTY
)
```

With `logger=` only, your services can **not** yet inject a `Logger` — that is a separate registration (see next section).

---

## Making the logger injectable

Register a logger class (or instance) as a `global_provider` so any service can declare `log: Logger` as a dependency:

=== "Class registration (singleton built by the container)"

    ```python
    from lauren import LaurenFactory
    from lauren.logging import JsonLogger
    from app.modules import AppModule

    app = LaurenFactory.create(
        AppModule,
        logger=JsonLogger(level="info"),           # framework events
        global_providers=[JsonLogger],             # injectable into services
    )
    ```

    When you pass the class, the DI container instantiates it as a singleton
    using its own `__init__` defaults. Both `logger=` and `global_providers`
    can point to the same class — they manage separate instances.

=== "Pre-built instance via `use_value`"

    ```python
    from lauren import LaurenFactory
    from lauren._di.custom import use_value
    from lauren.logging import JsonLogger, Logger
    from app.modules import AppModule

    shared_logger = JsonLogger(level="warn", name="MyApp")

    app = LaurenFactory.create(
        AppModule,
        logger=shared_logger,
        global_providers=[use_value(provide=Logger, value=shared_logger)],
    )
    ```

    `use_value` lets you share the exact same object with both the framework
    pipeline and your services, so every log line comes from one instance.

---

## Injecting a logger into your services

Services declare the `Logger` protocol (or a concrete type) as a constructor or field dependency:

=== "Field injection (dataclass-style)"

    ```python
    from lauren import injectable
    from lauren.logging import Logger

    @injectable()
    class OrderService:
        log: Logger  # injected by the DI container

        def create_order(self, order_id: str) -> None:
            self.log.info("Order created", context="OrderService", order_id=order_id)
    ```

=== "Constructor injection"

    ```python
    from lauren import injectable
    from lauren.logging import Logger

    @injectable()
    class PaymentService:
        def __init__(self, log: Logger) -> None:
            self.log = log

        def charge(self, amount: float) -> None:
            self.log.info("Charging", context="PaymentService", amount=amount)
    ```

The `context` keyword appears as the `[Label]` prefix in `ConsoleLogger` output and as the `"context"` field in `JsonLogger` output. Using the class name as the context value makes logs easy to correlate with code.

Any extra keywords you pass become structured fields in `JsonLogger`, or are rendered as `key=value` suffixes in `ConsoleLogger`:

```python
self.log.warn(
    "Retry scheduled",
    context="OrderService",
    attempt=2,
    order_id="order-42",
    delay_ms=500,
)
```

```json
{"level":"warn","message":"Retry scheduled","context":"OrderService","attempt":2,"order_id":"order-42","delay_ms":500,...}
```

---

## Log methods

| Method | Level |
|---|---|
| `log(msg, ...)` | INFO (NestJS-compatible alias) |
| `info(msg, ...)` | INFO (Python-idiomatic alias for `log`) |
| `debug(msg, ...)` | DEBUG |
| `verbose(msg, ...)` | VERBOSE |
| `warn(msg, ...)` / `warning(...)` | WARN |
| `error(msg, ...)` | ERROR |
| `log_record(record)` | Accepts a pre-built `LogRecord` |

All methods share the same signature:

```python
logger.info("message", *, context: str = "", **extra: Any) -> None
```

---

## Development setup

```python
from lauren import LaurenFactory
from lauren.logging import ConsoleLogger, LogLevel
from app.modules import AppModule

app = LaurenFactory.create(
    AppModule,
    logger=ConsoleLogger(level=LogLevel.DEBUG),
    global_providers=[ConsoleLogger],
)
```

With `level=LogLevel.DEBUG` you see all seven startup phases (`VERBOSE` level) plus the full framework boot trace. Colours auto-enable in a terminal.

---

## Production setup

```python
import os
from lauren import LaurenFactory
from lauren.logging import JsonLogger, LogLevel
from app.modules import AppModule

log_level = os.environ.get("LOG_LEVEL", "info")

app = LaurenFactory.create(
    AppModule,
    logger=JsonLogger(level=log_level, name="MyApp"),
    global_providers=[JsonLogger],
)
```

Or, using `default_logger()` so a container environment automatically selects JSON:

```python
from lauren.logging import default_logger

logger = default_logger(level=os.environ.get("LOG_LEVEL", "info"))

app = LaurenFactory.create(
    AppModule,
    logger=logger,
    global_providers=[JsonLogger],
)
```

`default_logger()` picks `ConsoleLogger` on a developer's TTY and `JsonLogger` in a container (non-TTY), which means the same startup command works in both contexts.

---

## Testing setup

Use `InMemoryLogger` so tests can make assertions on what was logged without parsing stdout:

```python
import pytest
from lauren import LaurenFactory
from lauren._di.custom import use_value
from lauren.logging import InMemoryLogger, LogLevel, Logger
from app.modules import AppModule


@pytest.fixture()
def mem_logger():
    return InMemoryLogger(level=LogLevel.DEBUG)


@pytest.fixture()
def app(mem_logger):
    return LaurenFactory.create(
        AppModule,
        global_providers=[use_value(provide=Logger, value=mem_logger)],
    )


def test_order_created_log(client, mem_logger):
    client.post("/orders", json={"item": "widget"})
    assert any("Order created" in m for m in mem_logger.messages())
    assert any("order_id" in r.extra for r in mem_logger.records)
```

`InMemoryLogger` exposes:

- `.records` — `list[LogRecord]`, full access to level, context, extra
- `.messages(level=None)` — extract message strings, optionally filtered by level
- `.contexts()` — extract non-empty context labels
- `.clear()` — reset between tests

---

## Interplay with Python's `stdlib logging` module

Lauren's logger system (`lauren.logging`) is **independent** of Python's `logging` module — they do not share handlers, propagation trees, or formatters.

However, both coexist in the same process:

- **Lauren framework internals** use `logging.getLogger("lauren")` (stdlib) in a small number of low-level exception paths — for instance, when an ASGI error cannot be surfaced any other way. These fire before Lauren's own startup is complete and go through the standard stdlib handler chain.
- **Everything else in Lauren** — startup phases, lifecycle hooks, request logging, your services — goes through the `Logger` you install.

Practical consequences:

1. If you configure a stdlib `logging` handler at the root or `"lauren"` logger, it will capture those early framework errors — but nothing from your services or Lauren's normal startup.
2. If you want a single log stream, configure both: a stdlib root handler for the `"lauren"` logger name, and `global_providers` for everything else. There is no built-in bridge, but wrapping `Logger` calls around `logging.getLogger` is straightforward.

### Bridging to stdlib

If your infrastructure relies on stdlib `logging` (e.g. a `RotatingFileHandler` or a `SentryHandler`):

```python
import logging
from lauren.logging import Logger, LogRecord, LogLevel, _BaseLogger
from lauren import injectable
from lauren.types import Scope


@injectable(scope=Scope.SINGLETON, provides=(Logger,))
class StdlibBridgeLogger(_BaseLogger):
    """Forwards every Lauren log record to stdlib logging."""

    def __init__(self, *, level: str | int | LogLevel = LogLevel.INFO) -> None:
        super().__init__(level=level)
        self._stdlib = logging.getLogger("lauren")

    def log_record(self, record: LogRecord) -> None:
        stdlib_level = int(record.level)  # values match (DEBUG=10, INFO=20, …)
        self._stdlib.log(stdlib_level, record.message, extra={"context": record.context, **record.extra})
```

Register it like any other logger:

```python
app = LaurenFactory.create(
    AppModule,
    logger=StdlibBridgeLogger(),
    global_providers=[StdlibBridgeLogger],
)
```

---

## Injecting concrete logger types directly

All three concrete types (`ConsoleLogger`, `JsonLogger`, `InMemoryLogger`) are registered in the container as both their own token and as `Logger`. If you register multiple concrete types via `global_providers`, services that inject `ConsoleLogger` directly get the console instance, services that inject `JsonLogger` get the JSON instance, and services that inject `Logger` get whichever one you bound to that token:

```python
from lauren._di.custom import use_value
from lauren.logging import ConsoleLogger, JsonLogger, InMemoryLogger, Logger

logger_console = ConsoleLogger(stream=cs)
logger_json = JsonLogger(stream=js)
logger_mem = InMemoryLogger()

app = LaurenFactory.create(
    AppModule,
    global_providers=[
        use_value(provide=ConsoleLogger, value=logger_console),
        use_value(provide=JsonLogger, value=logger_json),
        use_value(provide=Logger, value=logger_mem),   # "default" logger
    ],
)
```

```python
@injectable()
class AuditService:
    log: JsonLogger  # always the structured backend

@injectable()
class SearchService:
    log: Logger  # whatever the application default is
```

---

## Middleware and interceptors

Middleware classes can inject `Logger` just like services:

```python
from lauren import middleware
from lauren.logging import Logger
import time


@middleware()
class AccessLog:
    def __init__(self, log: Logger) -> None:
        self.log = log

    async def dispatch(self, request, call_next):
        t0 = time.monotonic()
        response = await call_next(request)
        self.log.info(
            f"{request.method} {request.path} → {response.status}",
            context="AccessLog",
            duration_ms=round((time.monotonic() - t0) * 1000, 1),
        )
        return response
```

Register it as global middleware so it wraps every request:

```python
app = LaurenFactory.create(
    AppModule,
    global_middlewares=[AccessLog],
    global_providers=[JsonLogger],
)
```

---

## `lauren-logging` companion package

For advanced needs — processor pipelines, `contextvars` binding, per-request trace IDs, pluggable backends — use the `lauren-logging` companion package. It sits on top of stock `lauren>=1.0` and requires no framework changes.

Three class-method presets cover the common cases:

```python
from lauren_logging import LoggingConfig, LoggingModule

# Development: ConsoleLogger at DEBUG
config = LoggingConfig.for_development()

# Production: your chosen backend at INFO
config = LoggingConfig.for_production(backend=MyBackend())

# Testing: InMemoryBackend returned alongside the config
config, in_memory = LoggingConfig.for_testing()
```

`LoggingModule.forRoot(config)` returns a Lauren module you add to your `AppModule` imports list — the same pattern as NestJS's `LoggerModule.forRoot(...)`.

---

## Errors at startup

| Error | Most likely cause |
|---|---|
| `MissingProviderError` | A service declares `log: Logger` but no logger was added to `global_providers` |
| `UnresolvableProviderError` | Logger was added to a specific module's `providers=` but not exported, so a sibling module cannot see it |

If you only pass `logger=` without `global_providers`, the DI container has no `Logger` registration and any service that requests one will fail at startup with `MissingProviderError`. The fix is always `global_providers=[ConsoleLogger]` (or `use_value(provide=Logger, value=...)`).

---

## See also

- [Custom Middleware](custom-middleware.md) — for per-request access logging patterns
- [Interceptors](interceptors.md) — for response timing and structured tracing
- [Custom Providers](custom-providers.md) — `use_value`, `use_factory`, `Token` for fine-grained logger wiring
- [Signals & Lifecycle Events](signals.md) — `RequestComplete` signal for post-request log aggregation
