# Scheduling & Hosted Services

Real services run work that is not a request: an outbox relay, a queue
consumer, a cache warmer, an expired-session purge at 03:00, a heartbeat.
Lauren gives that work a framework-owned home:

* **Hosted services** — classes whose `run(ctx)` the framework starts after
  startup, supervises (restart policy, backoff, failure signals, health) and
  stops in a deterministic order during shutdown.
* **Scheduled methods** — `@interval`, `@cron` and `@run_once` on singleton
  injectables, discovered and validated in `LaurenFactory.create(...)`, with
  overlap control, jitter, per-run timeouts, misfire handling and a fresh DI
  scope per run.

Never start loops with `asyncio.create_task(...)` inside `@post_construct`:
such a task is unsupervised, invisible to shutdown (it can still be using the
database after the pool closed), silently dies under the test client and runs
once per worker.

---

## Hosted services

<!-- docs-contract: run -->
```python
import asyncio

from lauren import LaurenFactory, injectable, module, pre_destruct
from lauren.scheduling import ManualClock, ScheduleModule, ServiceContext, hosted_service


@injectable()
class Outbox:
    def __init__(self) -> None:
        self.pending = ["order-1", "order-2", "order-3"]
        self.published: list[str] = []
        self.closed_after: list[str] = []

    @pre_destruct
    def close(self) -> None:
        self.closed_after = list(self.published)   # runs after the relay stopped


@hosted_service(restart="on-failure", max_restarts=5, backoff=(1.0, 30.0), stop_timeout=10.0)
class OutboxRelay:
    def __init__(self, outbox: Outbox) -> None:
        self._outbox = outbox

    async def run(self, ctx: ServiceContext) -> None:
        ctx.mark_ready()
        while not ctx.stop_requested:
            if self._outbox.pending:
                self._outbox.published.append(self._outbox.pending.pop(0))
            await ctx.sleep(1.0)  # returns early when stop is requested


clock = ManualClock()  # tests drive time; production uses the real clock


@module(imports=[ScheduleModule.for_root(clock=clock)], providers=[Outbox, OutboxRelay])
class RelayModule:
    pass


async def demo() -> Outbox:
    app = LaurenFactory.create(RelayModule)
    await app.startup()       # @post_construct, then hosted services start
    await clock.advance(5)    # five simulated seconds, no real waiting
    await app.shutdown()      # the relay stops before Outbox's @pre_destruct
    return app.container.singletons()[Outbox]


outbox = asyncio.run(demo())
assert outbox.published == outbox.closed_after == ["order-1", "order-2", "order-3"]
```

The contract:

* `@hosted_service(...)` must be called with parentheses. It checks at
  decoration time that the class defines `run(self, ctx)` (else
  `LifecycleConfigError`), stores its options on the class's own `__dict__`
  and makes the class a singleton `@injectable` unless it carries its own
  `@injectable`. List the class in a module's `providers` like any provider —
  its constructor receives dependencies as usual.
* `run` may be `async def` (recommended) or a plain `def`. A sync `run` runs on
  its own daemon thread and must poll `ctx.stop_requested`; threads cannot be
  cancelled, so after `stop_timeout` the framework stops waiting and logs that
  the thread was abandoned.
* Hosted services do not need `ScheduleModule`; import it only to share its
  `clock=` (tests) or to inject `SchedulerRegistry`, which also reports
  hosted-service status.

`ServiceContext`:

| Member | Meaning |
|---|---|
| `stop_requested` | `True` once shutdown asked the service to stop (thread-safe) |
| `await sleep(seconds)` | Sleep on the scheduler clock; returns `False` early when stop is requested |
| `await wait_stopped(timeout=None)` | `True` when stop arrived, `False` when `timeout` ran out |
| `mark_ready()` / `ready` | Signal readiness (thread-safe); reset on every restart |
| `name`, `restart_count`, `log` | The service name, restarts so far, the app logger bound with `context="Service:<name>"` |
| `scope(name=None)` | A new `OperationScope` for one unit of work's `Scope.REQUEST` dependencies |

Options:

