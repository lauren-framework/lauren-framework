"""Integration tests for the request-scope leak guard (PRD F19 §5.9).

Resolving a ``Scope.REQUEST`` provider outside any request, WebSocket
connection or ``app.scope()`` falls back to an untracked transient in the 1.x
default (with a one-time warning per provider) and raises with
``strict_request_scope=True``. Requests, WebSocket connections and operation
scopes are unaffected.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

from lauren import (
    Lauren,
    LaurenFactory,
    Scope,
    controller,
    get,
    injectable,
    module,
    use_factory,
)
from lauren.exceptions import DIScopeViolationError
from lauren.testing import TestClient, WsTestClient
from lauren.websockets import WebSocket, on_connect, ws_controller

CLOSED: list[str] = []


@pytest.fixture(autouse=True)
def _reset() -> None:
    CLOSED.clear()


@injectable(scope=Scope.REQUEST)
class DbSession:
    async def aclose(self) -> None:
        CLOSED.append("DbSession")


@injectable(scope=Scope.REQUEST)
class Other:
    pass


@injectable(scope=Scope.TRANSIENT)
class Report:
    def __init__(self, db: DbSession) -> None:
        self.db = db


@controller("/db")
class DbController:
    @get("/")
    async def read(self, db: DbSession) -> dict[str, bool]:
        return {"ok": isinstance(db, DbSession)}


@ws_controller("/ws")
class DbGateway:
    def __init__(self, db: DbSession) -> None:
        self.db = db

    @on_connect
    async def connected(self, ws: WebSocket) -> None:
        await ws.accept()
        await ws.send_json({"ok": isinstance(self.db, DbSession)})


@module(controllers=[DbController, DbGateway], providers=[DbSession, Other, Report])
class AppModule:
    pass


def _warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if "resolved outside a request" in r.getMessage()]


async def test_lax_mode_falls_back_to_a_new_instance_each_time() -> None:
    app = LaurenFactory.create(AppModule)
    await app.startup()
    try:
        first = await app.container.resolve(DbSession)
        second = await app.container.resolve(DbSession)
    finally:
        await app.shutdown()

    assert first is not second
    assert CLOSED == []  # never torn down: the leak the warning is about


async def test_lax_mode_warns_once_per_provider(caplog: pytest.LogCaptureFixture) -> None:
    app = LaurenFactory.create(AppModule)
    await app.startup()
    try:
        with caplog.at_level(logging.WARNING, logger="lauren"):
            await app.container.resolve(DbSession)
            await app.container.resolve(DbSession)
            await app.container.resolve(Other)
    finally:
        await app.shutdown()

    messages = _warnings(caplog)
    assert len(messages) == 2
    assert messages[0] == (
        "DbSession is request-scoped but was resolved outside a request or `app.scope()`; it will never be "
        "torn down. Use `app.scope()` or enable `strict_request_scope=True`."
    )
    assert messages[1].startswith("Other is request-scoped")


async def test_warnings_are_per_app(caplog: pytest.LogCaptureFixture) -> None:
    first, second = LaurenFactory.create(AppModule), LaurenFactory.create(AppModule)

    with caplog.at_level(logging.WARNING, logger="lauren"):
        await first.container.resolve(DbSession)
        await second.container.resolve(DbSession)

    assert len(_warnings(caplog)) == 2


async def test_strict_mode_raises_with_detail() -> None:
    app = LaurenFactory.create(AppModule, strict_request_scope=True)

    with pytest.raises(DIScopeViolationError) as info:
        await app.container.resolve(DbSession)

    assert info.value.detail == {"token": "DbSession", "reason": "no_active_scope"}
    assert "app.scope()" in str(info.value)


async def test_strict_mode_catches_a_request_scoped_dependency_of_a_transient() -> None:
    app = LaurenFactory.create(AppModule, strict_request_scope=True)

    with pytest.raises(DIScopeViolationError) as info:
        await app.container.resolve(Report)

    assert info.value.detail["token"] == "DbSession"


async def test_strict_mode_names_string_tokens() -> None:
    @module(providers=[use_factory(provide="conn", factory=lambda: object(), scope=Scope.REQUEST)])
    class FactoryModule:
        pass

    app = LaurenFactory.create(FactoryModule, strict_request_scope=True)

    with pytest.raises(DIScopeViolationError) as info:
        await app.container.resolve("conn")

    assert info.value.detail["token"] == "conn"


async def test_operation_scopes_work_in_strict_mode() -> None:
    app = LaurenFactory.create(AppModule, strict_request_scope=True)
    await app.startup()
    try:
        async with app.scope("job") as scope:
            db = await scope.resolve(DbSession)
            report = await scope.resolve(Report)
            assert report.db is db
    finally:
        await app.shutdown()

    assert CLOSED == ["DbSession"]


@pytest.mark.parametrize("strict", [False, True])
def test_http_requests_are_unaffected(strict: bool, caplog: pytest.LogCaptureFixture) -> None:
    app = LaurenFactory.create(AppModule, strict_request_scope=strict)

    with caplog.at_level(logging.WARNING, logger="lauren"), TestClient(app) as client:
        response = client.get("/db/")

    assert response.json() == {"ok": True}
    assert CLOSED == ["DbSession"]
    assert _warnings(caplog) == []


@pytest.mark.parametrize("strict", [False, True])
async def test_websocket_connections_are_unaffected(strict: bool, caplog: pytest.LogCaptureFixture) -> None:
    app = LaurenFactory.create(AppModule, strict_request_scope=strict)
    await app.startup()
    try:
        with caplog.at_level(logging.WARNING, logger="lauren"):
            async with WsTestClient(app).connect("/ws") as ws:
                assert await ws.receive_json() == {"ok": True}
    finally:
        await app.shutdown()

    assert _warnings(caplog) == []


async def test_lauren_forwards_strict_request_scope() -> None:
    lauren_app = Lauren(strict_request_scope=True, openapi_url=None, docs_url=None, redoc_url=None)
    lauren_app.add_provider(DbSession)

    compiled: Any = await lauren_app.startup()  # compiles without starting

    assert compiled.container._strict_request_scope is True
