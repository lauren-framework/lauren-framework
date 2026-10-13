"""E2E: a middleware's retained ``path_params`` reference is stable in-request.

``Request.path_params`` hands out the live, mutable dict (``lauren/types.py``
returns ``self._path_params`` itself). A middleware is allowed to keep that
reference for the duration of the request — PRD-02 §4 treats retention *within*
a request as supported, since the pooled ``Request`` stays leased until the
handler returns.

This test pins the in-request half of that contract: a reference captured by a
middleware still holds the correct params when the handler re-reads it after an
``await`` (a scheduler tick, so the read lands in a later event-loop iteration
rather than inline). The cross-request half — a dict retained from an earlier
request never being mutated — is covered by the integration and property tiers;
a strictly in-request test cannot observe that case, by construction.
"""

from __future__ import annotations

import anyio

from lauren import (
    CallNext,
    LaurenFactory,
    Request,
    Response,
    controller,
    get,
    middleware,
    module,
    use_middlewares,
)
from lauren.testing import TestClient

#: Live ``path_params`` dicts captured by the middleware, in request order.
#: These are references, never copies — that is the point of the test.
retained: list[dict[str, str]] = []


@middleware()
class RetainPathParams:
    """Keep a reference to the request's live ``path_params`` dict.

    Attached to the controller rather than globally, so it runs after routing
    and the dict is already populated with this request's params at capture
    time.
    """

    async def dispatch(self, request: Request, call_next: CallNext) -> Response:
        retained.append(request.path_params)
        return await call_next(request)


@controller("/items")
@use_middlewares(RetainPathParams)
class ItemController:
    @get("/{item_id}")
    async def show(self, request: Request) -> Response:
        params_at_entry = dict(request.path_params)

        # Force a scheduler tick: the read below therefore happens in a later
        # event-loop iteration, not inline with the middleware's capture.
        await anyio.sleep(0)

        held = retained[-1]
        return Response.json(
            {
                "params_at_entry": params_at_entry,
                "retained_after_yield": dict(held),
                "same_object": held is request.path_params,
            }
        )


@module(controllers=[ItemController])
class RetentionModule:
    pass


def _client() -> TestClient:
    return TestClient(LaurenFactory.create(RetentionModule))


async def test_retained_reference_still_holds_the_params_after_a_yield() -> None:
    retained.clear()
    client = _client()

    resp = await client.arequest("GET", "/items/42")
    assert resp.status_code == 200

    body = resp.json()
    assert body["params_at_entry"] == {"item_id": "42"}
    # The reference the middleware kept is the handler's own live dict...
    assert body["same_object"] is True
    # ...and it still carries this request's params after the scheduler tick.
    assert body["retained_after_yield"] == {"item_id": "42"}


async def test_reference_is_stable_for_every_request_of_a_burst() -> None:
    """Each request's in-flight reference is still correct after its own yield."""
    retained.clear()
    client = _client()

    for item_id in ("1", "2", "3"):
        resp = await client.arequest("GET", f"/items/{item_id}")
        assert resp.status_code == 200

        body = resp.json()
        assert body["same_object"] is True
        # Within this request the retained reference is exactly this request's
        # params — it was neither emptied nor swapped out mid-request.
        assert body["retained_after_yield"] == {"item_id": item_id}
        assert body["retained_after_yield"] == body["params_at_entry"]
