"""Regression lock: HTTP and WebSocket request-scope teardown are identical.

Both paths go through the shared finaliser (``lauren._di._scope.finalize_scope``)
— the same order (newest first, ``@pre_destruct`` then ``aclose()``), the same
error handling (logged, never raised) — and neither rebuilds a provider map
per request.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Protocol, runtime_checkable

import pytest

from lauren import (
    Depends,
    LaurenFactory,
    Request,
    Scope,
    WebSocket,
    controller,
    get,
    injectable,
    module,
    on_connect,
    pre_destruct,
    ws_controller,
)
from lauren.testing import WsTestClient

EVENTS: list[str] = []


@pytest.fixture(autouse=True)
def _reset_events() -> None:
    EVENTS.clear()


@injectable(scope=Scope.REQUEST)
class Session:
    @pre_destruct
    async def flush(self) -> None:
        EVENTS.append("Session.pre_destruct")

    async def aclose(self) -> None:
        EVENTS.append("Session.aclose")


@injectable(scope=Scope.REQUEST)
class Flaky:
    @pre_destruct
    def flush(self) -> None:
        EVENTS.append("Flaky.pre_destruct")
        raise RuntimeError("flush failed")

    def aclose(self) -> None:
        EVENTS.append("Flaky.aclose")


@injectable(scope=Scope.REQUEST)
def audit() -> Any:
    yield "audit"
    EVENTS.append("audit.teardown")


@runtime_checkable
class Plugin(Protocol):
    def name(self) -> str: ...


@injectable(scope=Scope.REQUEST, provides=(Plugin,), multi=True)
class PluginA:
    def name(self) -> str:
        return "a"

    @pre_destruct
    def close(self) -> None:
        EVENTS.append("PluginA.pre_destruct")


@injectable(scope=Scope.REQUEST, provides=(Plugin,), multi=True)
class PluginB:
    def name(self) -> str:
        return "b"

    @pre_destruct
    def close(self) -> None:
        EVENTS.append("PluginB.pre_destruct")


@controller("/r")
class HttpController:
    @get("/")
    async def index(self, session: Session, flaky: Flaky, log: Depends[audit], plugins: list[Plugin]) -> dict:
        return {"plugins": [p.name() for p in plugins]}


@ws_controller("/ws")
class Gateway:
    def __init__(self, session: Session, flaky: Flaky, log: Depends[audit], plugins: list[Plugin]) -> None:
        self.plugins = plugins

    @on_connect
    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()


@module(controllers=[HttpController, Gateway], providers=[Session, Flaky, audit, PluginA, PluginB])
class AppModule:
    pass


_TEARDOWN = [
    "PluginB.pre_destruct",
    "PluginA.pre_destruct",
    "audit.teardown",
    "Flaky.pre_destruct",
    "Flaky.aclose",
    "Session.pre_destruct",
    "Session.aclose",
]


async def _http(app: Any) -> Any:
    return await app.handle(Request(method="GET", path="/r/"))


async def _ws(app: Any) -> None:
    async with WsTestClient(app).connect("/ws") as ws:
        assert ws._accepted


def _drive(transport: str) -> tuple[Any, list[str]]:
    app = LaurenFactory.create(AppModule)

    async def main() -> Any:
        await app.startup()
        return await (_http(app) if transport == "http" else _ws(app))

    result = asyncio.run(main())
    return result, list(EVENTS)


@pytest.mark.parametrize("transport", ["http", "ws"])
def test_teardown_order_is_newest_first_pre_destruct_then_aclose(transport: str) -> None:
    _, events = _drive(transport)

    assert events == _TEARDOWN


def test_http_and_ws_teardown_logs_are_identical() -> None:
    _, http_events = _drive("http")
    EVENTS.clear()
    _, ws_events = _drive("ws")

    assert http_events == ws_events


@pytest.mark.parametrize("transport", ["http", "ws"])
def test_teardown_errors_are_logged_and_never_escape(
    transport: str, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.ERROR, logger="lauren"):
        result, _ = _drive(transport)

    if transport == "http":
        assert result.status == 200
    origin = "request" if transport == "http" else "websocket"
    assert f"Error in @pre_destruct on Flaky ({origin} teardown)" in caplog.text


@pytest.mark.parametrize("transport", ["http", "ws"])
def test_teardown_does_not_rebuild_the_provider_map_per_request(transport: str) -> None:
    app = LaurenFactory.create(AppModule)
    index = app.container.instance_index()

    def _forbidden() -> Any:
        raise AssertionError("all_providers() must not run on the request path")

    async def main() -> None:
        await app.startup()
        app.container.all_providers = _forbidden  # type: ignore[method-assign]
        await (_http(app) if transport == "http" else _ws(app))

    asyncio.run(main())

    assert app.container.instance_index() is index
    assert EVENTS == _TEARDOWN
