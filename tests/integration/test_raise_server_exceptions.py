"""``raise_server_exceptions=True`` and ``client.capture(...)``."""

from __future__ import annotations

import pytest

from lauren import (
    LaurenFactory,
    Request,
    RequestComplete,
    RequestReceived,
    controller,
    exception_handler,
    get,
    module,
    use_exception_handlers,
)
from lauren.exceptions import ForbiddenError
from lauren.testing import AsyncTestClient, TestClient


class TeapotError(Exception):
    pass


@exception_handler(TeapotError)
def teapot(exc: TeapotError, request: Request) -> tuple[dict, int]:
    return {"teapot": True}, 418


@exception_handler(KeyError)
def broken_handler(exc: KeyError, request: Request) -> dict:
    raise ValueError("the handler itself failed")


@controller("/")
@use_exception_handlers(teapot, broken_handler)
class BoomController:
    @get("/boom")
    async def boom(self) -> dict:
        raise RuntimeError("kaboom")

    @get("/forbidden")
    async def forbidden(self) -> dict:
        raise ForbiddenError("nope")

    @get("/teapot")
    async def brew(self) -> dict:
        raise TeapotError()

    @get("/handler-fails")
    async def handler_fails(self) -> dict:
        raise KeyError("k")

    @get("/ok")
    async def ok(self, request: Request) -> dict:
        return {"extensions": sorted(request.extensions)}


@module(controllers=[BoomController])
class BoomModule:
    pass


def _app():
    return LaurenFactory.create(BoomModule)


def test_unhandled_error_is_re_raised_with_its_traceback() -> None:
    with TestClient(_app(), raise_server_exceptions=True) as client:
        with pytest.raises(RuntimeError, match="kaboom") as info:
            client.get("/boom")
    assert info.traceback[-1].name == "boom"


def test_default_still_returns_the_500_envelope() -> None:
    with TestClient(_app()) as client:
        response = client.get("/boom")
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "internal_error"


@pytest.mark.parametrize(("path", "status"), [("/forbidden", 403), ("/teapot", 418), ("/nowhere", 404)])
def test_http_errors_and_handled_errors_are_not_raised(path: str, status: int) -> None:
    with TestClient(_app(), raise_server_exceptions=True) as client:
        assert client.get(path).status_code == status


def test_a_failing_exception_handler_raises_its_own_error() -> None:
    with TestClient(_app(), raise_server_exceptions=True) as client:
        with pytest.raises(ValueError, match="handler itself failed"):
            client.get("/handler-fails")


def test_the_marker_travels_as_a_scope_extension_not_a_header() -> None:
    with TestClient(_app(), raise_server_exceptions=True) as client:
        response = client.get("/ok")
    assert response.json() == {"extensions": ["lauren.testing"]}
    assert all(not k.lower().startswith("x-lauren") for k, _ in response.request.headers)


def test_listener_is_removed_on_close() -> None:
    app = _app()
    with TestClient(app, raise_server_exceptions=True) as client:
        client.get("/ok")
        assert app.signals.listener_count(RequestComplete) == 1
    assert app.signals.listener_count(RequestComplete) == 0


def test_capture_collects_the_named_events() -> None:
    with TestClient(_app()) as client:
        with client.capture(RequestReceived, RequestComplete) as events:
            client.get("/ok")
            client.get("/forbidden")
    assert [type(e) for e in events] == [RequestReceived, RequestComplete] * 2
    assert events[-1].status == 403


def test_capture_unsubscribes_on_exit() -> None:
    app = _app()
    with TestClient(app) as client:
        with client.capture(RequestComplete) as events:
            client.get("/ok")
        client.get("/ok")
    assert len(events) == 1
    assert app.signals.listener_count(RequestComplete) == 0


def test_capture_without_arguments_records_everything() -> None:
    with TestClient(_app()) as client:
        with client.capture() as events:
            client.get("/ok")
    assert {type(e) for e in events} >= {RequestReceived, RequestComplete}


def test_capture_requires_a_lauren_app() -> None:
    async def raw(scope, receive, send):  # pragma: no cover - never called
        pass

    with TestClient(raw) as client:
        with pytest.raises(TypeError, match="Lauren app"):
            with client.capture():
                pass  # pragma: no cover


async def test_async_client_raises_server_exceptions_and_captures() -> None:
    async with AsyncTestClient(_app(), raise_server_exceptions=True) as client:
        with client.capture(RequestComplete) as events:
            with pytest.raises(RuntimeError, match="kaboom"):
                await client.get("/boom")
    assert events[0].status == 500
