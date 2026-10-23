# Scheduling & Hosted Services

Hosted services, scheduled jobs, cron expressions, locks and clocks (`lauren.scheduling`).

## Hosted services

### `hosted_service`

```python
def hosted_service(args: Any = (), name: str | None = None, restart: Restart = 'on-failure', max_restarts: int = 5, restart_window: float | timedelta = 300.0, backoff: tuple[float, float] = (1.0, 30.0), on_exhausted: OnExhausted = 'fail_readiness', wait_ready: bool = False, start_timeout: float | timedelta = 30.0, stop_timeout: float | timedelta = 10.0, enabled: bool = True) -> Callable[[C], C]
```

Mark a class as a hosted service: its ``run(ctx)`` runs for the app's lifetime.

The framework starts ``run`` after every ``@post_construct`` hook (in the
providers' topological order), supervises it, and stops services in
reverse order during shutdown — after requests drain and *before*
``@pre_destruct``. The class must define ``async def run(self, ctx)``
(or a plain ``def``, run on a daemon thread; it must poll
``ctx.stop_requested``) and be listed in a module's ``providers``. It is
made a singleton ``@injectable`` unless it carries its own ``@injectable``.

``restart`` is ``"never"``, ``"on-failure"`` or ``"always"`` (also after a
normal return); at most ``max_restarts`` restarts within
``restart_window`` seconds, waiting a random ``0..min(cap, base * 2**n)``
seconds before the n-th (``backoff=(base, cap)``). When restarts run out,
``on_exhausted="fail_readiness"`` marks the service unhealthy and
``"log"`` only logs. ``wait_ready=True`` makes startup wait up to
``start_timeout`` seconds for ``ctx.mark_ready()``. On shutdown the
service has ``stop_timeout`` seconds to return before it is cancelled.
``enabled=False`` keeps it from starting. Must be called with parentheses.

### `ServiceContext`

```python
class ServiceContext(name: str, clock: Clock, log: Logger, scope_factory: Callable[..., OperationScope])
```

The handle a hosted service's ``run(ctx)`` receives.

* ``stop_requested`` — ``True`` once shutdown asked the service to stop
  (thread-safe, so a sync ``run`` can poll it).
* ``await sleep(seconds)`` — sleeps on the scheduler clock; returns
  ``False`` early when stop is requested, ``True`` otherwise.
* ``await wait_stopped(timeout=None)`` — waits for the stop request;
  ``True`` when it arrived, ``False`` when ``timeout`` ran out.
* ``mark_ready()`` — signals readiness (``@hosted_service(wait_ready=True)``
  makes startup wait for it); ``ready`` reads it back. Readiness resets
  when the service restarts.
* ``name``, ``restart_count`` and ``log`` (the app logger bound with
  ``context="Service:<name>"``).
* ``scope(name=None)`` — a new :class:`~lauren.context.OperationScope` for
  one iteration's ``Scope.REQUEST`` dependencies (the context is seeded
  into it, so such providers may take a ``ServiceContext`` parameter).

#### `ServiceContext.sleep`

```python
def sleep(self, seconds: float) -> bool
```

Sleep ``seconds``; return ``False`` as soon as stop is requested.

#### `ServiceContext.wait_stopped`

```python
def wait_stopped(self, timeout: float | None = None) -> bool
```

Wait for the stop request; ``False`` if ``timeout`` seconds pass first.

#### `ServiceContext.mark_ready`

```python
def mark_ready(self) -> None
```

