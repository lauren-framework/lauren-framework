"""Integration tests for health check & readiness probe endpoints (Skill 42)."""

from __future__ import annotations

import asyncio

from lauren import LaurenFactory, Scope, controller, get, injectable, module
from lauren.testing import TestClient
from lauren.types import Response


# ---------------------------------------------------------------------------
# HealthService
# ---------------------------------------------------------------------------


@injectable(scope=Scope.SINGLETON)
class HealthService:
    def __init__(self) -> None:
        self._checks: dict = {}
        self._ready: bool = True

    def register_check(self, name: str, check_fn) -> None:
        self._checks[name] = check_fn

    def set_ready(self, ready: bool) -> None:
        self._ready = ready

    async def run_checks(self) -> dict:
        results: dict = {}
        for name, fn in self._checks.items():
            try:
                ok = await fn() if asyncio.iscoroutinefunction(fn) else fn()
                results[name] = {"status": "ok" if ok else "degraded"}
            except Exception as exc:
                results[name] = {"status": "error", "error": str(exc)}
        if results:
            overall = (
                "ok"
                if all(r["status"] == "ok" for r in results.values())
                else "degraded"
            )
        else:
            overall = "ok"
        return {"status": overall, "checks": results}

    def is_ready(self) -> bool:
        return self._ready


# ---------------------------------------------------------------------------
# HealthController
# ---------------------------------------------------------------------------


@controller("/health")
class HealthController:
    def __init__(self, health: HealthService) -> None:
        self._health = health

    @get("/")
    async def full_status(self) -> dict:
        return await self._health.run_checks()

    @get("/live")
    async def liveness(self) -> dict:
        return {"status": "ok"}

    @get("/ready")
    async def readiness(self) -> Response:
        if self._health.is_ready():
            return Response.json({"status": "ready"})
        return Response.json({"status": "not_ready"}).with_status(503)


@module(controllers=[HealthController], providers=[HealthService])
class HealthModule:
    pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def build_app():
    return LaurenFactory.create(HealthModule)


def build_client():
    return TestClient(build_app())


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestHealthChecks:
    def test_liveness_returns_200(self):
        client = build_client()
        r = client.get("/health/live")
        assert r.status_code == 200

    def test_liveness_returns_ok_status(self):
        client = build_client()
        r = client.get("/health/live")
        assert r.json()["status"] == "ok"

    def test_readiness_returns_200_when_ready(self):
        client = build_client()
        r = client.get("/health/ready")
        assert r.status_code == 200

    def test_readiness_returns_ready_status(self):
        client = build_client()
        r = client.get("/health/ready")
        assert r.json()["status"] == "ready"

    def test_readiness_returns_503_when_not_ready(self):
        app = build_app()
        svc = asyncio.run(app.container.resolve(HealthService))
        svc.set_ready(False)
        client = TestClient(app)
        r = client.get("/health/ready")
        assert r.status_code == 503

    def test_readiness_returns_not_ready_body_when_503(self):
        app = build_app()
        svc = asyncio.run(app.container.resolve(HealthService))
        svc.set_ready(False)
        client = TestClient(app)
        r = client.get("/health/ready")
        assert r.json()["status"] == "not_ready"

    def test_full_status_no_checks_returns_ok(self):
        client = build_client()
        r = client.get("/health/")
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "ok"
        assert data["checks"] == {}

    def test_full_status_with_passing_check(self):
        app = build_app()
        svc = asyncio.run(app.container.resolve(HealthService))
        svc.register_check("db", lambda: True)
        client = TestClient(app)
        r = client.get("/health/")
        data = r.json()
        assert data["status"] == "ok"
        assert data["checks"]["db"]["status"] == "ok"

    def test_full_status_with_failing_check(self):
        app = build_app()
        svc = asyncio.run(app.container.resolve(HealthService))
        svc.register_check("db", lambda: False)
        client = TestClient(app)
        r = client.get("/health/")
        data = r.json()
        assert data["status"] == "degraded"
        assert data["checks"]["db"]["status"] == "degraded"

    def test_full_status_with_error_check(self):
        app = build_app()
        svc = asyncio.run(app.container.resolve(HealthService))
        svc.register_check(
            "cache", lambda: (_ for _ in ()).throw(RuntimeError("timeout"))
        )
        client = TestClient(app)
        r = client.get("/health/")
        data = r.json()
        assert data["status"] == "degraded"
        assert data["checks"]["cache"]["status"] == "error"
        assert "timeout" in data["checks"]["cache"]["error"]

    def test_full_status_with_async_check(self):
        app = build_app()
        svc = asyncio.run(app.container.resolve(HealthService))

        async def async_db_check():
            return True

        svc.register_check("async_db", async_db_check)
        client = TestClient(app)
        r = client.get("/health/")
        data = r.json()
        assert data["checks"]["async_db"]["status"] == "ok"

    def test_multiple_checks_all_ok(self):
        app = build_app()
        svc = asyncio.run(app.container.resolve(HealthService))
        svc.register_check("db", lambda: True)
        svc.register_check("cache", lambda: True)
        client = TestClient(app)
        r = client.get("/health/")
        data = r.json()
        assert data["status"] == "ok"
        assert len(data["checks"]) == 2
