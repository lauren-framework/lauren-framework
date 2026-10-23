"""End-to-end check of the D03 security defaults in one realistic app.

The app combines a WebSocket gateway with a privileged, method-guarded event,
a static asset tree that contains secrets next to public files, and cookie
sessions protecting a JSON write route. Each default must hold without any
opt-in: guarded messages never reach their handler, dotfiles are not served
(``/.well-known/`` is), and cross-site ``text/plain`` JSON is flagged — then
rejected once the app opts into ``strict_content_type=True``.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from lauren import (
    ContentConfig,
    Json,
    LaurenFactory,
    Session,
    SessionConfig,
    WebSocket,
    controller,
    module,
    on_message,
    post,
    use_guards,
    ws_controller,
)
from lauren.logging import InMemoryLogger, LogLevel
from lauren.static_files import StaticFilesModule
from lauren.testing import TestClient, WsTestClient


class AdminRole:
    async def can_activate(self, ctx: Any) -> bool:
        return ctx.request.headers.get("x-role") == "admin"


@ws_controller("/ops")
class OpsGateway:
    @on_message("db.wipe")
    @use_guards(AdminRole)
    async def wipe(self, ws: WebSocket) -> None:
        await ws.send_json({"event": "wiped"})

    @on_message("ping")
    async def ping(self, ws: WebSocket) -> None:
        await ws.send_json({"event": "pong"})


@controller("/settings")
class SettingsController:
    @post("/")
    async def update(self, session: Session, body: Json[dict]) -> dict:
        session["settings"] = body
        return {"saved": True}


def _assets(root: Path) -> Path:
    assets = root / "dist"
    (assets / ".well-known").mkdir(parents=True)
    (assets / ".git").mkdir()
    (assets / ".env.production").write_bytes(b"API_KEY=secret\n")
    (assets / ".git" / "config").write_bytes(b"[remote]\n")
    (assets / ".well-known" / "security.txt").write_bytes(b"Contact: mailto:sec@example.com\n")
    (assets / "app.js").write_bytes(b"console.log('app');")
    return assets


def _app(tmp_path: Path, log: InMemoryLogger, **kwargs: Any) -> Any:
    @module(
        controllers=[OpsGateway, SettingsController],
        imports=[StaticFilesModule.for_root("/assets", directory=_assets(tmp_path))],
    )
    class AppModule:
        pass

    return LaurenFactory.create(
        AppModule,
        sessions=SessionConfig(secret="s" * 32, secure=False),
        logger=log,
        **kwargs,
    )


@pytest.fixture()
def log() -> InMemoryLogger:
    return InMemoryLogger(level=LogLevel.DEBUG)


def test_guarded_websocket_event_is_denied_for_non_admins(tmp_path: Path, log: InMemoryLogger) -> None:
    app = _app(tmp_path, log)

    async def run() -> tuple[Any, Any, Any]:
        async with WsTestClient(app).connect("/ops") as ws:
            await ws.send_json({"event": "db.wipe"})
            denied = await asyncio.wait_for(ws.receive_json(), 2)
            await ws.send_json({"event": "ping"})
            pong = await asyncio.wait_for(ws.receive_json(), 2)
        async with WsTestClient(app).connect("/ops", headers={"x-role": "admin"}) as ws:
            await ws.send_json({"event": "db.wipe"})
            wiped = await asyncio.wait_for(ws.receive_json(), 2)
        return denied, pong, wiped

    denied, pong, wiped = asyncio.run(run())

    assert denied["error"]["code"] == "forbidden"
    assert denied["error"]["detail"] == {"guard": "AdminRole", "event": "db.wipe"}
    assert pong == {"event": "pong"}
    assert wiped == {"event": "wiped"}


@pytest.mark.parametrize(
    "url,status",
    [
        ("/assets/.env.production", 404),
        ("/assets/.git/config", 404),
        ("/assets/.well-known/security.txt", 200),
        ("/assets/app.js", 200),
    ],
)
def test_static_tree_hides_dotfiles(tmp_path: Path, log: InMemoryLogger, url: str, status: int) -> None:
    client = TestClient(_app(tmp_path, log))

    response = client.get(url)

    assert response.status_code == status
    assert b"secret" not in response.body


def test_session_json_route_flags_cross_site_simple_requests(tmp_path: Path, log: InMemoryLogger) -> None:
    client = TestClient(_app(tmp_path, log))

    response = client.post("/settings/", content=b'{"theme": "dark"}', headers={"content-type": "text/plain"})

    assert response.status_code == 200
    startup = [r for r in log.records if r.context == "LaurenFactory" and "cross-site" in r.message]
    assert [r.extra["routes"] for r in startup] == [["POST /settings"]]
    assert [r.extra["content_type"] for r in log.records if r.context == "ContentPolicy"] == ["text/plain"]


def test_strict_content_type_rejects_cross_site_simple_requests(tmp_path: Path, log: InMemoryLogger) -> None:
    client = TestClient(_app(tmp_path, log, content=ContentConfig(strict_content_type=True)))

    forged = client.post("/settings/", content=b'{"theme": "dark"}', headers={"content-type": "text/plain"})
    genuine = client.post("/settings/", json={"theme": "dark"})

    assert forged.status_code == 415
    assert forged.header("set-cookie") is None
    assert genuine.status_code == 200
    assert genuine.header("set-cookie") is not None
