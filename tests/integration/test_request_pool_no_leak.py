"""Integration: a pooled ``Request`` must not leak attributes between requests.

Two invariants, both driven through a real ``TestClient``:

* **Attributes** — a global middleware stamps a *plain attribute* on the request
  only when an ``x-marker`` header is present; the handler echoes it back. A
  pooled instance that carried a previous request's marker would surface here as
  a stale value on an untagged request.
* **Containers** — ``path_params`` is handed out *by reference*, so a dict a
  caller kept from request N must not be mutated when request N+1 reuses the
  pooled instance. A middleware that retains the live dict (rather than a copy)
  makes any in-place reuse observable as a clobbered snapshot.
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
    use_middlewares,
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


# -- Containers handed out by reference ---------------------------------------
# A controller middleware runs *after* routing, so by the time it looks at
# ``request.path_params`` the dict holds this request's params. Recording the
# live dict (never ``dict(...)``) is what lets the test below observe an
# in-place clobber on a later request.

#: Live ``path_params`` dicts, in request order — references, not copies.
_recorded_path_params: list[dict[str, str]] = []
#: ``id()`` of the ``Request`` each recorded dict came from, so the test can
#: assert the two requests really did share one pooled instance.
_recorded_request_ids: list[int] = []


@middleware()
class PathParamsRecorder:
    """Keep a reference to the live ``path_params`` dict of every request."""

    async def dispatch(self, request: Request, call_next: CallNext) -> Response:
        _recorded_path_params.append(request.path_params)
        _recorded_request_ids.append(id(request))
        return await call_next(request)


@controller("/")
@use_middlewares(PathParamsRecorder)
class ParamsController:
    """Two routes with different params, so a clobber cannot be mistaken for
    a legitimate value.
    """

    @get("/items/{item_id}")
    async def item(self, request: Request) -> Response:
        return Response.json(dict(request.path_params))

    @get("/widgets/{widget_id}")
    async def widget(self, request: Request) -> Response:
        return Response.json(dict(request.path_params))


@module(controllers=[ParamsController])
class ParamsModule:
    pass


def _params_client() -> TestClient:
    return TestClient(LaurenFactory.create(ParamsModule))


class TestPathParamsContainerNotMutated:
    """A retained ``path_params`` dict must survive the next pooled request."""

    def test_retained_path_params_are_not_clobbered_by_the_next_request(self) -> None:
        _recorded_path_params.clear()
        _recorded_request_ids.clear()
        client = _params_client()

        first = client.get("/items/42")
        assert first.status_code == 200
        assert first.json() == {"item_id": "42"}
        assert len(_recorded_path_params) == 1

        # The dict the middleware kept *is* the one the handler saw, retained
        # live; it is not a copy made for the test's convenience.
        held = _recorded_path_params[0]
        assert held == {"item_id": "42"}

        second = client.get("/widgets/99")
        assert second.status_code == 200
        assert second.json() == {"widget_id": "99"}

        # Guard against a vacuous pass: these assertions can only be broken by a
        # *pooled* instance, so prove the second request reused the first's
        # ``Request`` object before trusting the outcome below.
        assert _recorded_request_ids[0] == _recorded_request_ids[1]

        # Each request nevertheless got its own container...
        assert _recorded_path_params[0] is not _recorded_path_params[1]
        assert _recorded_path_params[1] == {"widget_id": "99"}

        # ...and the invariant: request 1's retained dict is byte-for-byte as it
        # was — not emptied, and not carrying request 2's params.
        assert held == {"item_id": "42"}
        assert "widget_id" not in held

    def test_holds_across_many_sequential_pooled_requests(self) -> None:
        """Every earlier snapshot stays intact as later requests reuse the pool."""
        _recorded_path_params.clear()
        _recorded_request_ids.clear()
        client = _params_client()

        expected = [{"item_id": str(i)} for i in range(5)]
        for snapshot in expected:
            resp = client.get(f"/items/{snapshot['item_id']}")
            assert resp.json() == snapshot

        resp = client.get("/widgets/99")
        assert resp.json() == {"widget_id": "99"}

        assert _recorded_path_params[: len(expected)] == expected
        assert len(set(_recorded_request_ids)) == 1  # one pooled instance throughout
