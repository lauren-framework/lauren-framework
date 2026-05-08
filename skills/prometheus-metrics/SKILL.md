---
name: prometheus-metrics
description: Instruments a Lauren app with Prometheus counters and histograms and exposes a /metrics scrape endpoint. Use when you need standard RED metrics (Rate, Errors, Duration) consumable by Prometheus and Grafana.
---

> Use `codemap find "SymbolName"` to locate any symbol before reading — it gives
> exact file + line range and is faster than grep across the whole repo.

# Prometheus Metrics Instrumentation

## Overview

`MetricsService` owns a dedicated `CollectorRegistry` (avoids polluting the
global default registry, which matters when running multiple apps in one
process). `MetricsController` exposes `/metrics` in the Prometheus text format.
A middleware records request count and latency automatically.

## MetricsService

```python
from prometheus_client import Counter, Histogram, generate_latest, CONTENT_TYPE_LATEST, CollectorRegistry
from lauren import injectable, Scope

@injectable(scope=Scope.SINGLETON)
class MetricsService:
    def __init__(self):
        self._registry = CollectorRegistry()
        self.request_count = Counter(
            "http_requests_total",
            "Total HTTP requests",
            ["method", "endpoint", "status"],
            registry=self._registry,
        )
        self.request_duration = Histogram(
            "http_request_duration_seconds",
            "HTTP request duration",
            ["method", "endpoint"],
            registry=self._registry,
        )

    def get_metrics(self) -> bytes:
        return generate_latest(self._registry)

    def record_request(self, method: str, endpoint: str, status: int, duration: float) -> None:
        self.request_count.labels(method=method, endpoint=endpoint, status=str(status)).inc()
        self.request_duration.labels(method=method, endpoint=endpoint).observe(duration)
```

## Controller

```python
from lauren import controller, get, module
from lauren.types import Response

@controller("/metrics")
class MetricsController:
    def __init__(self, metrics: MetricsService) -> None:
        self._metrics = metrics

    @get("/")
    async def expose(self) -> Response:
        data = self._metrics.get_metrics()
        return Response(content=data, media_type=CONTENT_TYPE_LATEST)

@module(controllers=[MetricsController], providers=[MetricsService])
class MetricsModule:
    pass
```

## Instrumentation middleware

```python
import time
from lauren import middleware, injectable, Scope
from lauren.types import Request, Response

@middleware()
@injectable(scope=Scope.SINGLETON)
class MetricsMiddleware:
    def __init__(self, metrics: MetricsService) -> None:
        self._metrics = metrics

    async def dispatch(self, request: Request, call_next) -> Response:
        start = time.monotonic()
        response = await call_next(request)
        duration = time.monotonic() - start
        self._metrics.record_request(
            method=request.method,
            endpoint=request.url.path,
            status=response.status_code,
            duration=duration,
        )
        return response
```

## Wiring

```python
app = LaurenFactory.create(
    AppModule,
    global_middlewares=[MetricsMiddleware],
)
```

## Notes

- Use a **separate** `CollectorRegistry` per app instance to avoid
  `ValueError: Duplicated timeseries` when tests create multiple apps.
- Install: `pip install prometheus-client`.
- In production, protect `/metrics` with IP allow-list middleware or a shared
  secret header to prevent metric leakage.
- For push-based workflows (Prometheus Pushgateway) use
  `prometheus_client.push_to_gateway` from a `BackgroundTasks` or scheduler job.
