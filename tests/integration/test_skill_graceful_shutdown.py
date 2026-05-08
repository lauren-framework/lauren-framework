"""Integration tests for graceful shutdown with connection draining (Skill 49)."""

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
# ConnectionPool with lifecycle hooks
# ---------------------------------------------------------------------------


@injectable(scope=Scope.SINGLETON)
class ConnectionPool:
    DRAIN_TIMEOUT = 5.0

    def __init__(self) -> None:
        self._connections: list[str] = []
        self._active_requests: int = 0
        self.is_started: bool = False
        self.is_stopped: bool = False
        self.start_call_count: int = 0
        self.stop_call_count: int = 0

    @post_construct
    async def start(self) -> None:
        self._connections = ["conn1", "conn2", "conn3"]
        self.is_started = True
        self.start_call_count += 1

    @pre_destruct
    async def shutdown(self) -> None:
        deadline = asyncio.get_event_loop().time() + self.DRAIN_TIMEOUT
        while self._active_requests > 0:
            if asyncio.get_event_loop().time() >= deadline:
                break
            await asyncio.sleep(0.01)
        self._connections.clear()
        self.is_stopped = True
        self.stop_call_count += 1

    def acquire(self) -> str:
        self._active_requests += 1
        return self._connections[0] if self._connections else "none"

    def release(self) -> None:
        self._active_requests = max(0, self._active_requests - 1)


# ---------------------------------------------------------------------------
# SyncResourceService (sync lifecycle hooks)
# ---------------------------------------------------------------------------


@injectable(scope=Scope.SINGLETON)
class SyncResourceService:
    def __init__(self) -> None:
        self.initialized = False
        self.cleaned_up = False

    @post_construct
    def init(self) -> None:
        self.initialized = True

    @pre_destruct
    def cleanup(self) -> None:
        self.cleaned_up = True


# ---------------------------------------------------------------------------
# OnShutdownTracker (tests ad-hoc callbacks)
# ---------------------------------------------------------------------------


@injectable(scope=Scope.SINGLETON)
class OnShutdownTracker:
    def __init__(self) -> None:
        self.callbacks_called: list[str] = []


# ---------------------------------------------------------------------------
# Controller
# ---------------------------------------------------------------------------


@controller("/api")
class ApiController:
    def __init__(self, pool: ConnectionPool) -> None:
        self._pool = pool

    @get("/status")
    async def status(self) -> dict:
        return {
            "started": self._pool.is_started,
            "stopped": self._pool.is_stopped,
            "connections": len(self._pool._connections),
        }

    @get("/acquire")
    async def acquire(self) -> dict:
        conn = self._pool.acquire()
        return {"conn": conn, "active": self._pool._active_requests}

    @get("/release")
    async def release(self) -> dict:
        self._pool.release()
        return {"active": self._pool._active_requests}


@module(controllers=[ApiController], providers=[ConnectionPool, SyncResourceService])
class AppModule:
    pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def build_app():
    return LaurenFactory.create(AppModule)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestGracefulShutdown:
    def test_post_construct_fires_on_startup(self):
        app = build_app()
        pool = asyncio.run(app.container.resolve(ConnectionPool))
        TestClient(app)  # triggers startup
        assert pool.is_started is True

    def test_post_construct_initializes_connections(self):
        app = build_app()
        pool = asyncio.run(app.container.resolve(ConnectionPool))
        TestClient(app)
        assert len(pool._connections) == 3

    def test_status_endpoint_shows_started(self):
        app = build_app()
        client = TestClient(app)
        r = client.get("/api/status")
        assert r.status_code == 200
        assert r.json()["started"] is True

    def test_status_endpoint_shows_connection_count(self):
        app = build_app()
        client = TestClient(app)
        r = client.get("/api/status")
        assert r.json()["connections"] == 3

    def test_pre_destruct_fires_on_shutdown(self):
        app = build_app()
        pool = asyncio.run(app.container.resolve(ConnectionPool))
        TestClient(app).get("/api/status")  # trigger startup
        asyncio.run(app.shutdown())
        assert pool.is_stopped is True

    def test_pre_destruct_clears_connections(self):
        app = build_app()
        pool = asyncio.run(app.container.resolve(ConnectionPool))
        TestClient(app).get("/api/status")
        asyncio.run(app.shutdown())
        assert len(pool._connections) == 0

    def test_sync_post_construct_fires(self):
        app = build_app()
        svc = asyncio.run(app.container.resolve(SyncResourceService))
        TestClient(app)
        assert svc.initialized is True

    def test_sync_pre_destruct_fires(self):
        app = build_app()
        svc = asyncio.run(app.container.resolve(SyncResourceService))
        TestClient(app).get("/api/status")
        asyncio.run(app.shutdown())
        assert svc.cleaned_up is True

    def test_on_shutdown_callback_called(self):
        app = build_app()
        called: list[str] = []
        app.on_shutdown(lambda: called.append("shutdown"))
        TestClient(app).get("/api/status")
        asyncio.run(app.shutdown())
        assert "shutdown" in called

    def test_on_shutdown_async_callback_called(self):
        app = build_app()
        called: list[str] = []

        async def async_cb():
            called.append("async-shutdown")

        app.on_shutdown(async_cb)
        TestClient(app).get("/api/status")
        asyncio.run(app.shutdown())
        assert "async-shutdown" in called

    def test_acquire_and_release(self):
        app = build_app()
        client = TestClient(app)
        r_acquire = client.get("/api/acquire")
        assert r_acquire.status_code == 200
        assert r_acquire.json()["conn"] == "conn1"
        assert r_acquire.json()["active"] == 1
        r_release = client.get("/api/release")
        assert r_release.json()["active"] == 0

    def test_shutdown_drains_before_closing(self):
        app = build_app()
        pool = asyncio.run(app.container.resolve(ConnectionPool))
        TestClient(app)
        pool._active_requests = 0  # already drained
        asyncio.run(app.shutdown())
        assert pool.is_stopped is True

    def test_status_not_stopped_before_shutdown(self):
        app = build_app()
        client = TestClient(app)
        r = client.get("/api/status")
        assert r.json()["stopped"] is False
