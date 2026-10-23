"""Readiness across the whole lifetime: ASGI lifespan, pre-stop draining and SIGTERM."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
from typing import Any

import pytest

from lauren import (
    AppLifecycleState,
    LaurenFactory,
    Scope,
    SignalBus,
    controller,
    get,
    injectable,
    module,
    post_construct,
)
from lauren.health import HealthModule, health_check
from lauren.signals import ShutdownBegin, install_signal_handlers


@injectable(scope=Scope.SINGLETON)
class Database:
    def __init__(self) -> None:
        self.connected = False

    @post_construct
    async def connect(self) -> None:
        self.connected = True

    @health_check("database", timeout=1.0)
    async def ping(self) -> bool:
        return self.connected


@controller("/orders")
class Orders:
    @get("/")
    async def index(self) -> list[int]:
        return [1, 2, 3]


@module(imports=[HealthModule.for_root(details="always")], providers=[Database], controllers=[Orders])
class AppModule:
    pass


async def get_status(app: Any, path: str) -> tuple[int, Any]:
    """One GET through the ASGI callable."""
    status = 0
    body = bytearray()
    received = False

    async def receive() -> dict[str, Any]:
        nonlocal received
        if received:
            return {"type": "http.disconnect"}
        received = True
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        nonlocal status
        if message["type"] == "http.response.start":
            status = message["status"]
        elif message["type"] == "http.response.body":
            body.extend(message.get("body", b""))

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("utf-8"),
        "query_string": b"",
        "headers": [],
        "client": ("127.0.0.1", 50000),
        "server": ("testserver", 80),
    }
    await app(scope, receive, send)
    return status, json.loads(body) if body else None


class Lifespan:
    """Drive the ASGI lifespan protocol the way uvicorn does."""

    def __init__(self, app: Any) -> None:
        self._inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.sent: list[str] = []
        self._answered = asyncio.Event()
        self._task = asyncio.create_task(
            app({"type": "lifespan", "asgi": {"version": "3.0"}}, self._receive, self._send)
        )

    async def _receive(self) -> dict[str, Any]:
        return await self._inbox.get()

    async def _send(self, message: dict[str, Any]) -> None:
        self.sent.append(message["type"])
        self._answered.set()

    async def startup(self) -> None:
        self._answered.clear()
        await self._inbox.put({"type": "lifespan.startup"})
        await asyncio.wait_for(self._answered.wait(), timeout=5)

    async def shutdown(self) -> None:
        await self._inbox.put({"type": "lifespan.shutdown"})
        await asyncio.wait_for(self._task, timeout=5)


async def test_readiness_follows_the_lifespan_and_the_pre_stop_drain() -> None:
    app = LaurenFactory.create(AppModule, lazy_startup=False)  # probed before the lifespan starts it
    lifespan = Lifespan(app)
    before = await get_status(app, "/health/ready")

    await lifespan.startup()
    serving = [await get_status(app, "/health/ready"), await get_status(app, "/orders")]
    app.begin_draining(reason="preStop")
    draining = [await get_status(app, "/health/ready"), await get_status(app, "/orders")]
    await lifespan.shutdown()
    stopped = await get_status(app, "/health/live")

    assert before == (503, {"status": "down"})
    assert serving == [(200, {"status": "up"}), (200, [1, 2, 3])]
    assert draining == [(503, {"status": "down"}), (200, [1, 2, 3])]
    assert lifespan.sent == ["lifespan.startup.complete", "lifespan.shutdown.complete"]
    assert (app.lifecycle_state, stopped[0]) == (AppLifecycleState.STOPPED, 503)


async def test_a_failed_lifespan_startup_fails_every_probe() -> None:
    @injectable()
    class Broken:
        @post_construct
        async def boom(self) -> None:
            raise RuntimeError("no database")

    @module(imports=[HealthModule.for_root()], providers=[Broken])
    class BrokenModule:
        pass

    app = LaurenFactory.create(BrokenModule)
    lifespan = Lifespan(app)

    await lifespan.startup()
    probes = [
        (await get_status(app, path))[0] for path in ("/health/live", "/health/ready", "/health/startup")
    ]

    assert lifespan.sent == ["lifespan.startup.failed"]
    assert app.lifecycle_state is AppLifecycleState.FAILED
    assert probes == [503, 503, 503]


async def test_the_report_tracks_the_lifecycle_state() -> None:
    app = LaurenFactory.create(AppModule)
    lifespan = Lifespan(app)
    await lifespan.startup()

    ready = (await get_status(app, "/health"))[1]
    app.begin_draining()
    draining = (await get_status(app, "/health"))[1]
    await lifespan.shutdown()

    assert (ready["status"], ready["lifecycle"], ready["checks"]["database"]["status"]) == (
        "up",
        "ready",
        "up",
    )
    assert (draining["status"], draining["lifecycle"]) == ("down", "draining")


@pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX signal handlers are required")
def test_sigterm_flips_readiness_before_shutdown_begins() -> None:
    signals = SignalBus()
    shutdown_began: list[ShutdownBegin] = []
    signals.on(ShutdownBegin)(shutdown_began.append)
    app = LaurenFactory.create(AppModule, signals=signals)

    async def scenario() -> tuple[Any, ...]:
        await app.startup()
        received = install_signal_handlers(app, signals=(signal.SIGTERM,), shutdown_delay=0.2)
        asyncio.get_running_loop().call_later(0.01, os.kill, os.getpid(), signal.SIGTERM)
        await asyncio.wait_for(received.wait(), timeout=5)
        during_delay = (
            app.lifecycle_state,
            len(shutdown_began),
            await get_status(app, "/health/ready"),
            await get_status(app, "/orders"),
        )
        await asyncio.wait_for(app._shutdown_complete.wait(), timeout=5)
        return during_delay

    state, began, ready, orders = asyncio.run(scenario())

    assert (state, began) == (AppLifecycleState.DRAINING, 0)
    assert ready == (503, {"status": "down"})
    assert orders == (200, [1, 2, 3])
    assert (app.lifecycle_state, len(shutdown_began)) == (AppLifecycleState.STOPPED, 1)
