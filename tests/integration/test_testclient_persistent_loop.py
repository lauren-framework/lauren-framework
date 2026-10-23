"""One persistent event loop per TestClient: loop-bound startup resources keep working."""

from __future__ import annotations

import asyncio

from lauren import (
    BackgroundTasks,
    Lauren,
    LaurenFactory,
    controller,
    get,
    injectable,
    module,
    post,
    post_construct,
    pre_destruct,
)
from lauren.testing import TestClient


@injectable()
class Ticker:
    """The interval-job pattern: a task spawned in ``@post_construct``."""

    def __init__(self) -> None:
        self.ticks = 0
        self.task: asyncio.Task[None] | None = None
        self.ready: asyncio.Event | None = None
        self.queue: asyncio.Queue[str] | None = None

    @post_construct
    async def start(self) -> None:
        self.ready = asyncio.Event()
        self.queue = asyncio.Queue()

        async def run() -> None:
            while True:
                self.ticks += 1
                self.ready.set()  # type: ignore[union-attr]
                await asyncio.sleep(0.005)

        self.task = asyncio.get_running_loop().create_task(run(), name="ticker")

    @pre_destruct
    async def stop(self) -> None:
        if self.task is not None:
            self.task.cancel()


@controller("/")
class TickerController:
    def __init__(self, ticker: Ticker) -> None:
        self.ticker = ticker

    @get("/state")
    async def state(self) -> dict:
        await self.ticker.ready.wait()  # type: ignore[union-attr]
        task = self.ticker.task
        assert task is not None
        return {"running": not task.done(), "ticks": self.ticker.ticks}

    @post("/enqueue")
    async def enqueue(self, tasks: BackgroundTasks) -> dict:
        async def push() -> None:
            await self.ticker.queue.put("job")  # type: ignore[union-attr]

        tasks.add_task(push)
        return {"queued": True}

    @get("/queue")
    async def queue(self) -> dict:
        return {"size": self.ticker.queue.qsize()}  # type: ignore[union-attr]


@module(controllers=[TickerController], providers=[Ticker])
class TickerModule:
    pass


def test_task_spawned_at_startup_is_still_running_at_the_third_request() -> None:
    with TestClient(LaurenFactory.create(TickerModule)) as client:
        states = [client.get("/state").json() for _ in range(3)]
    assert all(s["running"] for s in states)
    assert states[2]["ticks"] >= states[0]["ticks"]


def test_event_created_at_startup_is_awaitable_in_later_requests() -> None:
    with TestClient(LaurenFactory.create(TickerModule)) as client:
        assert client.get("/state").status_code == 200
        assert client.get("/state").status_code == 200


def test_bare_client_also_keeps_one_loop() -> None:
    client = TestClient(LaurenFactory.create(TickerModule))
    assert [client.get("/state").json()["running"] for _ in range(3)] == [True, True, True]
    client.close()


def test_background_tasks_finish_before_request_returns() -> None:
    with TestClient(LaurenFactory.create(TickerModule)) as client:
        client.post("/enqueue")
        assert client.get("/queue").json() == {"size": 1}


def test_lauren_app_routes_added_after_client_creation_still_count() -> None:
    app = Lauren(openapi_url=None, docs_url=None, redoc_url=None)
    client = TestClient(app)

    @app.get("/late")
    async def late() -> dict:
        return {"late": True}

    assert client.get("/late").json() == {"late": True}
    client.close()


def test_resolve_returns_the_live_singleton() -> None:
    with TestClient(LaurenFactory.create(TickerModule)) as client:
        ticker = client.resolve(Ticker)
        client.get("/state")
        assert ticker.task is not None and not ticker.task.done()


async def test_arequest_runs_on_the_client_loop_without_blocking_the_caller() -> None:
    client = TestClient(LaurenFactory.create(TickerModule))
    responses = await asyncio.gather(*(client.arequest("GET", "/state") for _ in range(4)))
    client.close()
    assert [r.json()["running"] for r in responses] == [True] * 4
