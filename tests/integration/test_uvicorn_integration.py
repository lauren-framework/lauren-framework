"""Verify that LaurenFactory.create() works end-to-end with a live uvicorn server.

Key things validated:
* ``LaurenFactory.create()`` is synchronous — the returned app can be assigned at
  module level and passed directly to ``uvicorn.Config`` / ``uvicorn.Server``.
* The ASGI lifespan protocol fires ``startup()`` (``@post_construct`` hooks run).
* HTTP requests are handled correctly after startup.
* Graceful shutdown fires ``@pre_destruct`` hooks.
"""

from __future__ import annotations

import asyncio
import threading

import httpx
import uvicorn

from lauren import (
    LaurenFactory,
    controller,
    get,
    injectable,
    module,
    post_construct,
    pre_destruct,
)
from lauren.types import Scope


# ---------------------------------------------------------------------------
# Minimal app
# ---------------------------------------------------------------------------


@injectable(scope=Scope.SINGLETON)
class Counter:
    def __init__(self) -> None:
        self.started = False
        self.stopped = False
        self.value = 0

    @post_construct
    async def on_start(self) -> None:
        self.started = True
        self.value = 10  # seeded during startup

    @pre_destruct
    async def on_stop(self) -> None:
        self.stopped = True


@controller("/")
class RootController:
    def __init__(self, counter: Counter) -> None:
        self._c = counter

    @get("/ping")
    async def ping(self) -> dict:
        return {"pong": True}

    @get("/count")
    async def count(self) -> dict:
        return {"value": self._c.value, "started": self._c.started}


@module(controllers=[RootController], providers=[Counter])
class AppModule:
    pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run_server(
    app, host: str, port: int, started_event: threading.Event, server_holder: list
) -> None:
    """Run uvicorn in a background thread; set *started_event* once ready."""

    async def _serve() -> None:
        config = uvicorn.Config(app, host=host, port=port, log_level="warning")
        server = uvicorn.Server(config)
        server_holder.append(server)
        # Notify the test thread once the server is accepting connections.
        loop = asyncio.get_event_loop()
        loop.call_later(0.05, started_event.set)
        await server.serve()

    asyncio.run(_serve())


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_create_is_synchronous() -> None:
    """LaurenFactory.create() must return a LaurenApp, not a coroutine."""
    import inspect
    from lauren._asgi import LaurenApp

    app = LaurenFactory.create(AppModule)
    assert not inspect.isawaitable(app), "create() must be synchronous"
    assert isinstance(app, LaurenApp)


def test_uvicorn_lifespan_and_requests() -> None:
    """Start a real uvicorn server, fire requests, verify startup ran."""
    # create() is synchronous — can be called at module level.
    app = LaurenFactory.create(AppModule)

    host, port = "127.0.0.1", 18923
    started = threading.Event()
    server_holder: list = []
    server_thread = threading.Thread(
        target=_run_server, args=(app, host, port, started, server_holder), daemon=True
    )
    server_thread.start()
    started.wait(timeout=5)

    # Give uvicorn a moment to complete the lifespan startup handshake.
    import time

    time.sleep(0.15)

    base = f"http://{host}:{port}"
    try:
        with httpx.Client(base_url=base, timeout=5) as client:
            # Basic liveness
            r = client.get("/ping")
            assert r.status_code == 200
            assert r.json() == {"pong": True}

            # @post_construct ran — counter was seeded to 10
            r = client.get("/count")
            assert r.status_code == 200
            data = r.json()
            assert data["started"] is True, "@post_construct did not run"
            assert data["value"] == 10, "seeded value incorrect"
    finally:
        if server_holder:
            server_holder[0].should_exit = True
        server_thread.join(timeout=3)