| Option | Default | Meaning |
|---|---|---|
| `name` | the class name | Unique app-wide (`DuplicateBindingError` otherwise) |
| `restart` | `"on-failure"` | `"never"`, `"on-failure"`, or `"always"` (also after a normal return) |
| `max_restarts` | `5` | Restarts allowed within `restart_window` |
| `restart_window` | `300.0` s | Sliding window for counting restarts |
| `backoff` | `(1.0, 30.0)` | `(base, cap)`: the n-th restart waits a random `0..min(cap, base * 2**n)` seconds (full jitter) |
| `on_exhausted` | `"fail_readiness"` | When restarts run out: `"fail_readiness"` marks the service unhealthy (`HostedServiceInfo.healthy` is `False`); `"log"` only logs |
| `wait_ready` | `False` | Startup waits up to `start_timeout` for `ctx.mark_ready()` |
| `start_timeout` | `30.0` s | With `wait_ready=True`; exceeded (or the service exits first) → `startup()` raises `LifecycleError` and stops the services already started |
| `stop_timeout` | `10.0` s | Grace period after stop is requested, before the task is cancelled |
| `enabled` | `True` | Static switch (for example `enabled=settings.relay_enabled`) |

Per-iteration dependencies come from `ctx.scope()`, the hosted-service
equivalent of a request: `Scope.REQUEST` providers get one instance per scope,
torn down (`@pre_destruct`, then `aclose()`) when the block exits.

<!-- docs-contract: fragment -->
```python
async def run(self, ctx: ServiceContext) -> None:
    while not ctx.stop_requested:
        async with ctx.scope() as scope:
            db = await scope.resolve(DbSession)      # fresh per iteration
            await self._relay(db)
        await ctx.sleep(1.0)
```

---

## Scheduled methods

<!-- docs-contract: run -->
```python
import asyncio
from datetime import timedelta

from lauren import LaurenFactory, Scope, injectable, module
from lauren.scheduling import JobContext, ManualClock, ScheduleModule, cron, interval, run_once


@injectable(scope=Scope.REQUEST)
class DbSession:
    def __init__(self) -> None:
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


@injectable()
class Maintenance:
    def __init__(self) -> None:
        self.events: list[str] = []

    @interval(seconds=60, initial_delay=5, jitter=5, overlap="skip")
    async def warm_cache(self) -> None:
        self.events.append("warm")

    @cron("0 3 * * *", tz="UTC", name="purge-sessions", timeout=300, exclusive=True)
    async def purge_expired(self, ctx: JobContext, db: DbSession) -> None:  # db: one per run
        cutoff = ctx.scheduled_at - timedelta(days=30)
        self.events.append(f"purge before {cutoff:%Y-%m-%d}")

    @run_once(delay=10)
    async def prime_search_index(self) -> None:
        self.events.append("prime")


clock = ManualClock()


@module(imports=[ScheduleModule.for_root(timezone="UTC", clock=clock)], providers=[Maintenance, DbSession])
class AppModule:
    pass


async def first_day() -> list[str]:
    app = LaurenFactory.create(AppModule)
    await app.startup()
    await clock.advance(hours=3, minutes=1)   # 2000-01-01 00:00 -> 03:01 UTC
    await app.shutdown()
    return app.container.singletons()[Maintenance].events


events = asyncio.run(first_day())
assert events.count("prime") == 1 and "purge before 1999-12-02" in events
assert events.count("warm") == 181   # 00:00:05 + every minute up to 03:00:05
```

Activation is explicit: an app whose providers carry scheduled methods but
that does not import `ScheduleModule.for_root()` fails in
`LaurenFactory.create` with `LifecycleConfigError` — *"Maintenance.warm_cache
is scheduled (@interval) but scheduling is not enabled; import
ScheduleModule.for_root() (or ScheduleModule.for_root(enabled=False) to disable
explicitly)"*. Jobs never silently not run.

`ScheduleModule.for_root(...)`:

| Option | Default | Meaning |
|---|---|---|
| `timezone` | `"UTC"` | Default zone of `@cron` jobs and of `scheduled_at` |
| `lock` | per-process `InMemorySchedulerLock` | `SchedulerLock` backing `exclusive=True` jobs |
| `enabled` | `True` | `False` keeps discovery, validation and `trigger()` but fires nothing |
| `clock` | the real clock | A `Clock`; pass a fresh `ManualClock` per app in tests (hosted services use it too) |
| `job_drain_timeout` | what is left of `drain_timeout` | How long shutdown waits for in-flight runs before cancelling them |

Decorators:

