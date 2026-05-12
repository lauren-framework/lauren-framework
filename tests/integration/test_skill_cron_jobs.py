"""Integration tests for the Cron & Interval Jobs skill (Skill 37).

All tests go through the TestClient / HTTP layer.
"""

from __future__ import annotations

import asyncio

from lauren import (
    LaurenFactory,
    Path,
    Scope,
    controller,
    get,
    injectable,
    module,
    post,
    post_construct,
    pre_destruct,
)
from lauren.exceptions import RouteNotFoundError
from lauren.testing import TestClient


# ---------------------------------------------------------------------------
# Domain
# ---------------------------------------------------------------------------


@injectable(scope=Scope.SINGLETON)
class SchedulerService:
    def __init__(self) -> None:
        self._tasks: list[asyncio.Task] = []
        self._jobs: list[tuple] = []
        self.executed_jobs: list[str] = []
        self._started = False

    def register(self, func, interval_seconds: float, name: str = "") -> None:
        """Register a job. If already started, launch the task immediately."""
        entry = (func, interval_seconds, name or func.__name__)
        self._jobs.append(entry)
        if self._started:
            task = asyncio.create_task(self._run_interval(func, interval_seconds, entry[2]))
            self._tasks.append(task)

    def job_names(self) -> list[str]:
        return [j[2] for j in self._jobs]

    @post_construct
    async def start(self) -> None:
        self._started = True
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
        self._started = False

    async def _run_interval(self, func, interval: float, name: str) -> None:
        while True:
            try:
                await func()
                self.executed_jobs.append(name)
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            await asyncio.sleep(interval)

    async def run_once(self, name: str) -> None:
        """Manually execute a named job once."""
        for func, _, job_name in self._jobs:
            if job_name == name:
                await func()
                self.executed_jobs.append(name)
                return
        raise KeyError(f"No job named {name!r}")


# ---------------------------------------------------------------------------
# A sample job injectable
# ---------------------------------------------------------------------------


@injectable(scope=Scope.SINGLETON)
class HeartbeatJob:
    def __init__(self, scheduler: SchedulerService) -> None:
        self._scheduler = scheduler
        self._scheduler.register(self.ping, interval_seconds=0.001, name="heartbeat")
        self.ping_count: int = 0

    async def ping(self) -> None:
        self.ping_count += 1


# ---------------------------------------------------------------------------
# Controller & Module
# ---------------------------------------------------------------------------


@controller("/scheduler")
class SchedulerController:
    def __init__(self, scheduler: SchedulerService) -> None:
        self._scheduler = scheduler

    @get("/status")
    async def status(self) -> dict:
        return {
            "task_count": len(self._scheduler._tasks),
            "executed": len(self._scheduler.executed_jobs),
            "started": self._scheduler._started,
        }

    @get("/jobs")
    async def jobs(self) -> dict:
        return {
            "jobs": self._scheduler.job_names(),
            "count": len(self._scheduler.job_names()),
        }

    @get("/executed")
    async def executed(self) -> dict:
        return {"jobs": self._scheduler.executed_jobs}

    @post("/run/{job_name}")
    async def run_once(self, job_name: Path[str]) -> dict:
        try:
            await self._scheduler.run_once(job_name)
            return {"executed": job_name}
        except KeyError:
            raise RouteNotFoundError(f"Job '{job_name}' not registered")


@module(controllers=[SchedulerController], providers=[SchedulerService, HeartbeatJob])
class SchedulerModule:
    pass


@module(controllers=[SchedulerController], providers=[SchedulerService])
class SchedulerOnlyModule:
    pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def build_app(root_module=None) -> TestClient:
    return TestClient(LaurenFactory.create(root_module or SchedulerModule))


# ---------------------------------------------------------------------------
# Tests — all through TestClient
# ---------------------------------------------------------------------------


class TestSchedulerViaClient:
    def test_no_jobs_registered_shows_empty(self) -> None:
        client = build_app(SchedulerOnlyModule)
        r = client.get("/scheduler/jobs")
        assert r.status_code == 200
        assert r.json()["count"] == 0
        assert r.json()["jobs"] == []

    def test_registered_job_appears_in_list(self) -> None:
        client = build_app()
        r = client.get("/scheduler/jobs")
        assert r.status_code == 200
        assert "heartbeat" in r.json()["jobs"]

    def test_run_once_executes_registered_job(self) -> None:
        client = build_app()
        r = client.post("/scheduler/run/heartbeat")
        assert r.status_code == 200
        assert r.json()["executed"] == "heartbeat"

    def test_run_once_unknown_returns_404(self) -> None:
        client = build_app(SchedulerOnlyModule)
        r = client.post("/scheduler/run/nonexistent")
        assert r.status_code == 404

    def test_executed_endpoint_records_manual_runs(self) -> None:
        client = build_app()
        client.post("/scheduler/run/heartbeat")
        client.post("/scheduler/run/heartbeat")
        r = client.get("/scheduler/executed")
        assert r.json()["jobs"].count("heartbeat") >= 2


class TestSchedulerController:
    def test_status_endpoint_shows_tasks(self) -> None:
        client = build_app()
        r = client.get("/scheduler/status")
        assert r.status_code == 200
        body = r.json()
        assert body["task_count"] >= 1
        assert body["started"] is True

    def test_executed_endpoint(self) -> None:
        client = build_app(SchedulerOnlyModule)
        r = client.get("/scheduler/executed")
        assert r.status_code == 200
        assert "jobs" in r.json()

    def test_app_starts_and_stops_cleanly(self) -> None:
        client = build_app()
        r = client.get("/scheduler/status")
        assert r.status_code == 200


class TestSchedulerLifecycle:
    def test_post_construct_starts_tasks(self) -> None:
        client = build_app()
        r = client.get("/scheduler/status")
        assert r.json()["task_count"] >= 1

    def test_scheduler_without_jobs_starts_empty(self) -> None:
        client = build_app(SchedulerOnlyModule)
        r = client.get("/scheduler/status")
        assert r.status_code == 200
        assert r.json()["task_count"] == 0
