---
name: cron-interval-jobs
description: Runs recurring jobs inside a Lauren app with @interval, @cron and @run_once plus ScheduleModule.for_root(). Use when you need periodic background work (cache warm-up, heartbeat, nightly cleanup) without an external scheduler.
---

> Use `codemap find "SymbolName"` to locate any symbol before reading — it gives
> exact file + line range and is faster than grep across the whole repo.

# Cron & Interval Jobs

Scheduling is built in (`lauren.scheduling`). Decorate methods of a singleton
`@injectable`, import `ScheduleModule.for_root()` once, and the framework
discovers, validates and runs the jobs — no `SchedulerService`, no
`asyncio.create_task` in `@post_construct`. Full capability skill:
[`building-lauren-scheduling`](../building-lauren-scheduling/).

## Jobs

```python
from datetime import timedelta

from lauren import Scope, injectable, module
from lauren.scheduling import JobContext, ScheduleModule, cron, interval, run_once


@injectable(scope=Scope.REQUEST)
class DbSession:
    async def aclose(self) -> None: ...          # torn down after every run


@injectable()
class CacheService:
    async def warm_up(self) -> None: ...


@injectable()
class Maintenance:
    def __init__(self, cache: CacheService) -> None:
        self._cache = cache

    @interval(seconds=60, initial_delay=5, jitter=5)           # every minute, first 5 s after startup
    async def cache_warmup(self) -> None:
        await self._cache.warm_up()

    @cron("0 3 * * *", tz="Europe/Paris", name="purge-sessions", timeout=300)
    async def purge(self, ctx: JobContext, db: DbSession) -> None:   # db: fresh per run
        cutoff = ctx.scheduled_at - timedelta(days=30)
        ctx.log.info("purging", before=cutoff.isoformat())

    @run_once(delay=10)                                        # once, 10 s after startup
    async def prime(self) -> None: ...


@module(imports=[ScheduleModule.for_root(timezone="UTC")], providers=[Maintenance, CacheService, DbSession])
class AppModule:
    pass
```

## Options that matter

| Option | Default | Use |
|---|---|---|
| `overlap` | `"skip"` | `"queue"` runs once after a slow run; `"allow"` runs concurrently (`max_instances`) |
| `timeout` | `None` | Cancel a run after N seconds (`JobFailed(error=TimeoutError)`) |
| `jitter` | `0` | Random 0..N s delay per fire to spread load |
| `misfire` / `misfire_grace` | `"coalesce"` / `min(60, period)` | Late wake-up: run once now, or `"skip"` to the next fire |
| `exclusive` | `False` | One runner per fire across workers (needs a shared `SchedulerLock`) |
| `mode` (`@interval`) | `"fixed_rate"` | `"fixed_delay"` waits `seconds` after each run completes |

## Status endpoint

```python
from lauren import controller, get, post
from lauren.scheduling import SchedulerRegistry


@controller("/scheduler")
class SchedulerController:
    def __init__(self, registry: SchedulerRegistry) -> None:
        self._registry = registry

    @get("/status")
    async def status(self) -> list:
        return [job.as_dict() for job in self._registry.jobs()]   # bounded stats, no history

    @post("/{name}/run")
    async def run_now(self, name: str) -> dict:
        return {"status": (await self._registry.trigger(name)).status}
```

Dynamic jobs: `registry.add_interval("heartbeat", fn, seconds=30)` /
`registry.add_cron(name, fn, "*/5 * * * *")` (they receive a `JobContext`
only), plus `pause`, `resume` and `remove`.

## Testing

```python fragment
clock = ManualClock()

@module(imports=[ScheduleModule.for_root(clock=clock)], providers=[Maintenance, CacheService, DbSession])
class TestModule: ...

app = LaurenFactory.create(TestModule)
await app.startup()
await clock.advance(minutes=5)      # fires everything due, no real waiting
await app.shutdown()
```

Or `ScheduleModule.for_root(enabled=False)` and `await registry.trigger("purge-sessions")`.

## Notes

- Scheduled methods without `ScheduleModule.for_root()` fail at startup
  (`LifecycleConfigError`); so does a scheduled method on a non-singleton
  provider. Bad cron expressions fail at decoration (`DecoratorUsageError`).
- Cron: five fields or six with leading seconds, names, `@daily`-style macros,
  IANA zones; DST gaps fire once after the gap, repeated hours once.
- Several workers run every job: use `exclusive=True` with a shared lock, or a
  dedicated scheduler process (`enabled=False` in the web workers).
