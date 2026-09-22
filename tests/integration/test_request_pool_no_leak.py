"""Integration: a pooled ``Request`` must not leak attributes between requests.

A global middleware stamps a *plain attribute* on the request only when an
``x-marker`` header is present; the handler echoes it back. A pooled instance
that carried a previous request's marker would surface here as a stale value
on an untagged request.
"""

from __future__ import annotations

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
class ConditionalMarker:
    """Attach ``x-marker`` to the request as a plain attribute, when present."""

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
class PoolModule:
    pass


def _client() -> TestClient:
    app = LaurenFactory.create(PoolModule, global_middlewares=[ConditionalMarker])
    return TestClient(app)


class TestRequestPoolNoLeak:
    def test_attribute_does_not_leak_to_next_request(self) -> None:
        client = _client()

        tagged = client.get("/echo", headers={"x-marker": "acme"})
        assert tagged.status_code == 200
        assert tagged.json()["marker"] == "acme"

        # No header this time — a leaked attribute would show "acme" here.
        untagged = client.get("/echo")
        assert untagged.status_code == 200
        assert untagged.json()["marker"] is None

    def test_no_leak_across_many_sequential_requests(self) -> None:
        client = _client()
        for i in range(50):
            tagged = client.get("/echo", headers={"x-marker": f"m{i}"})
            assert tagged.json()["marker"] == f"m{i}"
            untagged = client.get("/echo")
            assert untagged.json()["marker"] is None
