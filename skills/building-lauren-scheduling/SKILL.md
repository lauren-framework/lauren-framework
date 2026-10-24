---
name: building-lauren-scheduling
description: Adds framework-owned background work to a Lauren app. Covers @hosted_service long-running loops (ServiceContext, restart policy, backoff, wait_ready, stop order before @pre_destruct), @interval / @cron / @run_once scheduled methods on singletons with ScheduleModule.for_root(), overlap / timeout / jitter / misfire options, per-run DI scopes and JobContext, cron syntax and DST, SchedulerRegistry (status, trigger, dynamic jobs), exclusive jobs with SchedulerLock across workers, lifecycle signals, and testing with ManualClock, enabled=False, scheduler=False and hosted_services=False. Use instead of asyncio.create_task in @post_construct, APScheduler, or a hand-rolled SchedulerService.
---

> Use `codemap find "SymbolName"` to locate any symbol before reading — it gives
> exact file + line range and is faster than grep across the whole repo.

# Lauren Hosted Services & Scheduling

Everything lives in `lauren.scheduling` (key names are also exported from
`lauren`). It is discovered and validated inside `LaurenFactory.create`; the
guide is `docs/guides/scheduling.md`.

## Pick the tool

| Need | Use |
|---|---|
| A loop for the app's whole lifetime (queue consumer, outbox relay, heartbeat) | `@hosted_service()` |
| Code on a fixed period | `@interval(seconds=...)` |
| Code at wall-clock times | `@cron("0 3 * * *", tz=...)` |
| Code once after startup | `@run_once(delay=...)` |
| Work after one HTTP response | `BackgroundTasks` (not this skill) |
| Durable, retried jobs | an external queue — out of scope |

## Hosted service

```python
from lauren import Scope, injectable, module
from lauren.scheduling import ServiceContext, hosted_service


@injectable(scope=Scope.REQUEST)
class UnitOfWork:
    async def aclose(self) -> None: ...


@injectable()
class Broker:
    async def connect(self) -> None: ...
    async def poll(self) -> list[dict]: return []


@hosted_service(restart="on-failure", max_restarts=5, backoff=(1.0, 30.0), wait_ready=True, stop_timeout=10.0)
class OrderConsumer:
    def __init__(self, broker: Broker) -> None:        # constructor DI as usual
        self._broker = broker

    async def run(self, ctx: ServiceContext) -> None:
        await self._broker.connect()
        ctx.mark_ready()                                 # startup waits for this (wait_ready=True)
        while not ctx.stop_requested:
            for message in await self._broker.poll():
                async with ctx.scope() as scope:         # per-message Scope.REQUEST deps
                    uow = await scope.resolve(UnitOfWork)
                    ctx.log.info("handled", id=message.get("id"), uow=type(uow).__name__)
            await ctx.sleep(1.0)                         # False as soon as stop is requested


@module(providers=[Broker, OrderConsumer, UnitOfWork])
class MessagingModule:
    pass
```

Rules: call with parentheses; define `run(self, ctx)` (`async def`, or `def`
on its own thread that polls `ctx.stop_requested`); keep the class a singleton.
Order: services start after every `@post_construct` (dependency order) and stop
in reverse order **after** requests drain and **before** `@pre_destruct`.
`restart="never" | "on-failure" | "always"`; when restarts run out,
`on_exhausted="fail_readiness"` makes `HostedServiceInfo.healthy` `False`.

## Scheduled methods

```python
from lauren import injectable, module
from lauren.scheduling import JobContext, ScheduleModule, cron, interval, run_once


@injectable()
class Reports:
    @interval(seconds=300, overlap="queue", timeout=120)
    async def refresh(self) -> None: ...

    @cron("30 6 * * MON-FRI", tz="America/New_York", exclusive=True, name="daily-report")
    async def daily(self, ctx: JobContext) -> None:
        ctx.log.info("report", for_day=ctx.scheduled_at.date().isoformat())

    @run_once(delay=15)
    async def warm(self) -> None: ...


@module(imports=[ScheduleModule.for_root(timezone="UTC")], providers=[Reports])
class ReportsModule:
    pass
```

Job parameters: `self`, an optional `JobContext`, and DI parameters visible
from the provider's module — resolved per run in a fresh `OperationScope`
(request-scoped providers are torn down after the run). Options: `name`,
`overlap` (`skip` / `queue` / `allow`), `max_instances`, `timeout`, `jitter`,
`misfire` (`coalesce` / `skip`), `misfire_grace`, `exclusive`, `enabled`.
`@interval` adds `initial_delay` and `mode` (`fixed_rate` grid from startup, or
`fixed_delay` after each run).

Cron: `minute hour dom month dow` (or six fields, seconds first), `*`, lists,
ranges, steps, `JAN`-`DEC`, `SUN`-`SAT`, `@daily`-style macros; both day fields
restricted → either matches. Preview with
`CronExpression("0 3 * * *", tz="UTC").iter_from(now)`.

## Control & observability

- `SchedulerRegistry` (inject it): `jobs()`, `job(name)`, `services()`,
  `await trigger(name)`, `pause`, `resume`, `remove`, `add_interval(name, fn,
  seconds=...)`, `add_cron(name, fn, expression)`.
- Signals: `HostedServiceStarted` / `Stopped` / `Failed`, `JobStarted` /
  `Completed` / `Failed` / `Skipped(reason="overlap" | "locked" | "misfire" | "disabled")`.

## Several workers

`exclusive=True` acquires `lauren:job:{name}:{fire_time}` on the
`SchedulerLock` for each fire; losers emit `JobSkipped(reason="locked")`. The
default `InMemorySchedulerLock` is per process — pass a shared one:

```python fragment
class RedisSchedulerLock:
    async def acquire(self, key: str, *, ttl: float) -> bool:
        return bool(await redis.set(key, "1", nx=True, px=int(ttl * 1000)))

    async def release(self, key: str) -> None:
        await redis.delete(key)

ScheduleModule.for_root(lock=RedisSchedulerLock())
```

Or run jobs in one process: `ScheduleModule.for_root(enabled=False)` /
`LaurenFactory.create(..., scheduler=False, hosted_services=False)` in the web
workers.

## Testing

```python fragment
clock = ManualClock()

@module(imports=[ScheduleModule.for_root(clock=clock)], providers=[Reports])
class TestModule: ...

async def test_refresh_runs_every_five_minutes():
    app = LaurenFactory.create(TestModule)
    await app.startup()
    await clock.advance(minutes=10)          # fires due jobs in order; no real waiting
    registry = await app.container.resolve(SchedulerRegistry)
    assert registry.job("Reports.refresh").runs == 3
    await app.shutdown()
```

- `await clock.jump(hours=1)` simulates a suspended process (misfire tests).
- `enabled=False` / `scheduler=False` + `await registry.trigger(name)` runs one
  job with full DI; `hosted_services=False` builds without starting services.
- Drive `startup()` / `shutdown()` in one event loop; today's `TestClient`
  ends background work with its startup loop.

## Startup errors

| Error | Cause |
|---|---|
| `LifecycleConfigError` | Jobs without `ScheduleModule.for_root()`, a job / service on a non-singleton provider, `run` missing, `ScheduleModule` imported twice |
| `DuplicateBindingError` | Two jobs or two services share a name |
| `UnresolvableParameterError` | A job parameter nothing visible can provide |
| `MetadataInheritanceError` | A registered subclass of a hosted service not re-decorated |
| `DecoratorUsageError` | Bad cron / tz / seconds, bare decorator |
| `LifecycleError` (from `startup()`) | A `wait_ready` service not ready within `start_timeout` |
