# Health Check Module

Minimal production health + readiness endpoints. Drop in as-is.

```python
# app/health/health_controller.py
from lauren import controller, get

@controller("/health")
class HealthController:
    @get("/")
    async def live(self) -> dict:
        return {"status": "ok"}

    @get("/ready")
    async def ready(self) -> dict:
        # Add real readiness checks here (DB ping, etc.)
        return {"status": "ready"}
```

```python
# app/health/health_module.py
from lauren import module
from app.health.health_controller import HealthController

@module(controllers=[HealthController])
class HealthModule: ...
```

```python
# app/app_module.py
from lauren import module
from app.health.health_module import HealthModule

@module(imports=[HealthModule, ...])
class AppModule: ...
```

## With database ping

```python
from lauren import controller, get, injectable, Scope

@injectable(scope=Scope.SINGLETON)
class Database:
    async def ping(self) -> bool:
        try:
            await self._pool.execute("SELECT 1")
            return True
        except Exception:
            return False

@controller("/health")
class HealthController:
    def __init__(self, db: Database) -> None:
        self._db = db

    @get("/")
    async def live(self) -> dict:
        return {"status": "ok"}

    @get("/ready")
    async def ready(self) -> dict:
        db_ok = await self._db.ping()
        if not db_ok:
            return {"status": "degraded", "db": "unreachable"}, 503
        return {"status": "ready", "db": "ok"}
```

Returning a `(body, status_code)` tuple sets the HTTP status without importing `Response`.
