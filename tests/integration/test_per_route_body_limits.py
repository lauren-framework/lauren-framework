"""Per-route and per-controller ``max_body_size`` (F03 §5.4) and the 413 regression (§1.2)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from lauren import (
    ByteStream,
    Bytes,
    Json,
    LaurenFactory,
    Request,
    Response,
    UploadFile,
    controller,
    middleware,
    module,
    post,
)
from lauren.reflect import get_all_routes, reflect_routes
from lauren.testing import TestClient

APP_LIMIT = 100


@dataclass
class Doc:
    text: str


@controller("/media", max_body_size=50)
class MediaController:
    @post("/default")
    async def default(self, body: Bytes) -> dict:
        return {"size": len(body)}

    @post("/tight", max_body_size=10)
    async def tight(self, body: Bytes) -> dict:
        return {"size": len(body)}

    @post("/video", max_body_size=1000)
    async def video(self, body: ByteStream) -> dict:
        return {"size": sum([len(chunk) async for chunk in body])}


@controller("/plain")
class PlainController:
    @post("/json")
    async def json_body(self, body: Json[Doc]) -> dict:
        return {"len": len(body.text)}

    @post("/bytes")
    async def raw(self, body: Bytes) -> dict:
        return {"size": len(body)}

    @post("/stream")
    async def stream(self, body: ByteStream) -> dict:
        return {"size": sum([len(chunk) async for chunk in body])}

    @post("/upload")
    async def upload(self, file: UploadFile) -> dict:
        return {"size": file.size}


@module(controllers=[MediaController, PlainController])
class AppModule:
    pass


def _client(**kwargs: Any) -> TestClient:
    return TestClient(LaurenFactory.create(AppModule, max_body_size=APP_LIMIT, **kwargs))


@pytest.mark.parametrize(
    ("url", "size", "status"),
    [
        ("/media/default", 50, 200),
        ("/media/default", 51, 413),
        ("/media/tight", 10, 200),
        ("/media/tight", 11, 413),
        ("/media/video", 1000, 200),
        ("/media/video", 1001, 413),
        ("/plain/bytes", 100, 200),
        ("/plain/bytes", 101, 413),
    ],
)
def test_route_then_controller_then_app_limit(url: str, size: int, status: int) -> None:
    response = _client().post(url, content=b"x" * size)

    assert response.status_code == status
    if status == 413:
        assert response.json()["error"]["code"] == "request_body_too_large"


def test_a_route_limit_above_the_app_limit_streams_a_large_body() -> None:
    chunks = (b"y" * 100 for _ in range(9))  # streamed: no Content-Length

    response = _client().post("/media/video", content=chunks)

    assert response.status_code == 200
    assert response.json() == {"size": 900}


def test_413_reports_the_route_limit() -> None:
    response = _client().post("/media/tight", content=b"x" * 20)

    assert response.json()["error"]["detail"] == {"max_body_size": 10}


@pytest.mark.parametrize("url", ["/plain/json", "/plain/bytes", "/plain/stream", "/plain/upload"])
def test_every_oversize_body_path_is_413_not_422(url: str) -> None:
    client = _client()
    if url == "/plain/upload":
        response = client.post(url, files={"file": ("a.bin", b"z" * 500, "application/octet-stream")})
    else:
        response = client.post(
            url, content=b'{"text": "' + b"z" * 500 + b'"}', headers={"content-type": "application/json"}
        )

    assert response.status_code == 413


@middleware()
class BufferFirst:
    """A global middleware that reads the body before routing."""

    async def dispatch(self, request: Request, call_next: Any) -> Response:
        await request.body()
        return await call_next(request)


def test_pre_routing_buffering_is_bounded_by_the_app_limit() -> None:
    client = _client(global_middlewares=[BufferFirst])

    response = client.post("/media/video", content=b"v" * 500)

    assert response.status_code == 413
    assert response.json()["error"]["detail"] == {"max_body_size": APP_LIMIT}


def test_an_already_buffered_body_over_the_route_limit_is_rejected_after_routing() -> None:
    client = _client(global_middlewares=[BufferFirst])

    small = client.post("/media/tight", content=b"t" * 10)
    large = client.post("/media/tight", content=b"t" * 40)

    assert small.status_code == 200
    assert large.status_code == 413
    assert large.json()["error"]["detail"] == {"max_body_size": 10}


async def test_an_announced_oversize_body_is_rejected_without_reading_it() -> None:
    app = LaurenFactory.create(AppModule, max_body_size=APP_LIMIT)
    received: list[dict[str, Any]] = []
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        received.append({})
        return {"type": "http.request", "body": b"x" * 5000, "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "path": "/media/tight",
        "raw_path": b"/media/tight",
        "query_string": b"",
        "headers": [(b"content-length", b"5000")],
        "client": ("127.0.0.1", 1),
        "server": ("test", 80),
        "scheme": "http",
        "root_path": "",
    }
    await app.startup()
    try:
        await app(scope, receive, send)
    finally:
        await app.shutdown()

    assert sent[0]["status"] == 413
    assert received == []


def test_reflection_reports_the_limits() -> None:
    client = _client()
    static = {r.path: r.max_body_size for r in reflect_routes(MediaController)}
    with client:
        effective = {r.full_path: r.max_body_size for r in get_all_routes(client.app)}

    assert static == {"/default": 50, "/tight": 10, "/video": 1000}
    assert effective["/media/tight"] == 10 and effective["/media/default"] == 50
    assert effective["/plain/bytes"] is None
