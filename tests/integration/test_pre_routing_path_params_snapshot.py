"""Lock the router's sanctioned in-place population of ``Request.path_params``.

``lauren/_asgi/__init__.py`` routes by mutating the request's existing dict::

    req._path_params.clear()
    req._path_params.update(params)

That is an in-place mutation of the very container ``Request.path_params`` hands
out by reference — the pattern the framework otherwise forbids (PRD-02 §11.1,
``CLAUDE.md`` §8). It is *sanctioned* for exactly one reason, and these tests pin
that reason so it cannot erode silently:

1. **Precondition** — ``Request.reset()`` assigns ``self._path_params = {}``, so
   the dict the router receives is fresh and unshared with any earlier request.
   Nothing here can therefore touch a dict retained from a previous request.
2. **Load-bearing** — global middlewares wrap the routing step, so a reference
   they take *before* calling ``next`` is empty at capture time and is populated
   in place by the router afterwards. That is the observable every pre-routing
   middleware relies on: same object, populated later.

If the router were changed to assign a fresh dict instead, the retained reference
would stay ``{}`` forever and
``test_pre_routing_snapshot_is_populated_in_place`` would fail. That failure is
intentional: it is the signal that the pre-routing-snapshot contract changed and
needs its own decision rather than a silent regression.
"""

from __future__ import annotations

from dataclasses import dataclass

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


@dataclass
class Snapshot:
    """What the pre-routing reference held before and after the router ran."""

    before: dict[str, str]
    after: dict[str, str]
    holds_live_dict: bool
    object_id: int


#: One record per request, in request order.
captured: list[Snapshot] = []


@middleware()
class PreRoutingSnapshot:
    """Capture ``path_params`` before routing and re-read it after the response."""

    async def dispatch(self, request: Request, call_next: CallNext) -> Response:
        held = request.path_params
        before = dict(held)
        object_id = id(held)

        response = await call_next(request)

        captured.append(
            Snapshot(
                before=before,
                after=dict(held),
                holds_live_dict=held is request.path_params,
                object_id=object_id,
            )
        )
        return response


@controller("/items")
class ItemController:
    @get("/{item_id}")
    async def show(self, request: Request) -> Response:
        return Response.json(
            {
                "params": dict(request.path_params),
                "params_id": id(request.path_params),
            }
        )


@module(controllers=[ItemController])
class PreRoutingModule:
    pass


def _client() -> TestClient:
    return TestClient(LaurenFactory.create(PreRoutingModule, global_middlewares=[PreRoutingSnapshot]))


async def test_pre_routing_snapshot_is_populated_in_place() -> None:
    """The pre-routing reference is empty, then filled in place by the router."""
    captured.clear()
    client = _client()

    resp = await client.arequest("GET", "/items/42")
    assert resp.status_code == 200
    body = resp.json()

    assert len(captured) == 1
    record = captured[0]

    # Before routing the global middleware sees the empty container reset() left.
    assert record.before == {}
    # The router populated that same object in place rather than replacing it...
    assert record.after == {"item_id": "42"}
    assert record.holds_live_dict is True
    # ...and it is the handler's own dict, not a copy made along the way.
    assert record.object_id == body["params_id"]
    assert body["params"] == {"item_id": "42"}


async def test_each_pooled_request_gets_its_own_dict() -> None:
    """The precondition holds under pool reuse: never the earlier request's dict."""
    captured.clear()
    client = _client()

    for item_id in ("1", "2", "3"):
        resp = await client.arequest("GET", f"/items/{item_id}")
        assert resp.status_code == 200
        body = resp.json()

        record = captured[-1]
        # Every request starts from an empty container...
        assert record.before == {}
        # ...its own container ends up holding only its own params...
        assert record.after == {"item_id": item_id}
        assert body["params"] == {"item_id": item_id}
        assert record.object_id == body["params_id"]

    # ...and no request ever populated a container a previous request had used.
    assert len({record.object_id for record in captured}) == len(captured)
