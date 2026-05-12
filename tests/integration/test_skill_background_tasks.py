"""Integration tests for the Background Task Scheduler skill (Skill 36).

Tests verify that tasks submitted via BackgroundTasks run after the response
and that job results are observable via the singleton JobService.
"""

from __future__ import annotations

from pydantic import BaseModel

from lauren import (
    BackgroundTasks,
    Json,
    LaurenFactory,
    Scope,
    controller,
    get,
    injectable,
    module,
    post,
)
from lauren.testing import TestClient


# ---------------------------------------------------------------------------
# Domain
# ---------------------------------------------------------------------------


class JobRequest(BaseModel):
    user_id: str
    job_type: str


@injectable(scope=Scope.SINGLETON)
class JobService:
    def __init__(self) -> None:
        self.completed_jobs: list[str] = []

    async def process_job(self, user_id: str, job_type: str) -> None:
        self.completed_jobs.append(f"{job_type}:{user_id}")

    async def process_multiple(self, items: list[str]) -> None:
        self.completed_jobs.extend(items)


# ---------------------------------------------------------------------------
# Controller & Module
# ---------------------------------------------------------------------------


@controller("/jobs")
class JobController:
    def __init__(self, svc: JobService) -> None:
        self._svc = svc

    @post("/submit")
    async def submit(self, body: Json[JobRequest], tasks: BackgroundTasks) -> dict:
        tasks.add_task(self._svc.process_job, body.user_id, body.job_type)
        return {"status": "queued", "user_id": body.user_id}

    @post("/submit-batch")
    async def submit_batch(self, body: Json[list[JobRequest]], tasks: BackgroundTasks) -> dict:
        for req in body:
            # body items may be dicts or Pydantic model instances
            user_id = req.user_id if hasattr(req, "user_id") else req["user_id"]
            job_type = req.job_type if hasattr(req, "job_type") else req["job_type"]
            tasks.add_task(self._svc.process_job, user_id, job_type)
        return {"status": "queued", "count": len(body)}

    @get("/results")
    async def results(self) -> dict:
        return {"completed": self._svc.completed_jobs}


@module(controllers=[JobController], providers=[JobService])
class JobModule:
    pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def build_app() -> TestClient:
    return TestClient(LaurenFactory.create(JobModule))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestBackgroundTaskScheduler:
    def test_submit_returns_queued(self) -> None:
        client = build_app()
        r = client.post("/jobs/submit", json={"user_id": "u1", "job_type": "email"})
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "queued"
        assert body["user_id"] == "u1"

    def test_job_runs_after_response(self) -> None:
        client = build_app()
        client.post("/jobs/submit", json={"user_id": "u1", "job_type": "email"})
        # BackgroundTasks run synchronously in TestClient before the next call
        r = client.get("/jobs/results")
        assert r.status_code == 200
        assert "email:u1" in r.json()["completed"]

    def test_multiple_jobs_all_run(self) -> None:
        client = build_app()
        client.post("/jobs/submit", json={"user_id": "u1", "job_type": "email"})
        client.post("/jobs/submit", json={"user_id": "u2", "job_type": "sms"})
        client.post("/jobs/submit", json={"user_id": "u3", "job_type": "push"})
        r = client.get("/jobs/results")
        completed = r.json()["completed"]
        assert "email:u1" in completed
        assert "sms:u2" in completed
        assert "push:u3" in completed

    def test_batch_submit_all_queued(self) -> None:
        client = build_app()
        batch = [
            {"user_id": "a", "job_type": "report"},
            {"user_id": "b", "job_type": "export"},
        ]
        r = client.post("/jobs/submit-batch", json=batch)
        assert r.status_code == 200
        assert r.json()["count"] == 2
        r2 = client.get("/jobs/results")
        completed = r2.json()["completed"]
        assert "report:a" in completed
        assert "export:b" in completed

    def test_no_tasks_no_side_effects(self) -> None:
        client = build_app()
        r = client.get("/jobs/results")
        assert r.status_code == 200
        assert r.json()["completed"] == []

    def test_job_service_singleton_accumulates(self) -> None:
        client = build_app()
        client.post("/jobs/submit", json={"user_id": "x", "job_type": "t1"})
        client.post("/jobs/submit", json={"user_id": "x", "job_type": "t2"})
        r = client.get("/jobs/results")
        completed = r.json()["completed"]
        assert completed.count("t1:x") == 1
        assert completed.count("t2:x") == 1
