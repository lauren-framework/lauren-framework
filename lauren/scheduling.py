"""Hosted services and scheduled jobs: work that is not a request.

A **hosted service** is a class whose ``run(ctx)`` the framework starts after
startup, supervises (restart policy, backoff, failure signals) and stops in
a deterministic order during shutdown — after requests drain, before
``@pre_destruct``::

    from lauren.scheduling import ServiceContext, hosted_service


    @hosted_service(restart="on-failure", stop_timeout=10.0)
    class OutboxRelay:
        def __init__(self, db: Database) -> None:
            self._db = db

        async def run(self, ctx: ServiceContext) -> None:
            while not ctx.stop_requested:
                await self._db.relay_outbox()
                await ctx.sleep(1.0)  # returns early when stop is requested

**Scheduled methods** on singleton injectables run on an interval, a cron
expression or once after startup; activation is explicit with
``ScheduleModule.for_root()``::

    from lauren import injectable, module
    from lauren.scheduling import JobContext, ScheduleModule, cron, interval


    @injectable()
    class Maintenance:
        @interval(seconds=60, jitter=5)
        async def warm_cache(self) -> None: ...

        @cron("0 3 * * *", tz="Europe/Paris", timeout=300)
        async def purge(self, ctx: JobContext, db: DbSession) -> None:  # db: Scope.REQUEST, per run
            ...


    @module(imports=[ScheduleModule.for_root()], providers=[Maintenance, DbSession, OutboxRelay])
    class AppModule: ...

Everything is discovered and validated in ``LaurenFactory.create``: a bad
cron expression or time zone fails at decoration time; a non-singleton
provider, a duplicate name, an unresolvable job parameter or a missing
``ScheduleModule`` fail at startup. Time flows through a :class:`Clock`, so
tests use :class:`ManualClock` and never wait on wall-clock time.
"""

from __future__ import annotations

from ._scheduling._clock import Clock, ManualClock, SystemClock
from ._scheduling._context import JobContext, ServiceContext
from ._scheduling._cron import CronExpression
from ._scheduling._decorators import HostedServiceMeta, ScheduleMeta, cron, hosted_service, interval, run_once
from ._scheduling._lock import InMemorySchedulerLock, SchedulerLock
from ._scheduling._module import ScheduleModule
from ._scheduling._scheduler import JobInfo, JobRun, SchedulerRegistry
from ._scheduling._supervisor import HostedServiceInfo
from .signals import (
    HostedServiceFailed,
    HostedServiceStarted,
    HostedServiceStopped,
    JobCompleted,
    JobFailed,
    JobSkipped,
    JobStarted,
)

__all__ = [
    # decorators
    "hosted_service",
    "interval",
    "cron",
    "run_once",
    "ScheduleMeta",
    "HostedServiceMeta",
    # activation and control
    "ScheduleModule",
    "SchedulerRegistry",
    "JobInfo",
    "JobRun",
    "HostedServiceInfo",
    # contexts
    "JobContext",
    "ServiceContext",
    # cron
    "CronExpression",
    # locks
    "SchedulerLock",
    "InMemorySchedulerLock",
    # clocks
    "Clock",
    "SystemClock",
    "ManualClock",
    # signals (also in lauren.signals)
    "HostedServiceStarted",
    "HostedServiceStopped",
    "HostedServiceFailed",
    "JobStarted",
    "JobCompleted",
    "JobFailed",
    "JobSkipped",
]
