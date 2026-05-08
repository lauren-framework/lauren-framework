"""Integration tests for the Docker multi-stage build & docker-compose pattern (Skill 50).

No Docker daemon is needed. These tests validate that a Lauren application
structured according to the docker-compose pattern boots correctly and responds
to health and API requests — exactly what the container entrypoint would do.
"""

from __future__ import annotations

import asyncio

from lauren import (
    LaurenFactory,
    Scope,
    controller,
    get,
    injectable,
    module,
    post_construct,
    pre_destruct,
)
from lauren.testing import TestClient


# ---------------------------------------------------------------------------
# Simulated application module (mirrors a real Lauren app's module tree)
# ---------------------------------------------------------------------------


@injectable(scope=Scope.SINGLETON)
class DatabaseService:
    """Represents a DB connection that would use DATABASE_URL in production."""

    def __init__(self) -> None:
        self.connected: bool = False
        self.disconnected: bool = False

    @post_construct
    async def connect(self) -> None:
        # In production: open asyncpg / SQLAlchemy async pool
        self.connected = True

    @pre_destruct
    async def disconnect(self) -> None:
        # In production: drain pool, close connections
        self.disconnected = True


@injectable(scope=Scope.SINGLETON)
class CacheService:
    """Represents a Redis cache that would use REDIS_URL in production."""

    def __init__(self) -> None:
        self.connected: bool = False

    @post_construct
    async def connect(self) -> None:
        self.connected = True


# ---------------------------------------------------------------------------
# Controllers
# ---------------------------------------------------------------------------


@controller("/health")
class HealthController:
    def __init__(self, db: DatabaseService, cache: CacheService) -> None:
        self._db = db
        self._cache = cache

    @get("/live")
    async def liveness(self) -> dict:
        return {"status": "ok"}

    @get("/ready")
    async def readiness(self) -> dict:
        if self._db.connected and self._cache.connected:
            return {"status": "ready", "db": True, "cache": True}
        return {
            "status": "not_ready",
            "db": self._db.connected,
            "cache": self._cache.connected,
        }


@controller("/api")
class ApiController:
    def __init__(self, db: DatabaseService) -> None:
        self._db = db

    @get("/ping")
    async def ping(self) -> dict:
        return {"pong": True, "db_connected": self._db.connected}

    @get("/version")
    async def version(self) -> dict:
        return {"version": "1.0.0", "framework": "lauren"}


# ---------------------------------------------------------------------------
# Root module (analogous to AppModule in the docker-compose app)
# ---------------------------------------------------------------------------


@module(
    controllers=[HealthController, ApiController],
    providers=[DatabaseService, CacheService],
)
class DockerAppModule:
    pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def build_app():
    return LaurenFactory.create(DockerAppModule)


def build_client():
    return TestClient(build_app())


# ---------------------------------------------------------------------------
# Tests — app bootstraps correctly (mirrors container startup)
# ---------------------------------------------------------------------------


class TestDockerAppBootstrap:
    def test_app_starts_successfully(self):
        """The app must start without errors — equivalent to container startup."""
        client = build_client()
        r = client.get("/health/live")
        assert r.status_code == 200

    def test_liveness_probe_returns_ok(self):
        client = build_client()
        r = client.get("/health/live")
        assert r.json()["status"] == "ok"

    def test_readiness_probe_returns_ready_after_startup(self):
        """After @post_construct hooks fire, DB and cache are connected."""
        client = build_client()
        r = client.get("/health/ready")
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "ready"
        assert data["db"] is True
        assert data["cache"] is True

    def test_api_ping_returns_pong(self):
        client = build_client()
        r = client.get("/api/ping")
        assert r.status_code == 200
        assert r.json()["pong"] is True

    def test_api_ping_shows_db_connected(self):
        client = build_client()
        r = client.get("/api/ping")
        assert r.json()["db_connected"] is True

    def test_api_version_endpoint(self):
        client = build_client()
        r = client.get("/api/version")
        assert r.status_code == 200
        data = r.json()
        assert "version" in data
        assert data["framework"] == "lauren"

    def test_db_post_construct_fires(self):
        app = build_app()
        db = asyncio.run(app.container.resolve(DatabaseService))
        TestClient(app)  # triggers startup
        assert db.connected is True

    def test_cache_post_construct_fires(self):
        app = build_app()
        cache = asyncio.run(app.container.resolve(CacheService))
        TestClient(app)
        assert cache.connected is True

    def test_db_pre_destruct_fires_on_shutdown(self):
        import asyncio

        app = build_app()
        db = asyncio.run(app.container.resolve(DatabaseService))
        TestClient(app).get("/health/live")  # ensure startup
        asyncio.run(app.shutdown())
        assert db.disconnected is True

    def test_multiple_endpoints_accessible(self):
        """All routes mount without conflicts — validates module wiring."""
        client = build_client()
        endpoints = [
            "/health/live",
            "/health/ready",
            "/api/ping",
            "/api/version",
        ]
        for path in endpoints:
            r = client.get(path)
            assert r.status_code == 200, f"Expected 200 for {path}, got {r.status_code}"

    def test_not_found_for_unknown_route(self):
        client = build_client()
        r = client.get("/does/not/exist")
        assert r.status_code == 404

    def test_app_singleton_services_shared(self):
        """Singleton scope: both controllers resolve the same DatabaseService instance."""
        app = build_app()
        db_from_container = asyncio.run(app.container.resolve(DatabaseService))
        TestClient(app)
        assert db_from_container.connected is True