| Decorator | Semantics |
|---|---|
| `@interval(seconds, *, initial_delay=0, mode="fixed_rate", ...)` | `"fixed_rate"` fires on a grid anchored at startup (`READY`) plus `initial_delay` — the first run is `initial_delay` seconds after startup; `"fixed_delay"` waits `seconds` after each run completes |
| `@cron(expression, *, tz=None, ...)` | Next fire computed in `tz` (default: the `ScheduleModule` time zone) |
| `@run_once(delay=0, ...)` | Fires once, `delay` seconds after `READY` |

Common options (all three decorators):

| Option | Default | Meaning |
|---|---|---|
| `name` | `Class.method` | Unique app-wide; stacked schedules on one method default to `Class.method#1`, `#2`, … |
| `overlap` | `"skip"` | While the previous run is still going: `"skip"` the fire (`JobSkipped(reason="overlap")`), `"queue"` one run right after it (at most one queued), or `"allow"` concurrent runs up to `max_instances` |
| `max_instances` | `1` (`3` with `"allow"`) | Concurrency cap per job |
| `timeout` | `None` | Cancel a run after N seconds → `JobFailed(error=TimeoutError)` |
| `jitter` | `0` | A uniform random `0..jitter` second delay per fire (spreads multi-worker load; the grid does not drift) |
| `misfire` | `"coalesce"` | If the scheduler wakes more than `misfire_grace` late: `"coalesce"` runs once now for the latest missed fire; `"skip"` waits for the next fire (`JobSkipped(reason="misfire")`) |
| `misfire_grace` | `min(60 s, period)` | Lateness that still counts as on time |
| `exclusive` | `False` | Take the `SchedulerLock` for each fire so one worker runs it |
| `enabled` | `True` | Static switch |

Seconds-valued options also accept a `datetime.timedelta`. Stacking
accumulates: one method may carry `@interval` **and** `@cron`.

Method rules, validated at startup:

* The provider must be `Scope.SINGLETON` — the job runs on the singleton
  instance (`LifecycleConfigError` otherwise). Static and class methods work.
* Parameters may be `self`, a `JobContext` (any name, matched by annotation)
  and **dependencies** resolvable from the provider's module
  (`UnresolvableParameterError` naming the job and parameter otherwise). They
  are resolved per run in a fresh `OperationScope`, so `Scope.REQUEST`
  providers get one instance per run, torn down when the run ends. Such a
  provider may itself take a `JobContext` constructor parameter.
* Sync methods run in a worker thread, with the same timeout semantics as sync
  hosted services (the thread is abandoned; poll `ctx.cancelled`).
* Scheduled methods are found the way routes are: inherited methods count, and
  an override without the decorator removes the schedule.

`JobContext`: `name`, `scheduled_at` (the nominal fire time, aware, in the
job's time zone), `fired_at`, `attempt` (always `1`; retries belong to a
durable job queue), `log` (bound with `context="Job:<name>"`), `cancelled` and
`scope` (the run's `OperationScope` when the method takes dependencies, else
`None`).

---

## Cron expressions

| Syntax | Meaning |
|---|---|
| `m h dom mon dow` | Five fields: minute (0-59), hour (0-23), day of month (1-31), month (1-12), day of week (0-7; 0 and 7 are Sunday) |
| `s m h dom mon dow` | Six fields: a **leading** seconds field (0-59) |
| `*`, `a,b`, `a-b`, `*/n`, `a-b/n`, `a/n` | Any, list, range, step, stepped range, step from `a` |
| `JAN`-`DEC`, `SUN`-`SAT` | Names (case-insensitive); `FRI-SUN` wraps to Sunday |
| `@yearly` / `@annually`, `@monthly`, `@weekly`, `@daily` / `@midnight`, `@hourly` | Macros |

When both day-of-month and day-of-week are restricted, a day matching
**either** fires (POSIX cron: `0 0 1,15 * SUN` runs on the 1st, the 15th and
every Sunday). Quartz extensions (`L`, `W`, `#`, `?`) and `@reboot` are
rejected, and so is any expression that can never fire (`0 0 30 2 *`).
Validation happens at decoration time: `@cron("61 * * * *")` raises
`DecoratorUsageError` whose `detail` names the `field`, the `allowed` range and
the `reason`. An unknown `tz=` is rejected the same way (on Windows, install
the `tzdata` package for IANA zones; `"UTC"` always works).

Daylight-saving policy:

