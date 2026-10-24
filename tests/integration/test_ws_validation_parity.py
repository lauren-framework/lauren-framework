"""Integration: one payload yields the same issues over HTTP and WebSocket (PRD F02 §10.5).

The same app answers ``POST /p/<kind>`` and the WebSocket event ``<kind>``
with identical payload types. For every type the HTTP 422 ``issues`` equal the
WebSocket error frame's ``issues`` once the location roots (``body`` vs.
``frame.data``) are removed — with pydantic importable and with it blocked.
"""

from __future__ import annotations

import asyncio
import dataclasses
import sys
from typing import Any, Literal, TypedDict

import pytest

from lauren import Discriminated, Json, LaurenFactory, controller, module, post
from lauren.testing import TestClient, WsTestClient
from lauren.validation import ValidationPolicy
from lauren.websockets import WebSocket, on_message, ws_controller


@dataclasses.dataclass
class Address:
    city: str


@dataclasses.dataclass
class DCMsg:
    text: str
    count: int
    address: Address | None = None


class TDMsg(TypedDict):
    text: str
    count: int


@dataclasses.dataclass
class Cat:
    kind: Literal["cat"]
    lives: int = 9


@dataclasses.dataclass
class Dog:
    kind: Literal["dog"]
    good: bool = True


Pet = Discriminated[Cat | Dog, "kind"]

try:
    import pydantic as _pydantic

    class PydMsg(_pydantic.BaseModel):
        text: str
        count: int

except ImportError:  # pragma: no cover - pydantic is an optional extra
    PydMsg = None  # type: ignore[assignment,misc]

CASES: list[tuple[str, Any]] = [
    ("dc", {"text": 1, "count": "many", "address": {"city": 5}}),
    ("dc", {"count": 1, "extra": True}),
    ("td", {"text": "x", "count": "many"}),
    ("pet", {"kind": "cat", "lives": "many"}),
    ("pet", {"kind": "fish"}),
    ("pet", {"lives": 1}),
]


def _build_app(*, with_pydantic: bool, policy: ValidationPolicy) -> Any:
    @controller("/p")
    class HttpEndpoints:
        @post("/dc")
        async def dc(self, body: Json[DCMsg]) -> dict:
            return {}

        @post("/td")
        async def td(self, body: Json[TDMsg]) -> dict:
            return {}

        @post("/pet")
        async def pet(self, body: Json[Pet]) -> dict:  # type: ignore[valid-type]
            return {}

    @ws_controller("/ws")
    class Gateway:
        @on_message("dc")
        async def dc(self, ws: WebSocket, body: Json[DCMsg]) -> None:
            await ws.send_json({"ok": True})

        @on_message("td")
        async def td(self, ws: WebSocket, body: Json[TDMsg]) -> None:
            await ws.send_json({"ok": True})

        @on_message("pet")
        async def pet(self, ws: WebSocket, body: Json[Pet]) -> None:  # type: ignore[valid-type]
            await ws.send_json({"ok": True})

    controllers: list[type] = [HttpEndpoints, Gateway]
    if with_pydantic:

        @controller("/py")
        class PydHttp:
            @post("/pyd")
            async def pyd(self, body: Json[PydMsg]) -> dict:  # type: ignore[valid-type]
                return {}

        @ws_controller("/wspy")
        class PydGateway:
            @on_message("pyd")
            async def pyd(self, ws: WebSocket, body: Json[PydMsg]) -> None:  # type: ignore[valid-type]
                await ws.send_json({"ok": True})

        controllers += [PydHttp, PydGateway]

    @module(controllers=controllers)
    class AppModule:
        pass

    return LaurenFactory.create(AppModule, validation=policy)


def _http_issues(app: Any, path: str, payload: Any) -> list[dict[str, Any]]:
    response = TestClient(app).post(path, json=payload)
    assert response.status_code == 422
    issues = response.json()["error"]["detail"]["issues"]
    for issue in issues:
        assert issue["loc"][0] == "body"
        issue["loc"] = issue["loc"][1:]
    return issues


def _ws_issues(app: Any, path: str, event: str, payload: Any) -> list[dict[str, Any]]:
    async def run() -> dict[str, Any]:
        async with WsTestClient(app).connect(path) as ws:
            await ws.send_json({"event": event, "data": payload})
            return await ws.receive_json()

    frame = asyncio.run(run())
    assert frame["error"]["code"] == "websocket_validation_error"
    assert frame["error"]["detail"]["event"] == event
    issues = frame["error"]["detail"]["issues"]
    for issue in issues:
        assert issue["loc"][:2] == ["frame", "data"]
        issue["loc"] = issue["loc"][2:]
    return issues


@pytest.fixture
def block_pydantic(monkeypatch: pytest.MonkeyPatch, _preload_lauren: None) -> None:
    import lauren.streaming

    monkeypatch.setitem(sys.modules, "pydantic", None)  # type: ignore[arg-type]
    monkeypatch.setattr(lauren.streaming, "_PYDANTIC_AVAILABLE", False)


ENFORCE = ValidationPolicy(stdlib_models="enforce")


@pytest.mark.parametrize(("kind", "payload"), CASES)
def test_http_and_websocket_issues_match(kind: str, payload: Any) -> None:
    app = _build_app(with_pydantic=False, policy=ENFORCE)

    http = _http_issues(app, f"/p/{kind}", payload)

    assert http == _ws_issues(app, "/ws", kind, payload)


@pytest.mark.parametrize(("kind", "payload"), CASES)
def test_http_and_websocket_issues_match_without_pydantic(
    block_pydantic: None, kind: str, payload: Any
) -> None:
    app = _build_app(with_pydantic=False, policy=ENFORCE)

    http = _http_issues(app, f"/p/{kind}", payload)

    assert http == _ws_issues(app, "/ws", kind, payload)


def test_pydantic_payloads_match_in_the_default_mode() -> None:
    pytest.importorskip("pydantic")
    app = _build_app(with_pydantic=True, policy=ValidationPolicy())
    payload = {"text": 1, "count": "many"}

    http = _http_issues(app, "/py/pyd", payload)

    assert [i["type"] for i in http] == ["string_type", "int_parsing"]
    assert http == _ws_issues(app, "/wspy", "pyd", payload)


def test_websocket_frames_validate_natively_without_pydantic(block_pydantic: None) -> None:
    app = _build_app(with_pydantic=False, policy=ENFORCE)

    async def run() -> dict[str, Any]:
        async with WsTestClient(app).connect("/ws") as ws:
            await ws.send_json({"event": "dc", "data": {"text": "x", "count": "2"}})
            return await ws.receive_json()

    assert asyncio.run(run()) == {"ok": True}


def test_websocket_frames_keep_the_legacy_errors_list() -> None:
    app = _build_app(with_pydantic=False, policy=ENFORCE)

    async def run() -> dict[str, Any]:
        async with WsTestClient(app).connect("/ws") as ws:
            await ws.send_json({"event": "dc", "data": {"text": "x"}})
            return await ws.receive_json()

    detail = asyncio.run(run())["error"]["detail"]
    assert detail["errors"] == ["frame.data.count: Field required"]


def test_frames_without_a_data_member_are_rooted_at_frame() -> None:
    app = _build_app(with_pydantic=False, policy=ENFORCE)

    async def run() -> dict[str, Any]:
        async with WsTestClient(app).connect("/ws") as ws:
            await ws.send_json({"event": "td", "text": "x", "count": "many"})
            return await ws.receive_json()

    issues = asyncio.run(run())["error"]["detail"]["issues"]
    assert issues[0]["loc"] == ["frame", "count"]
