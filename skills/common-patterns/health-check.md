# Health Check Module

Production liveness, readiness and startup probes. Drop in as-is — no
controller to write. Full skill: [health-check-probes](../health-check-probes/SKILL.md).

```python
# app/app_module.py
from lauren import module
from lauren.health import HealthModule

from app.database import Database

@module(imports=[HealthModule.for_root()], providers=[Database])
class AppModule: ...
```

```python
# app/database.py
from lauren import Scope, injectable, post_construct, pre_destruct
from lauren.health import health_check

@injectable(scope=Scope.SINGLETON)
class Database:
    @post_construct
    async def open(self) -> None:
        self._pool = await create_pool(DSN)

    @pre_destruct
    async def close(self) -> None:
        await self._pool.close()

    @health_check("database", timeout=2.0, cache_ttl=5.0)
    async def ping(self) -> bool:
        await self._pool.execute("SELECT 1")   # raising or returning False = down
        return True
```

* `GET /health/live` — 200 unless the app failed or stopped; never runs dependency checks.
* `GET /health/ready` — 503 before startup completes, while draining
  (`app.begin_draining()`), and while `database` is down.
* `GET /health/startup` — 200 once startup completed (Kubernetes `startupProbe`).
* `GET /health` — `{"status", "lifecycle", "checks": {"database": {"status": "up"}}}`;
  add `details="always"` for latency and error messages.

Don't hand-roll `{"status": "ok"}`: it answers "ok" before `@post_construct`
ran, during shutdown and while the database is down.
