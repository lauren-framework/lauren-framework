"""Integration tests for the Cron & Interval Jobs skill (``skills/cron-interval-jobs``).

The skill's jobs run through ``@interval`` / ``@cron`` / ``@run_once`` and
``ScheduleModule.for_root()``; its status endpoint uses ``SchedulerRegistry``.
"""

from __future__ import annotations

from datetime import timedelta

from lauren import LaurenFactory, Scope, controller, get, injectable, module, post
from lauren.scheduling import (
    JobContext,
    ManualClock,
    ScheduleModule,
    SchedulerRegistry,
    cron,
    interval,
    run_once,
)
from lauren.testing import TestClient

# ---------------------------------------------------------------------------
# Domain (as in the skill)
# ---------------------------------------------------------------------------


class Journal:
    """Records job activity so the tests can observe it."""

    def __init__(self) -> None:
        self.entries: list[str] = []


@injectable(scope=Scope.REQUEST)
class DbSession:
    closed: list[bool] = []

    async def aclose(self) -> None:
        DbSession.closed.append(True)


@injectable()
class CacheService:
    def __init__(self) -> None:
        self.warmed = 0

    async def warm_up(self) -> None:
        self.warmed += 1


@injectable()
class Maintenance:
    def __init__(self, cache: CacheService) -> None:
        self._cache = cache
        self.purges: list[str] = []
        self.primed = 0

    @interval(seconds=60, initial_delay=5, jitter=5)
    async def cache_warmup(self) -> None:
        await self._cache.warm_up()

    @cron("0 3 * * *", tz="UTC", name="purge-sessions", timeout=300)
    async def purge(self, ctx: JobContext, db: DbSession) -> None:
        cutoff = ctx.scheduled_at - timedelta(days=30)
        self.purges.append(cutoff.date().isoformat())

    @run_once(delay=10)
    async def prime(self) -> None:
        self.primed += 1


@controller("/scheduler")
class SchedulerController:
    def __init__(self, registry: SchedulerRegistry) -> None:
        self._registry = registry

    @get("/status")
    async def status(self) -> list:
        return [job.as_dict() for job in self._registry.jobs()]

    @post("/{name}/run")
    async def run_now(self, name: str) -> dict:
        return {"status": (await self._registry.trigger(name)).status}


def _module(*, clock: ManualClock | None = None, enabled: bool = True) -> type:
    @module(
        imports=[ScheduleModule.for_root(timezone="UTC", clock=clock, enabled=enabled)],
        controllers=[SchedulerController],
        providers=[Maintenance, CacheService, DbSession],
    )
    class AppModule:
        pass

    return AppModule


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestStatusEndpoint:
    def test_status_lists_every_job(self) -> None:
        client = TestClient(LaurenFactory.create(_module(enabled=False)))

        names = [job["name"] for job in client.get("/scheduler/status").json()]

        assert names == ["Maintenance.cache_warmup", "purge-sessions", "Maintenance.prime"]

    def test_run_now_executes_a_job_with_its_dependencies(self) -> None:
        app = LaurenFactory.create(_module(enabled=False))
        client = TestClient(app)

        response = client.post("/scheduler/purge-sessions/run")

        assert response.json() == {"status": "completed"}
        assert len(app.container.singletons()[Maintenance].purges) == 1

    def test_status_reports_the_last_run(self) -> None:
        client = TestClient(LaurenFactory.create(_module(enabled=False)))
        client.post("/scheduler/Maintenance.prime/run")

        prime = next(j for j in client.get("/scheduler/status").json() if j["name"] == "Maintenance.prime")

        assert prime["last_status"] == "completed" and prime["runs"] == 1


class TestSchedule:
    async def test_jobs_fire_on_the_clock(self) -> None:
        clock = ManualClock()
        app = LaurenFactory.create(_module(clock=clock))
        await app.startup()

        await clock.advance(hours=3, minutes=1)
        await app.shutdown()

        maintenance = app.container.singletons()[Maintenance]
        cache = app.container.singletons()[CacheService]
        assert cache.warmed == 181  # 00:00:05 and every minute up to 03:00:05
        assert maintenance.primed == 1
        assert maintenance.purges == ["1999-12-02"]
        assert DbSession.closed  # the per-run session was torn down
