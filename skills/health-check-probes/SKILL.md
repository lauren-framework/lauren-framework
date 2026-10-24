---
name: health-check-probes
description: Adds liveness, readiness and startup probes plus a full health report to a Lauren application with HealthModule.for_root() and @health_check methods, and drains readiness before shutdown. Use when deploying to Kubernetes (kubelet probes), behind a load-balancer health check, for uptime monitors, or whenever you need structured dependency-status reporting.
---

> Use `codemap find "health_check"` to see which providers already declare checks before adding one.

# Health, Readiness & Liveness Probes

Import one module; mark dependency pings with `@health_check`. No controller,
no service, no registration calls.

```python
from __future__ import annotations

from lauren import Scope, injectable, module
from lauren.health import HealthModule, HealthResult, health_check


@injectable(scope=Scope.SINGLETON)            # checks must live on singletons
class Database:
    @health_check("database", timeout=2.0, cache_ttl=5.0)   # feeds /ready (tags=("ready",))
    async def ping(self) -> bool:
        await self._pool.execute("SELECT 1")    # raising = down
        return True


@injectable(scope=Scope.SINGLETON)
class SearchClient:
    @health_check("search", critical=False, timeout=1.0)    # degrades, never fails readiness
    async def ping(self) -> HealthResult:
        took = await self._client.ping()
        if took > 0.5:
            return HealthResult.degraded(latency_ms=took * 1000, reason="slow")
        return HealthResult.up(latency_ms=took * 1000)


@module(imports=[HealthModule.for_root()], providers=[Database, SearchClient])
class AppModule:
    pass
```

| Endpoint | 200 when | Probe |
|---|---|---|
| `GET /health/live` | the process is not `FAILED` / `STOPPED` (no dependency checks!) | `livenessProbe` |
| `GET /health/ready` | the app is `READY` and no critical `"ready"` check is down | `readinessProbe` |
| `GET /health/startup` | startup completed | `startupProbe` |
| `GET /health` | aggregate up / degraded (503 when down) | monitors |

Probes return `{"status": ...}` only. The report shows check names and
statuses; details (latency, reasons, error messages) only with
`details="always"` or `details="when_authorized", details_guard=AdminGuard`
(a denial hides details, never a 403).

## Rules

- **Liveness never checks dependencies.** A database outage must remove pods
  from rotation, not restart them. Use `tags=("live",)` only for "this process
  is broken" signals.
- Return `None` / `True` (up), `False` (down) or a `HealthResult`; raising is down.
- Every check runs under its `timeout`, concurrently, single-flighted; set
  `cache_ttl` to protect the dependency from probe storms.
- The provider must be `Scope.SINGLETON` (`LifecycleConfigError` otherwise);
  names are unique (`DuplicateBindingError`); parameters other than resolvable
  dependencies fail startup (`UnresolvableParameterError`).
- Global auth guards: the probe routes carry `PUBLIC_METADATA_KEY`; honour it
  with `if ctx.get_metadata(PUBLIC_METADATA_KEY, False): return True`.

## Options

```python
HealthModule.for_root(
    path="/health",
    details="never",                 # "always" | "when_authorized" (+ details_guard=)
    format="lauren",                 # "ietf" → application/health+json
    event_loop_lag_ms=500,           # built-in event_loop check (live + ready)
    disk_paths={"/": 1 << 30},       # disk:/ check — minimum free bytes
    max_rss_bytes=2 << 30,           # memory check (POSIX)
    version="1.4.2",                 # shown in the report
    report_status_code=True,         # report answers 503 when down
    degraded_status=200,             # degraded keeps the instance in rotation
)
```

## Draining before shutdown

Uvicorn stops listening *before* it calls `app.shutdown()`, so readiness must
fail earlier. `app.lifecycle_state` goes `READY → DRAINING` on
`app.begin_draining()`; requests are still served, `/health/ready` answers 503.

```python
from lauren.signals import install_signal_handlers

install_signal_handlers(app, shutdown_delay=10)   # SIGTERM: drain, serve 10 s, then shutdown()
```

## Kubernetes

```yaml
startupProbe:
  httpGet: {path: /health/startup, port: 8000}
  periodSeconds: 2
  failureThreshold: 30
livenessProbe:
  httpGet: {path: /health/live, port: 8000}
  periodSeconds: 10
readinessProbe:
  httpGet: {path: /health/ready, port: 8000}
  periodSeconds: 5
lifecycle:
  preStop:
    exec: {command: ["sleep", "5"]}
```

## Testing

```python
from lauren import LaurenFactory, Scope, injectable, module
from lauren.health import HealthModule, health_check
from lauren.testing import TestClient


@injectable(scope=Scope.SINGLETON)
class DownDatabase:
    @health_check("database")
    async def ping(self) -> bool:
        return False


def test_a_database_outage_fails_readiness_not_liveness():
    @module(imports=[HealthModule.for_root(details="always")], providers=[DownDatabase])
    class TestModule:
        pass

    app = LaurenFactory.create(TestModule)
    client = TestClient(app)                      # starts the app: READY

    assert client.get("/health/ready").status_code == 503
    assert client.get("/health/live").status_code == 200
    assert client.get("/health").json()["checks"]["database"]["status"] == "down"


def test_draining_fails_readiness():
    @module(imports=[HealthModule.for_root()])
    class TestModule:
        pass

    app = LaurenFactory.create(TestModule)
    client = TestClient(app)
    app.begin_draining()

    assert client.get("/health/ready").status_code == 503
```

## Appendix: a manual probe without the module

If you cannot import `HealthModule`, follow the lifecycle state — never answer
a constant `{"status": "ok"}`:

```python
from lauren import AppLifecycleState, LaurenFactory, LifecycleStateChanged, controller, get, use_value
from lauren.types import Response


class Readiness:
    state = AppLifecycleState.CREATED


@controller("/status")
class StatusController:
    def __init__(self, readiness: Readiness) -> None:
        self.readiness = readiness

    @get("/ready")
    async def ready(self) -> Response:
        ok = self.readiness.state is AppLifecycleState.READY
        return Response.json({"status": "up" if ok else "down"}, status=200 if ok else 503)


readiness = Readiness()
app = LaurenFactory.create(AppModule, global_providers=[use_value(provide=Readiness, value=readiness)])
app.signals.on(LifecycleStateChanged)(lambda event: setattr(readiness, "state", event.current))
```

See `docs/guides/health-checks.md` for the full semantics.
