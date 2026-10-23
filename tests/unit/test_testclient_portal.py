"""The TestClient's portal loop: one thread per client, cleaned up on close and on GC."""

from __future__ import annotations

import asyncio
import gc
import threading
import warnings

import pytest

from lauren import LaurenFactory, controller, get, module
from lauren.testing import TestClient
from lauren.testing._portal import Portal


@controller("/")
class PingController:
    @get("/ping")
    async def ping(self) -> dict:
        return {"thread": threading.current_thread().name}


@module(controllers=[PingController])
class PingModule:
    pass


def _app():
    return LaurenFactory.create(PingModule)


def _portal_threads() -> set[threading.Thread]:
    return {t for t in threading.enumerate() if t.name.startswith("lauren-testclient-")}


def test_sync_calls_run_the_loop_in_the_calling_thread() -> None:
    with TestClient(_app()) as client:
        first = client.get("/ping").json()["thread"]
        second = client.get("/ping").json()["thread"]
    assert first == second == threading.current_thread().name


def test_calls_from_inside_a_running_loop_are_served_by_the_portal_thread() -> None:
    client = TestClient(_app())

    async def inside() -> str:
        return client.get("/ping").json()["thread"]

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        name = asyncio.run(inside())
    client.close()
    assert name.startswith("lauren-testclient-")


def test_calls_from_worker_threads_share_the_same_loop() -> None:
    import concurrent.futures

    with TestClient(_app()) as client:
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            names = list(pool.map(lambda _: client.get("/ping").json()["thread"], range(8)))
    assert all(name.startswith("ThreadPoolExecutor") for name in names)


def test_handlers_see_the_calling_threads_context_variables() -> None:
    import contextvars

    from lauren import Lauren

    marker: contextvars.ContextVar[str] = contextvars.ContextVar("marker", default="unset")
    app = Lauren(openapi_url=None, docs_url=None, redoc_url=None)

    @app.get("/marker")
    async def read_marker() -> dict:
        return {"marker": marker.get()}

    token = marker.set("from-the-test")
    try:
        with TestClient(app) as client:
            assert client.get("/marker").json() == {"marker": "from-the-test"}
    finally:
        marker.reset(token)


def test_background_tasks_progress_between_calls() -> None:
    from lauren import injectable, post_construct, pre_destruct

    progressed = threading.Event()

    @injectable()
    class Worker:
        @post_construct
        async def start(self) -> None:
            async def run() -> None:
                for _ in range(3):
                    await asyncio.sleep(0)
                progressed.set()
                await asyncio.Event().wait()

            self.task = asyncio.get_running_loop().create_task(run())

        @pre_destruct
        async def stop(self) -> None:
            self.task.cancel()

    @module(controllers=[PingController], providers=[Worker])
    class WorkerModule:
        pass

    with TestClient(LaurenFactory.create(WorkerModule)):
        # No client call is in progress: only the idle thread can run the task.
        assert progressed.wait(timeout=30)


def test_portal_thread_is_a_daemon() -> None:
    with TestClient(_app()) as client:
        assert client._driver.portal.thread.daemon is True


def test_close_stops_the_thread_and_closes_the_loop() -> None:
    client = TestClient(_app())
    portal = client._driver.portal
    client.close()
    assert not portal.thread.is_alive()
    assert portal.loop.is_closed()


def test_garbage_collected_client_stops_its_portal() -> None:
    before = _portal_threads()
    client = TestClient(_app())
    client.get("/ping")
    portal = client._driver.portal
    del client
    gc.collect()
    assert not portal.thread.is_alive()
    assert portal.loop.is_closed()
    assert _portal_threads() <= before


def test_many_unclosed_clients_do_not_accumulate_threads() -> None:
    before = _portal_threads()
    for _ in range(5):
        TestClient(_app()).get("/ping")
    gc.collect()
    assert _portal_threads() <= before


def test_close_is_idempotent() -> None:
    client = TestClient(_app())
    client.close()
    client.close()


def test_closed_client_refuses_requests() -> None:
    client = TestClient(_app())
    client.close()
    with pytest.raises(RuntimeError, match="closed"):
        client.get("/ping")


def test_portal_rejects_calls_from_its_own_thread() -> None:
    portal = Portal(name="lauren-testclient-selfcall")
    try:

        async def reenter() -> None:
            portal.call(lambda: asyncio.sleep(0))

        with pytest.raises(RuntimeError, match="deadlock"):
            portal.call(reenter)
    finally:
        portal.stop()


def test_portal_stop_reports_and_cancels_leftover_tasks() -> None:
    portal = Portal(name="lauren-testclient-leftovers")

    async def spawn() -> None:
        asyncio.get_running_loop().create_task(asyncio.sleep(3600), name="forever")

    portal.call(spawn)
    assert portal.stop() == ["forever"]
    assert portal.stop() == []


def test_portal_surfaces_loop_factory_errors() -> None:
    def broken() -> asyncio.AbstractEventLoop:
        raise OSError("no loop for you")

    with pytest.raises(OSError, match="no loop"):
        Portal(name="lauren-testclient-broken", loop_factory=broken)


def test_custom_loop_factory_is_used() -> None:
    created: list[asyncio.AbstractEventLoop] = []

    def factory() -> asyncio.AbstractEventLoop:
        loop = asyncio.new_event_loop()
        created.append(loop)
        return loop

    with TestClient(_app(), backend_options={"loop_factory": factory}) as client:
        assert client._driver.portal.loop is created[0]


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [({"backend": "trio"}, "asyncio"), ({"backend_options": {"nope": 1}}, "unknown backend_options")],
)
def test_unsupported_backend_settings_raise(kwargs, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        TestClient(_app(), **kwargs)


def test_close_warns_about_tasks_the_app_left_running() -> None:
    from lauren import injectable, post_construct

    @injectable()
    class Spawner:
        @post_construct
        async def start(self) -> None:
            asyncio.get_running_loop().create_task(asyncio.sleep(3600), name="orphan-loop")

    @module(controllers=[PingController], providers=[Spawner])
    class SpawnModule:
        pass

    client = TestClient(LaurenFactory.create(SpawnModule))
    with pytest.warns(RuntimeWarning, match="orphan-loop"):
        client.close()


def test_sync_call_inside_a_running_loop_warns_once(monkeypatch) -> None:
    from lauren.testing import _client

    monkeypatch.setitem(_client._loop_warning, "issued", False)
    client = TestClient(_app())

    async def inside() -> list[warnings.WarningMessage]:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            client.get("/ping")
            client.get("/ping")
        return [w for w in caught if issubclass(w.category, RuntimeWarning)]

    caught = asyncio.run(inside())
    client.close()
    assert len(caught) == 1
    assert "AsyncTestClient" in str(caught[0].message)
