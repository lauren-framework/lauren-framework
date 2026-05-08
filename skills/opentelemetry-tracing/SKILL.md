---
name: opentelemetry-tracing
description: Adds OpenTelemetry distributed tracing to a Lauren application using an in-process TracerProvider and per-request span middleware. Use when instrumenting a service for distributed tracing, exporting spans to Jaeger/OTLP, or testing trace propagation across handlers.
---

> Use `codemap find "TracingService"` to locate any existing tracing setup before adding a new one.

# OpenTelemetry Distributed Tracing

The pattern wires two components:

1. **`TracingService`** — singleton that owns the `TracerProvider` and `InMemorySpanExporter` (swap for `OTLPSpanExporter` in production).
2. **`TracingMiddleware`** — per-request middleware that opens a span, attaches route and method attributes, and closes it after the response.

## TracingService

```python
from __future__ import annotations

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from lauren import injectable, Scope

@injectable(scope=Scope.SINGLETON)
class TracingService:
    """Owns the OpenTelemetry TracerProvider and span exporter."""

    def __init__(self) -> None:
        self._exporter = InMemorySpanExporter()
        self._provider = TracerProvider()
        self._provider.add_span_processor(SimpleSpanProcessor(self._exporter))
        self._tracer = self._provider.get_tracer(__name__)

    def get_tracer(self) -> trace.Tracer:
        return self._tracer

    def get_finished_spans(self):
        return self._exporter.get_finished_spans()

    def clear_spans(self) -> None:
        self._exporter.clear()
```

> **Important:** Do *not* call `trace.set_tracer_provider()` inside `TracingService.__init__`
> when writing tests — it mutates global state and causes interference between test cases.
> Keep the provider local to the service instance and call `get_tracer()` / `get_finished_spans()`
> directly on the service. Only call `trace.set_tracer_provider()` once in application
> startup code (e.g., `main.py`), not in a DI-managed constructor.

## Tracing middleware

```python
from __future__ import annotations

from lauren import injectable, Scope, middleware
from lauren.types import Request, Response

@middleware()
@injectable(scope=Scope.SINGLETON)
class TracingMiddleware:
    """Opens a span for every HTTP request and records method + path."""

    def __init__(self, tracing: TracingService) -> None:
        self._tracing = tracing

    async def dispatch(self, request: Request, call_next) -> Response:
        tracer = self._tracing.get_tracer()
        span_name = f"{request.method} {request.path}"
        with tracer.start_as_current_span(span_name) as span:
            span.set_attribute("http.method", request.method)
            span.set_attribute("http.target", request.path)
            response = await call_next(request)
            span.set_attribute("http.status_code", response.status)
            return response
```

## Module wiring

```python
from lauren import module, controller, get

@controller("/api")
class ApiController:
    @get("/hello")
    async def hello(self) -> dict:
        return {"message": "hello"}

@module(controllers=[ApiController], providers=[TracingService, TracingMiddleware])
class AppModule:
    pass
```

Register the middleware globally at factory time:

```python
from lauren import LaurenFactory

app = LaurenFactory.create(
    AppModule,
    global_middlewares=[TracingMiddleware],
)
```

## Production exporter swap

Replace `InMemorySpanExporter` + `SimpleSpanProcessor` with the OTLP exporter:

```python
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.trace.export import BatchSpanProcessor

exporter = OTLPSpanExporter(endpoint="http://jaeger:4317", insecure=True)
provider.add_span_processor(BatchSpanProcessor(exporter))
```

## Testing

```python
def test_span_created_per_request():
    svc = TracingService()
    client = build_app_with_tracing(svc)
    client.get("/api/hello")
    spans = svc.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].name == "GET /api/hello"
```

Use a fresh `TracingService` instance per test (not from DI) to avoid cross-test span accumulation.
