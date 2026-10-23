"""Health, readiness and liveness probes.

Import ``HealthModule.for_root(...)`` into the root module and mark provider
methods with ``@health_check``::

    from lauren import Scope, injectable, module
    from lauren.health import HealthModule, HealthResult, health_check

    @injectable(scope=Scope.SINGLETON)
    class Database:
        @health_check("database", timeout=2.0, cache_ttl=5.0)
        async def ping(self) -> bool:
            return await self.pool.ping()

    @module(imports=[HealthModule.for_root()], providers=[Database])
    class AppModule:
        pass

``GET /health/live`` restarts nothing unless the process itself is broken
(it never runs dependency checks by default), ``GET /health/ready`` fails
before startup completes, while draining (``app.begin_draining()``) and when
a critical ``"ready"`` check is down, ``GET /health/startup`` passes once
startup completed, and ``GET /health`` is the full report. Every check runs
under its timeout, concurrently, single-flighted and optionally cached.
"""

from __future__ import annotations

from ._health import HealthModule, HealthResult, HealthStatus, health_check

__all__ = ["HealthModule", "HealthResult", "HealthStatus", "health_check"]
