---
name: cron-interval-jobs
description: Registers and runs recurring interval jobs inside a Lauren app. Use when you need periodic background work (cache warm-up, heartbeat, cleanup) without an external scheduler.
---

> Use `codemap find "SymbolName"` to locate any symbol before reading — it gives
> exact file + line range and is faster than grep across the whole repo.

# Cron & Interval Job Registration

## Overview

`SchedulerService` holds a registry of `(callable, interval_seconds)` pairs.
`@post_construct` starts an `asyncio.Task` per job that loops forever with
`asyncio.sleep`. `@pre_destruct` cancels all tasks cleanly when the app shuts
down. Register jobs in the module factory or from other services that inject
`SchedulerService`.

## SchedulerService

```python
import asyncio
from typing import Callable
from lauren import injectable, Scope, post_construct, pre_destruct

@injectable(scope=Scope.SINGLETON)
class SchedulerService:
    def __init__(self):
        self._tasks: list[asyncio.Task] = []
        self._jobs: list[tuple[Callable, float, str]] = []
        self.executed_jobs: list[str] = []  # for testing/observability

    def register(self, func: Callable, interval_seconds: float, name: str = "") -> None:
        """Register a coroutine function to run every interval_seconds."""
        self._jobs.append((func, interval_seconds, name or func.__name__))

    @post_construct
    async def start(self) -> None:
        for func, interval, name in self._jobs:
            task = asyncio.create_task(self._run_interval(func, interval, name))
            self._tasks.append(task)

    @pre_destruct
    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

    async def _run_interval(self, func: Callable, interval: float, name: str) -> None:
        while True:
            try:
                await func()
                self.executed_jobs.append(name)
            except asyncio.CancelledError:
                raise
            except Exception:
                pass  # log in production
            await asyncio.sleep(interval)
```

## Registering jobs

Register from a module-level `@post_construct` on any injectable:

```python
@injectable(scope=Scope.SINGLETON)
class CacheWarmupJob:
    def __init__(self, cache: CacheService, scheduler: SchedulerService) -> None:
        self._cache = cache
        scheduler.register(self.run, interval_seconds=60.0, name="cache-warmup")

    async def run(self) -> None:
        await self._cache.warm_up()
```

Or register programmatically before `LaurenFactory.create`:

```python
scheduler = SchedulerService()
scheduler.register(my_async_func, 30.0, "heartbeat")
app = LaurenFactory.create(AppModule, global_providers=[use_value(provide=SchedulerService, value=scheduler)])
```

## Module wiring

```python
@module(providers=[SchedulerService, CacheWarmupJob])
class SchedulerModule:
    pass
```

## Status endpoint

```python
@controller("/scheduler")
class SchedulerController:
    def __init__(self, scheduler: SchedulerService) -> None:
        self._scheduler = scheduler

    @get("/status")
    async def status(self) -> dict:
        return {
            "running_tasks": len(self._scheduler._tasks),
            "executed_jobs": len(self._scheduler.executed_jobs),
        }
```

## Notes

- `asyncio.CancelledError` must be re-raised in the loop body so `stop()` can
  cancel tasks cleanly.
- For cron-style scheduling (specific times rather than fixed intervals)
  use APScheduler (`pip install apscheduler`) and register it as a singleton
  that integrates with `@post_construct` / `@pre_destruct`.
- In multi-worker deployments, elect a leader (e.g. via Redis `SET NX`) to
  avoid duplicate job execution.
