"""``with TestClient(app)``: startup on enter, full shutdown on exit, one driver per app."""

from __future__ import annotations

from typing import Any

import pytest

from lauren import Lauren, LaurenFactory, controller, get, injectable, module, post_construct, pre_destruct
from lauren.exceptions import LifecycleViolationError
from lauren.testing import TestClient


def _build(events: list[str], *, fail_startup: bool = False) -> Any:
    @injectable()
    def connection():
        events.append("generator:setup")
        yield "conn"
        events.append("generator:teardown")

    @injectable()
    class Database:
        @post_construct
        async def open(self) -> None:
            events.append("post_construct")
            if fail_startup:
                raise ConnectionRefusedError("db down")

        @pre_destruct
        async def close(self) -> None:
            events.append("pre_destruct")

    @controller("/")
    class Ping:
        def __init__(self, db: Database) -> None:
            self.db = db

        @get("/ping")
        async def ping(self) -> dict:
            return {"ok": True}

    @module(controllers=[Ping], providers=[Database, connection])
    class AppModule:
        pass

    app = LaurenFactory.create(AppModule)

    @app.on_shutdown
    def on_shutdown() -> None:
        events.append("on_shutdown")

    return app


def test_enter_runs_startup_and_exit_runs_the_full_shutdown() -> None:
    events: list[str] = []
    with TestClient(_build(events)) as client:
        assert "post_construct" in events
        assert client.get("/ping").status_code == 200
        assert "pre_destruct" not in events
    assert events[-3:] == ["on_shutdown", "pre_destruct", "generator:teardown"]


def test_startup_failure_surfaces_the_original_exception() -> None:
    with pytest.raises(ConnectionRefusedError, match="db down"):
        with TestClient(_build([], fail_startup=True)):
            pass  # pragma: no cover - never reached


def test_bare_client_startup_failure_surfaces_the_original_exception() -> None:
    with pytest.raises(ConnectionRefusedError):
        TestClient(_build([], fail_startup=True))


def test_bare_client_starts_on_construction_like_1x() -> None:
    events: list[str] = []
    client = TestClient(_build(events))
    assert "post_construct" in events
    client.close()
    assert "pre_destruct" in events


def test_bare_client_that_is_never_closed_runs_no_shutdown_hooks() -> None:
    import gc

    events: list[str] = []
    TestClient(_build(events)).get("/ping")
    gc.collect()
    assert "pre_destruct" not in events


def test_lifespan_off_neither_starts_nor_stops() -> None:
    events: list[str] = []
    app = _build(events)
    with TestClient(app, lifespan="off"):
        pass
    assert events == []


def test_second_lifespan_managed_client_for_a_driven_app_raises() -> None:
    app = _build([])
    with TestClient(app):
        with pytest.raises(RuntimeError, match="new_session"):
            with TestClient(app):
                pass  # pragma: no cover


def test_second_bare_client_attaches_to_the_running_loop() -> None:
    app = _build([])
    first = TestClient(app)
    second = TestClient(app)
    assert second._driver is first._driver
    assert second.get("/ping").status_code == 200
    first.close()
    with pytest.raises(RuntimeError, match="closed"):
        second.get("/ping")


def test_entering_a_shut_down_app_is_refused_with_guidance() -> None:
    app = _build([])
    with TestClient(app):
        pass
    with pytest.raises(RuntimeError, match="fresh app") as info:
        with TestClient(app):
            pass  # pragma: no cover
    assert isinstance(info.value.__cause__, LifecycleViolationError)


def test_new_session_shares_the_lifecycle_and_has_its_own_jar() -> None:
    events: list[str] = []
    with TestClient(_build(events)) as client:
        client.cookies.set("who", "first")
        other = client.new_session(headers={"x-user": "2"})
        with other:
            assert other.get("/ping").status_code == 200
            assert "who" not in other.cookies
            assert other.headers == {"x-user": "2"}
        assert "pre_destruct" not in events  # closing the session does not shut down
        assert client.get("/ping").status_code == 200
    assert "pre_destruct" in events


def test_lauren_app_lifespan() -> None:
    events: list[str] = []
    app = Lauren(openapi_url=None, docs_url=None, redoc_url=None)

    @app.get("/x")
    async def handler() -> dict:
        return {}

    app.on_shutdown(lambda: events.append("shutdown"))
    with TestClient(app) as client:
        assert client.get("/x").status_code == 200
        assert app._compiled._started
    assert events == ["shutdown"]


# -- foreign ASGI apps -----------------------------------------------------------


class LifespanApp:
    def __init__(self, *, fail: bool = False) -> None:
        self.events: list[str] = []
        self.fail = fail

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] == "lifespan":
            while True:
                message = await receive()
                if message["type"] == "lifespan.startup":
                    self.events.append("startup")
                    scope["state"]["greeting"] = "hi"
                    if self.fail:
                        await send({"type": "lifespan.startup.failed", "message": "nope"})
                        return
                    await send({"type": "lifespan.startup.complete"})
                else:
                    self.events.append("shutdown")
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        body = scope.get("state", {}).get("greeting", "none").encode()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": body})


def test_foreign_app_gets_lifespan_and_state_in_with_mode() -> None:
    app = LifespanApp()
    with TestClient(app) as client:
        assert client.get("/").text == "hi"
    assert app.events == ["startup", "shutdown"]


def test_foreign_app_gets_no_lifespan_without_with_in_auto_mode() -> None:
    app = LifespanApp()
    client = TestClient(app)
    assert client.get("/").text == "none"
    client.close()
    assert app.events == []


def test_foreign_app_lifespan_on_starts_at_construction() -> None:
    app = LifespanApp()
    client = TestClient(app, lifespan="on")
    assert app.events == ["startup"]
    client.close()
    assert app.events == ["startup", "shutdown"]


def test_foreign_lifespan_startup_failure_raises() -> None:
    with pytest.raises(RuntimeError, match="nope"):
        with TestClient(LifespanApp(fail=True)):
            pass  # pragma: no cover


async def _http_only(scope: dict, receive: Any, send: Any) -> None:
    assert scope["type"] == "http"
    await send({"type": "http.response.start", "status": 204, "headers": []})
    await send({"type": "http.response.body", "body": b""})


def test_foreign_app_without_lifespan_support_is_tolerated_in_auto_mode() -> None:
    with TestClient(_http_only) as client:
        assert client.get("/").status_code == 204


def test_foreign_app_without_lifespan_support_fails_in_on_mode() -> None:
    with pytest.raises(RuntimeError, match="does not support"):
        TestClient(_http_only, lifespan="on")


def test_invalid_lifespan_mode_is_rejected() -> None:
    with pytest.raises(ValueError, match="lifespan"):
        TestClient(_http_only, lifespan="sometimes")  # type: ignore[arg-type]
