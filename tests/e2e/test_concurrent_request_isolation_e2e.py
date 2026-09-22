"""E2E: pooled ``Request`` isolation under sequential and concurrent load."""

from __future__ import annotations

import asyncio

from lauren import (
    CallNext,
    LaurenFactory,
    Request,
    Response,
    controller,
    get,
    middleware,
    module,
)
from lauren.testing import TestClient


@middleware()
class StampMiddleware:
    async def dispatch(self, request: Request, call_next: CallNext) -> Response:
        incoming = request.headers.get("x-marker")
        if incoming:
            setattr(request, "marker", incoming)
        return await call_next(request)


@controller("/")
class EchoController:
    @get("/echo")
    async def echo(self, request: Request) -> Response:
        return Response.json({"marker": getattr(request, "marker", None)})


@module(controllers=[EchoController])
class AppModule:
    pass


def _client() -> TestClient:
    return TestClient(LaurenFactory.create(AppModule, global_middlewares=[StampMiddleware]))


async def test_sequential_requests_never_see_a_previous_marker() -> None:
    """Alternating tagged/untagged requests prove pooled reuse leaks nothing."""
    client = _client()
    for i in range(200):
        tagged = await client.arequest("GET", "/echo", headers={"x-marker": f"m{i}"})
        assert tagged.json()["marker"] == f"m{i}"
        untagged = await client.arequest("GET", "/echo")
        assert untagged.json()["marker"] is None


async def test_concurrent_requests_are_isolated() -> None:
    client = _client()

    async def one(i: int) -> tuple[int, str | None]:
        resp = await client.arequest("GET", "/echo", headers={"x-marker": f"c{i}"})
        return i, resp.json()["marker"]

    results = await asyncio.gather(*(one(i) for i in range(64)))
    for i, marker in results:
        assert marker == f"c{i}"
