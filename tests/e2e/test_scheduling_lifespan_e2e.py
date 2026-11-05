"""End-to-end: the real ASGI lifespan protocol with a hosted service and a scheduled job.

The app is started with ``lifespan.startup``, serves HTTP while a hosted
service and an interval job update shared state (driven by a ``ManualClock``),
then stops with ``lifespan.shutdown``. The test asserts what HTTP sees, the
shutdown order, and that no task is left pending or destroyed.
"""

from __future__ import annotations

import asyncio
import gc
import json
import warnings
from typing import Any

from lauren import LaurenFactory, controller, get, injectable, module, pre_destruct
from lauren.scheduling import ManualClock, ScheduleModule, ServiceContext, hosted_service, interval


async def _http_get(app: Any, path: str) -> tuple[int, Any]:
    sent: list[dict[str, Any]] = []
    request_sent = False

    async def receive() -> dict[str, Any]:
        nonlocal request_sent
        if not request_sent:
            request_sent = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await asyncio.Event().wait()  # pragma: no cover - never asked twice
        return {}  # pragma: no cover

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": [],
        "client": ("127.0.0.1", 1),
        "server": ("testserver", 80),
    }
    await app(scope, receive, send)
    status = next(m["status"] for m in sent if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return status, json.loads(body)


async def test_lifespan_serves_requests_while_background_work_runs_and_shuts_down_cleanly() -> None:
    loop = asyncio.get_running_loop()
    loop_errors: list[dict[str, Any]] = []
    previous_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: loop_errors.append(context))
    clock = ManualClock()
    log: list[str] = []

    @injectable()
    class Counter:
        def __init__(self) -> None:
            self.ticks = 0
            self.snapshots: list[int] = []

        @pre_destruct
        def close(self) -> None:
            log.append("counter.pre_destruct")

    @hosted_service()
    class Ticker:
        def __init__(self, counter: Counter) -> None:
            self.counter = counter

        async def run(self, ctx: ServiceContext) -> None:
            while await ctx.sleep(1.0):
                self.counter.ticks += 1
            log.append("ticker.stopped")

    @injectable()
    class Snapshots:
        def __init__(self, counter: Counter) -> None:
            self.counter = counter

        @interval(seconds=5, initial_delay=5.5)  # between ticks: deterministic snapshots
        async def snapshot(self) -> None:
            self.counter.snapshots.append(self.counter.ticks)

    @controller("/counter")
    class CounterController:
        def __init__(self, counter: Counter) -> None:
            self.counter = counter

        @get("/")
        async def read(self) -> dict:
            return {"ticks": self.counter.ticks, "snapshots": self.counter.snapshots}

    @module(
        imports=[ScheduleModule.for_root(clock=clock)],
        controllers=[CounterController],
        providers=[Counter, Ticker, Snapshots],
    )
    class AppModule: ...

    app = LaurenFactory.create(AppModule)
    inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    outbox: list[str] = []

    async def send(message: dict[str, Any]) -> None:
        outbox.append(message["type"])

    with warnings.catch_warnings():
        warnings.simplefilter("error", RuntimeWarning)  # "coroutine ... was never awaited"
        lifespan = asyncio.create_task(app({"type": "lifespan", "asgi": {"version": "3.0"}}, inbox.get, send))
        await inbox.put({"type": "lifespan.startup"})
        while "lifespan.startup.complete" not in outbox:
            await asyncio.sleep(0)

        await clock.advance(3)
        early = await _http_get(app, "/counter/")
        await clock.advance(9)
        later = await _http_get(app, "/counter/")

        await inbox.put({"type": "lifespan.shutdown"})
        await lifespan
        gc.collect()

    loop.set_exception_handler(previous_handler)
    assert early == (200, {"ticks": 3, "snapshots": []})
    assert later == (200, {"ticks": 12, "snapshots": [5, 10]})
    assert outbox == ["lifespan.startup.complete", "lifespan.shutdown.complete"]
    assert log == ["ticker.stopped", "counter.pre_destruct"]
    assert loop_errors == []
    leftovers = [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]
    assert not [
        t for t in leftovers if t.get_name().startswith(("lauren-hosted", "lauren-job", "lauren-scheduler"))
    ]
