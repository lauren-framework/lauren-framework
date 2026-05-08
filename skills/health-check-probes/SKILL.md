---
name: health-check-probes
description: Adds liveness, readiness, and full-status health-check endpoints to a Lauren application. Use when deploying to Kubernetes (kubelet probes), behind a load-balancer health check, or whenever you need structured dependency-status reporting.
---

> Use `codemap find "HealthService"` to check for an existing health module before adding one.

# Health Check & Readiness Probe Endpoints

Three endpoints on a single `HealthController`:

| Endpoint | Purpose | Typical Kubernetes probe |
|---|---|---|
| `GET /health/live` | Process is alive (always 200 if the server responds) | `livenessProbe` |
| `GET /health/ready` | Dependencies are reachable — returns 503 if not | `readinessProbe` |
| `GET /health/` | Full component-level JSON report | Monitoring dashboards |

## HealthService

```python
from __future__ import annotations

import asyncio
from typing import Callable
from lauren import injectable, Scope

@injectable(scope=Scope.SINGLETON)
class HealthService:
    """Runs named check functions and aggregates results."""

    def __init__(self) -> None:
        self._checks: dict[str, Callable] = {}
        self._ready: bool = True

    def register_check(self, name: str, check_fn: Callable) -> None:
        """Register a named check. check_fn() must return a truthy value on success."""
        self._checks[name] = check_fn

    def set_ready(self, ready: bool) -> None:
        """Override the readiness flag (useful in tests or during warm-up)."""
        self._ready = ready

    async def run_checks(self) -> dict:
        results: dict[str, dict] = {}
        for name, fn in self._checks.items():
            try:
                ok = await fn() if asyncio.iscoroutinefunction(fn) else fn()
                results[name] = {"status": "ok" if ok else "degraded"}
            except Exception as exc:
                results[name] = {"status": "error", "error": str(exc)}
        if results:
            overall = "ok" if all(r["status"] == "ok" for r in results.values()) else "degraded"
        else:
            overall = "ok"
        return {"status": overall, "checks": results}

    def is_ready(self) -> bool:
        return self._ready
```

## HealthController

```python
from __future__ import annotations

from lauren import controller, get, module
from lauren.types import Response

@controller("/health")
class HealthController:
    def __init__(self, health: HealthService) -> None:
        self._health = health

    @get("/")
    async def full_status(self) -> dict:
        """Return full component report — aggregates all registered checks."""
        return await self._health.run_checks()

    @get("/live")
    async def liveness(self) -> dict:
        """Liveness probe — always 200 while the process is alive."""
        return {"status": "ok"}

    @get("/ready")
    async def readiness(self) -> Response:
        """Readiness probe — 200 when ready, 503 when not."""
        if self._health.is_ready():
            return Response.json({"status": "ready"})
        return Response.json({"status": "not_ready"}).with_status(503)

@module(controllers=[HealthController], providers=[HealthService])
class HealthModule:
    pass
```

## Registering custom checks

Call `register_check` in a `@post_construct` hook on any service that depends on `HealthService`:

```python
from lauren import injectable, Scope, post_construct

@injectable(scope=Scope.SINGLETON)
class DatabaseService:
    def __init__(self, health: HealthService) -> None:
        self._health = health
        self._connected = False

    @post_construct
    async def connect(self) -> None:
        # ... open connection pool ...
        self._connected = True
        self._health.register_check("database", self._ping)

    async def _ping(self) -> bool:
        # Return True when the DB responds; False or raise on failure
        return self._connected
```

## Usage in a Kubernetes deployment

```yaml
livenessProbe:
  httpGet:
    path: /health/live
    port: 8000
  initialDelaySeconds: 5
  periodSeconds: 10

readinessProbe:
  httpGet:
    path: /health/ready
    port: 8000
  initialDelaySeconds: 10
  periodSeconds: 5
```

## Testing

```python
def test_liveness_always_ok():
    client = build_app(HealthModule)
    assert client.get("/health/live").status_code == 200

def test_readiness_503_when_not_ready():
    app = LaurenFactory.create(HealthModule)
    svc = app.container.resolve(HealthService)
    svc.set_ready(False)
    client = TestClient(app)
    assert client.get("/health/ready").status_code == 503
```