Signal that the service is ready (safe to call from a sync service's thread).

#### `ServiceContext.scope`

```python
def scope(self, name: str | None = None) -> OperationScope
```

Open a new DI scope for one unit of work (use it with ``async with``).

### `HostedServiceInfo`

```python
class HostedServiceInfo(name: str, state: str, ready: bool, restart_count: int, last_error: str | None, healthy: bool)
```

A snapshot of one hosted service.

``state`` is ``"pending"`` (not started yet, or ``hosted_services=False``),
``"disabled"`` (``enabled=False``), ``"running"``, ``"restarting"``
(waiting out a backoff), ``"stopping"``, ``"stopped"`` (stopped or
returned) or ``"failed"`` (crashed with no restart left). ``healthy`` is
``False`` only for a failed service with ``on_exhausted="fail_readiness"``
— the signal a readiness probe acts on.

#### `HostedServiceInfo.as_dict`

```python
def as_dict(self) -> dict[str, Any]
```

A JSON-friendly ``dict``.

## Scheduled methods

### `interval`

```python
def interval(seconds: Any = _MISSING, initial_delay: float | timedelta = 0.0, mode: IntervalMode = 'fixed_rate', name: str | None = None, overlap: Overlap = 'skip', max_instances: int | None = None, timeout: float | timedelta | None = None, jitter: float | timedelta = 0.0, misfire: Misfire = 'coalesce', misfire_grace: float | timedelta | None = None, exclusive: bool = False, enabled: bool = True) -> Callable[[F], F]
```

Run the decorated singleton method every ``seconds``.

``mode="fixed_rate"`` fires on a grid anchored at startup (``READY``) plus
``initial_delay``, so the first run is ``initial_delay`` seconds after
startup; ``"fixed_delay"`` waits ``seconds`` after each run completes.
``seconds`` / ``initial_delay`` / ``timeout`` / ``jitter`` /
``misfire_grace`` take seconds or a :class:`~datetime.timedelta`. See
:func:`cron` for the common options. Requires ``ScheduleModule.for_root()``.

### `cron`

```python
def cron(expression: Any = _MISSING, tz: str | tzinfo | None = None, name: str | None = None, overlap: Overlap = 'skip', max_instances: int | None = None, timeout: float | timedelta | None = None, jitter: float | timedelta = 0.0, misfire: Misfire = 'coalesce', misfire_grace: float | timedelta | None = None, exclusive: bool = False, enabled: bool = True) -> Callable[[F], F]
```

Run the decorated singleton method on a cron schedule.

``expression`` has five fields (``minute hour day-of-month month
day-of-week``) or six with a leading seconds field, or is a macro such as
``"@daily"``; it is validated here (:class:`~lauren.scheduling.CronExpression`).
``tz`` is an IANA zone (default: the ``ScheduleModule`` time zone).

Common options (``@interval`` and ``@run_once`` take them too): ``name``
(default ``Class.method``, unique app-wide); ``overlap`` — ``"skip"`` a
fire while the previous run is still going, ``"queue"`` one run after it,
or ``"allow"`` concurrent runs; ``max_instances`` (1, or 3 with
``"allow"``); ``timeout`` in seconds (the run is cancelled and reported as
a ``TimeoutError``); ``jitter`` — a random ``0..jitter`` second delay per
fire; ``misfire`` — when the scheduler wakes more than ``misfire_grace``
seconds late, ``"coalesce"`` runs once now and ``"skip"`` waits for the next
fire; ``exclusive`` — take the ``SchedulerLock`` for each fire so one
worker runs it; ``enabled`` — a static switch.

### `run_once`

```python
def run_once(delay: Any = 0.0, name: str | None = None, overlap: Overlap = 'skip', max_instances: int | None = None, timeout: float | timedelta | None = None, jitter: float | timedelta = 0.0, misfire: Misfire = 'coalesce', misfire_grace: float | timedelta | None = None, exclusive: bool = False, enabled: bool = True) -> Callable[[F], F]
```

Run the decorated singleton method once, ``delay`` seconds after startup (``READY``).

The one-shot counterpart of :func:`interval` (NestJS ``@Timeout``). Takes
the common options described on :func:`cron`.

### `JobContext`

```python
class JobContext(name: str, scheduled_at: datetime, fired_at: datetime, log: Logger)
```

What a scheduled run knows about itself; declare a ``JobContext`` parameter to receive it.

* ``name`` — the job name; ``attempt`` is always ``1`` (retries belong to a
  durable job queue, not the scheduler).
* ``scheduled_at`` — the nominal fire time, an aware datetime in the job's
  time zone; ``fired_at`` — when the run actually started.
* ``log`` — the app logger bound with ``context="Job:<name>"``.
* ``cancelled`` — becomes ``True`` when the run times out or shutdown
  cancels it (sync jobs, which cannot be interrupted, should poll it).
* ``scope`` — the run's :class:`~lauren.context.OperationScope` when the
  method takes dependency-injected parameters, else ``None``.

A ``Scope.REQUEST`` provider constructed during the run may also declare a
``JobContext`` constructor parameter.

## Activation and control

### `ScheduleModule`

```python
class ScheduleModule
```

Enables scheduled jobs: import ``ScheduleModule.for_root()`` once, in any module.

Without it, an app whose providers carry ``@interval`` / ``@cron`` /
``@run_once`` fails in ``LaurenFactory.create`` with
:class:`~lauren.exceptions.LifecycleConfigError` instead of silently never
running them. Importing it also makes :class:`SchedulerRegistry`
injectable everywhere.

#### `ScheduleModule.for_root`

```python
def for_root(cls, timezone: str | tzinfo = 'UTC', lock: SchedulerLock | None = None, enabled: bool = True, clock: Clock | None = None, job_drain_timeout: float | None = None) -> type
```

Return the module to put in ``@module(imports=[...])``.

``timezone`` is the default zone of ``@cron`` jobs (and of
``scheduled_at``). ``lock`` backs ``exclusive=True`` jobs (default: a
per-process :class:`InMemorySchedulerLock`). ``enabled=False`` keeps
discovery, validation and :meth:`SchedulerRegistry.trigger` but fires
nothing. ``clock`` replaces the real clock (pass a fresh
:class:`ManualClock` per app in tests; hosted services use it too).
``job_drain_timeout`` bounds how long shutdown waits for in-flight
runs (default: what is left of ``drain_timeout``).

### `SchedulerRegistry`

```python
class SchedulerRegistry()
```

The app's scheduler, for status and control; inject it like any provider.

Available when ``ScheduleModule.for_root()`` is imported (one registry per
app). :meth:`jobs` lists every job (discovered and dynamic) as
:class:`JobInfo`; :meth:`services` lists hosted services as
:class:`~lauren.scheduling.HostedServiceInfo`. :meth:`trigger` runs a job
now and returns its :class:`JobRun`, respecting ``overlap`` and
``exclusive``. Dynamically added jobs (:meth:`add_interval`,
:meth:`add_cron`) receive a ``JobContext`` only — no dependency
injection — and their names must not collide with existing jobs
(:class:`~lauren.exceptions.DuplicateBindingError`). Unknown names raise
``KeyError``.

#### `SchedulerRegistry.jobs`

```python
def jobs(self) -> tuple[JobInfo, ...]
```

Every job, in registration order.

#### `SchedulerRegistry.job`

```python
def job(self, name: str) -> JobInfo
```

One job by name.

#### `SchedulerRegistry.services`

```python
def services(self) -> tuple[HostedServiceInfo, ...]
```

Every hosted service of the app, in start order.

#### `SchedulerRegistry.trigger`

```python
def trigger(self, name: str) -> JobRun
```

Run ``name`` now and wait for it (overlap and ``exclusive`` still apply).

Works while the app is running, also when firing is disabled
(``ScheduleModule.for_root(enabled=False)``), so tests can run a job
with full dependency injection. Raises
:class:`~lauren.exceptions.LifecycleViolationError` before
``startup()`` or during shutdown.

#### `SchedulerRegistry.pause`

```python
def pause(self, name: str) -> None
```

Stop firing ``name`` (a run in progress continues).

#### `SchedulerRegistry.resume`

```python
def resume(self, name: str) -> None
```

Fire ``name`` again from the next fire time after now.

#### `SchedulerRegistry.remove`

```python
def remove(self, name: str) -> None
```

Remove ``name`` from the schedule (a run in progress continues).

#### `SchedulerRegistry.add_interval`

```python
def add_interval(self, name: str, fn: Callable[..., Any], seconds: float | timedelta, initial_delay: float | timedelta = 0.0, mode: str = 'fixed_rate', overlap: str = 'skip', max_instances: int | None = None, timeout: float | timedelta | None = None, jitter: float | timedelta = 0.0, misfire: str = 'coalesce', misfire_grace: float | timedelta | None = None, exclusive: bool = False) -> JobInfo
```

Add a job running ``fn()`` / ``fn(ctx)`` every ``seconds`` (options as ``@interval``).

Added while the app runs, the first fire is ``initial_delay`` seconds
from now; added before startup, from ``READY``.

#### `SchedulerRegistry.add_cron`

```python
def add_cron(self, name: str, fn: Callable[..., Any], expression: str | CronExpression, tz: str | tzinfo | None = None, overlap: str = 'skip', max_instances: int | None = None, timeout: float | timedelta | None = None, jitter: float | timedelta = 0.0, misfire: str = 'coalesce', misfire_grace: float | timedelta | None = None, exclusive: bool = False) -> JobInfo
```

Add a job running ``fn()`` / ``fn(ctx)`` on a cron schedule (options as ``@cron``).

### `JobInfo`

```python
class JobInfo(name: str, kind: str, schedule: str, next_run_at: datetime | None, last_run_at: datetime | None, last_status: str | None, last_error: str | None, last_duration_s: float | None, running: int, paused: bool, enabled: bool, dynamic: bool, runs: int, failures: int, skips: int)
```

A snapshot of one job, returned by :meth:`SchedulerRegistry.jobs`.

``kind`` is ``"interval"``, ``"cron"`` or ``"once"``; ``schedule`` is a
readable description. ``next_run_at`` is ``None`` while the scheduler is
not firing (disabled, before startup, during shutdown) or the job is
paused, disabled or done. ``last_status`` is ``"completed"``,
``"failed"`` or ``"cancelled"`` (``None`` before the first run).
Statistics are bounded: the last outcome plus counters, no history.

#### `JobInfo.as_dict`

```python
def as_dict(self) -> dict[str, Any]
```

A JSON-friendly ``dict`` (datetimes as ISO 8601 strings).

### `JobRun`

```python
class JobRun(name: str, status: RunStatus, scheduled_at: datetime, duration_s: float = 0.0, error: BaseException | None = None, reason: str | None = None)
```

The outcome of :meth:`SchedulerRegistry.trigger`.

``status`` is ``"completed"``, ``"failed"`` (``error`` holds the
exception; a timeout is a ``TimeoutError``) or ``"skipped"`` (``reason``
is ``"overlap"``, ``"locked"`` or ``"disabled"``, or ``"stopping"`` when
shutdown began right after an ``exclusive`` run won its lock).

## Cron expressions, locks and clocks

### `CronExpression`

```python
class CronExpression(expression: str, tz: str | tzinfo | None = None)
```

A parsed cron expression whose next fire times can be previewed.

Five fields (``minute hour day-of-month month day-of-week``) or six with a
leading seconds field; ``*``, lists ``a,b``, ranges ``a-b``, steps ``*/n``
and ``a-b/n``, ``JAN``-``DEC`` and ``SUN``-``SAT`` (``0`` and ``7`` are
Sunday), and the macros ``@yearly`` / ``@annually``, ``@monthly``,
``@weekly``, ``@daily`` / ``@midnight`` and ``@hourly``. When both
day-of-month and day-of-week are restricted, a day matching either fires
(POSIX cron semantics).

``tz`` (an IANA name or a :class:`~datetime.tzinfo`) is the zone the fields
are read in. Without it, an aware datetime passed to :meth:`next_after`
keeps its own zone and a naive one is treated as naive wall time.

#### `CronExpression.with_tz`

```python
def with_tz(self, tz: str | tzinfo | None) -> CronExpression
```

A copy of this expression read in ``tz``.

#### `CronExpression.matches`

```python
def matches(self, moment: datetime) -> bool
```

``True`` when ``moment`` (read in :attr:`tz` when aware) satisfies every field.

#### `CronExpression.next_after`

```python
def next_after(self, moment: datetime) -> datetime
```

The first fire time strictly after ``moment``.

An aware ``moment`` returns an aware datetime in :attr:`tz` (or in
``moment``'s own zone when the expression has none); a naive one is
read as wall time in :attr:`tz` (or stays naive without one).

#### `CronExpression.iter_from`

```python
def iter_from(self, moment: datetime) -> Iterator[datetime]
```

Yield fire times strictly after ``moment``, in increasing order (endless).

### `SchedulerLock`

```python
class SchedulerLock
```

Claims one fire of an ``exclusive=True`` job, cluster-wide when the backend is shared.

The scheduler calls ``acquire(f"lauren:job:{name}:{fire_time_iso}", ttl=...)``
before an exclusive run and skips the run (``JobSkipped(reason="locked")``)
when it returns ``False``. The key names one fire time, so the claim is
held until ``ttl`` runs out rather than released after the run: a worker
that wakes late must not run the same fire again. ``ttl`` is the job's
``timeout``, else ``min(period, 3600)`` seconds, so a crashed holder blocks
nothing for long. ``release`` is called only when a claimed run never
started (the scheduler was stopping).

#### `SchedulerLock.acquire`

```python
def acquire(self, key: str, ttl: float) -> bool
```

Claim ``key`` for ``ttl`` seconds; ``False`` when someone else holds it.

#### `SchedulerLock.release`

```python
def release(self, key: str) -> None
```

Drop the claim on ``key`` (no-op when it is not held).

### `InMemorySchedulerLock`

```python
class InMemorySchedulerLock(clock: Clock | None = None)
```

A :class:`SchedulerLock` held in this process only (the default).

Exclusivity is per process: with several workers, each runs every
exclusive job. Share a store-backed lock across workers, or run
scheduled work in one dedicated process. ``clock`` measures the TTL
(the app's scheduler clock when the lock is created by ``ScheduleModule``).

#### `InMemorySchedulerLock.acquire`

```python
def acquire(self, key: str, ttl: float) -> bool
```

#### `InMemorySchedulerLock.release`

```python
def release(self, key: str) -> None
```

### `ManualClock`

```python
class ManualClock(start: datetime | None = None)
```

A clock that only moves when :meth:`advance` is awaited (for tests).

``start`` is the initial wall time (an aware datetime; a naive one is read
as UTC). It defaults to ``2000-01-01T00:00:00Z``; the monotonic reading
starts at ``0.0``. Wall time and monotonic time move together.

:meth:`advance` wakes every sleeper whose deadline falls inside the window,
in deadline order, and lets the event loop run after each wake-up, so
every job due in the window fires (and a job that does not block
finishes) before it returns. Sync jobs run on worker threads; ``advance``
waits for those it started (up to five seconds of real time each round).
:meth:`jump` moves time without waking anything on the way (a suspended
process), to test misfire handling. Use one clock per app and drive it
from the loop the app runs on.

#### `ManualClock.monotonic`

```python
def monotonic(self) -> float
```

#### `ManualClock.now`

```python
def now(self) -> datetime
```

#### `ManualClock.sleep`

```python
def sleep(self, seconds: float) -> None
```

#### `ManualClock.advance`

```python
def advance(self, seconds: float = 0.0, minutes: float = 0.0, hours: float = 0.0, days: float = 0.0) -> None
```

Move time forward, waking (in order) every sleeper due in the window.

``advance()`` with no argument just lets pending work run. Raises
``ValueError`` for a negative step: the clock never goes backwards.

#### `ManualClock.jump`

```python
def jump(self, seconds: float = 0.0, minutes: float = 0.0, hours: float = 0.0, days: float = 0.0) -> None
```

Move time forward in one step, as if the process had been suspended.

Unlike :meth:`advance`, nothing wakes on the way: every sleeper due in
the window wakes together, *after* the jump — the way to test misfire
handling. Raises ``ValueError`` for a negative step.