| Situation | Behaviour |
|---|---|
| Local time does not exist (spring-forward gap, e.g. 02:30 in New York on the March change) | Fires once, at the first valid instant after the gap (03:00) |
| Local time is ambiguous (fall-back, e.g. 01:30 in New York on the November change) | Fires once, at the first occurrence; the repeated hour does not fire again |
| `@interval` / `@run_once` | Monotonic clock: unaffected by DST and wall-clock changes |

`CronExpression` is public, to preview a schedule:

<!-- docs-contract: run -->
```python
from datetime import datetime, timezone
from itertools import islice

from lauren.scheduling import CronExpression

nightly = CronExpression("0 3 * * MON-FRI", tz="UTC")
start = datetime(2026, 10, 23, 12, 0, tzinfo=timezone.utc)   # a Friday

upcoming = [dt.strftime("%a %d %H:%M") for dt in islice(nightly.iter_from(start), 3)]
assert upcoming == ["Mon 26 03:00", "Tue 27 03:00", "Wed 28 03:00"]
assert nightly.next_after(start) == datetime(2026, 10, 26, 3, 0, tzinfo=timezone.utc)
assert nightly.matches(datetime(2026, 10, 26, 3, 0, tzinfo=timezone.utc))
```

---

## Lifecycle: the exact order

**Startup** (`LaurenApp.startup()`):

1. `StartupBegin`.
2. Every `@post_construct` hook, in dependency order.
3. **Hosted services start**, in their providers' dependency order (a service
   that injects another starts after it). `wait_ready=True` services are
   awaited up to `start_timeout`.
4. **The scheduler is armed.** Nothing fires yet.
5. The app is ready; `StartupComplete`.
6. **The scheduler starts firing.** `@run_once` delays and `initial_delay`
   count from here.

**Shutdown** (`LaurenApp.shutdown(drain_timeout=...)`):

1. `ShutdownBegin`; **the scheduler stops firing** at once. Hosted services
   keep running — in-flight requests may depend on them.
2. In-flight requests drain.
3. **In-flight job runs** are awaited up to `job_drain_timeout` (default: what
   is left of `drain_timeout`), then cancelled (`JobFailed` with a
   `CancelledError`).
4. **Hosted services stop in reverse start order**: stop is requested, each
   gets `stop_timeout`, then it is cancelled.
5. `on_shutdown` callbacks.
6. `@pre_destruct` hooks — **after** services stop, so a service's
   dependencies are alive for its whole graceful stop.

Under uvicorn, `lifespan.shutdown` arrives after the server stopped accepting
connections, so steps 3-6 run in a quiet process.

---

## Observability

**Signals** (on `app.signals`, also importable from `lauren.signals`):

| Event | Fields |
|---|---|
| `HostedServiceStarted` | `name` — each time `run()` starts (restarts included) |
| `HostedServiceStopped` | `name`, `duration_s` (since the first start) — once the service is done for good |
| `HostedServiceFailed` | `name`, `error`, `restart_count`, `will_restart` |
| `JobStarted` | `name`, `scheduled_at` |
| `JobCompleted` | `name`, `duration_s`, `scheduled_at` |
| `JobFailed` | `name`, `error` (a `TimeoutError` for timeouts, `CancelledError` at shutdown), `duration_s`, `scheduled_at` |
| `JobSkipped` | `name`, `reason` (`"overlap"`, `"locked"`, `"misfire"`, `"disabled"`), `scheduled_at` |

**`SchedulerRegistry`** — one per app, injectable once `ScheduleModule` is
imported:

<!-- docs-contract: run -->
```python
from lauren import controller, get, post
from lauren.scheduling import SchedulerRegistry


@controller("/admin/jobs")
class JobsController:
    def __init__(self, registry: SchedulerRegistry) -> None:
        self._registry = registry

    @get("/")
    async def list_jobs(self) -> list:
        # name, kind, schedule, next_run_at, last_run_at, last_status, running, counters
        return [job.as_dict() for job in self._registry.jobs()]

    @get("/services")
    async def services(self) -> list:
        return [service.as_dict() for service in self._registry.services()]

    @post("/{name}/run")
    async def run_now(self, name: str) -> dict:
        run = await self._registry.trigger(name)   # respects overlap and exclusive
        return {"status": run.status}
```

`SchedulerRegistry` also has `job(name)`, `pause(name)`, `resume(name)`,
`remove(name)`, `add_interval(name, fn, seconds=...)` and
`add_cron(name, fn, expression, tz=...)`. Dynamically added jobs receive a
`JobContext` only (no dependency injection) and their names must not collide
with existing jobs (`DuplicateBindingError`). Statistics are bounded — the
last outcome and counters, no history. `HostedServiceInfo.healthy` is the
readiness signal for a service whose restarts are exhausted.

---

## Multiple workers

`uvicorn --workers 4` runs four copies of every job. Pick one of:

* **`exclusive=True` with a shared lock.** For each fire the scheduler calls
  `lock.acquire("lauren:job:{name}:{fire_time_iso}", ttl=...)` and skips the
  run (`JobSkipped(reason="locked")`) when another worker holds it. The key
  names one fire time and is held until the TTL (the job's `timeout`, else
  `min(period, 1 h)`) runs out, so a worker that wakes late cannot re-run it
  and a crashed holder blocks nothing for long. The default
  `InMemorySchedulerLock` is per process — the app logs one INFO line at
  startup saying so. Cron jobs suit this best; interval grids differ per
  worker.
* **A dedicated process**: serve HTTP with `ScheduleModule.for_root(enabled=False)`
  (or `LaurenFactory.create(..., scheduler=False, hosted_services=False)`) and
  run one scheduler process.
* **An external scheduler** (a Kubernetes `CronJob`) running a one-off command
  for heavy or critical work.

A shared lock needs only two methods. A Redis sketch until a store-backed lock
ships:

<!-- docs-contract: fragment -->
```python
class RedisSchedulerLock:
    def __init__(self, redis) -> None:
        self._redis = redis

    async def acquire(self, key: str, *, ttl: float) -> bool:
        return bool(await self._redis.set(key, "1", nx=True, px=int(ttl * 1000)))

    async def release(self, key: str) -> None:
        await self._redis.delete(key)


ScheduleModule.for_root(lock=RedisSchedulerLock(redis))
```

---

## Testing

* **`ManualClock`** — `ScheduleModule.for_root(clock=ManualClock(start=...))`.
  `await clock.advance(minutes=5)` fires every job due in the window, in
  order, and waits for runs that do not block (sync jobs on threads included).
  `await clock.jump(hours=1)` moves time without waking anything on the way —
  a suspended process — to test misfire handling. Hosted services'
  `ctx.sleep()` and restart backoff follow the same clock. (Lifecycle bounds —
  `start_timeout`, `stop_timeout`, the job drain — stay in real time, so a
  manual clock can never hang `startup()` or `shutdown()`.)
* **`ScheduleModule.for_root(enabled=False)`** or
  **`LaurenFactory.create(..., scheduler=False)`** keep discovery and
  validation but fire nothing; **`await registry.trigger("name")`** then runs
  one job with full dependency injection and scope semantics.
* **`LaurenFactory.create(..., hosted_services=False)`** builds and validates
  hosted services without starting them.
* `with TestClient(app) as client:` runs startup, requests and shutdown on one
  persistent event loop, so hosted services and the scheduler keep running
  between requests and stop when the block exits. In async tests, drive
  `await app.startup()` / `await app.shutdown()` (or `AsyncTestClient`) on the
  test's own loop.

---

## Startup errors

| Condition | Error |
|---|---|
| Malformed cron, unknown `tz`, non-positive `seconds`, bare decorator | `DecoratorUsageError` (at decoration) |
| `@hosted_service` class without `run(self, ctx)` | `LifecycleConfigError` (at decoration) |
| Hosted service or scheduled job on a non-singleton provider | `LifecycleConfigError` (`reason="not_singleton"`) |
| Scheduled methods but no `ScheduleModule.for_root()` | `LifecycleConfigError` (`reason="scheduling_not_enabled"`) |
| `ScheduleModule.for_root()` imported twice | `LifecycleConfigError` (`reason="duplicate_schedule_module"`) |
| Two jobs or two services with one name | `DuplicateBindingError` |
| A job parameter nothing can provide | `UnresolvableParameterError` |
| A registered subclass of a hosted service that is not re-decorated | `MetadataInheritanceError` |
| A `wait_ready` service not ready within `start_timeout` | `LifecycleError` from `startup()` |

See also: [Lifecycle hooks](../core-concepts/lifecycle.md),
[Background tasks](background-tasks.md) (per-request fire-and-forget work),
[Signals](signals.md) and the [scheduling reference](../reference/scheduling.md).
