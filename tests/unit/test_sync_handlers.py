"""Unit tests for synchronous route handlers.

Covers the ``inspect.isawaitable`` dispatch path that allows plain ``def``
methods to be used as route handlers alongside (or instead of) ``async def``.
"""

from __future__ import annotations

from pydantic import BaseModel

from lauren import (
    Json,
    LaurenFactory,
    Path,
    Query,
    Response,
    controller,
    delete,
    get,
    module,
    patch,
    post,
    put,
)
from lauren.testing import TestClient


def build(root_module: type) -> TestClient:
    return TestClient(LaurenFactory.create(root_module))


# ---------------------------------------------------------------------------
# Basic sync handlers — instance, static, classmethod bindings
# ---------------------------------------------------------------------------


@controller("/sync")
class SyncController:
    @get("/plain")
    def plain(self) -> dict:
        return {"sync": True}

    @get("/text")
    def text_response(self) -> Response:
        return Response.text("sync text")

    @staticmethod
    @get("/static")
    def static_route() -> dict:
        return {"binding": "static"}

    @classmethod
    @get("/cls")
    def cls_route(cls) -> dict:
        return {"binding": "classmethod", "cls": cls.__name__}


@module(controllers=[SyncController])
class SyncModule:
    pass


class TestSyncBasicBindings:
    def test_instance_method_returns_dict(self):
        r = build(SyncModule).get("/sync/plain")
        assert r.status_code == 200
        assert r.json() == {"sync": True}

    def test_instance_method_returns_response(self):
        r = build(SyncModule).get("/sync/text")
        assert r.status_code == 200
        assert r.text == "sync text"

    def test_static_method(self):
        r = build(SyncModule).get("/sync/static")
        assert r.status_code == 200
        assert r.json()["binding"] == "static"

    def test_classmethod(self):
        r = build(SyncModule).get("/sync/cls")
        assert r.status_code == 200
        data = r.json()
        assert data["binding"] == "classmethod"
        assert data["cls"] == "SyncController"


# ---------------------------------------------------------------------------
# Sync handlers with parameter extractors
# ---------------------------------------------------------------------------


@controller("/params")
class SyncParamsController:
    @get("/{item_id}")
    def get_by_id(self, item_id: Path[int]) -> dict:
        return {"id": item_id}

    @get("/search")
    def search(self, q: Query[str]) -> dict:
        return {"q": q}

    @get("/optional")
    def optional_param(self, limit: Query[int] = 10) -> dict:
        return {"limit": limit}


@module(controllers=[SyncParamsController])
class SyncParamsModule:
    pass


class TestSyncWithExtractors:
    def test_path_param(self):
        r = build(SyncParamsModule).get("/params/42")
        assert r.json() == {"id": 42}

    def test_query_param(self):
        r = build(SyncParamsModule).get("/params/search?q=hello")
        assert r.json() == {"q": "hello"}

    def test_query_default(self):
        r = build(SyncParamsModule).get("/params/optional")
        assert r.json() == {"limit": 10}

    def test_query_override(self):
        r = build(SyncParamsModule).get("/params/optional?limit=5")
        assert r.json() == {"limit": 5}


# ---------------------------------------------------------------------------
# Sync handlers with request body
# ---------------------------------------------------------------------------


class Item(BaseModel):
    name: str
    price: float


@controller("/body")
class SyncBodyController:
    @post("/create")
    def create(self, item: Json[Item]) -> dict:
        return {"name": item.name, "price": item.price}

    @put("/update/{item_id}")
    def update(self, item_id: Path[int], item: Json[Item]) -> dict:
        return {"id": item_id, "name": item.name}

    @patch("/patch/{item_id}")
    def patch_item(self, item_id: Path[int], item: Json[Item]) -> dict:
        return {"id": item_id, "updated": True, "name": item.name}

    @delete("/delete/{item_id}")
    def delete_item(self, item_id: Path[int]) -> dict:
        return {"deleted": item_id}


@module(controllers=[SyncBodyController])
class SyncBodyModule:
    pass


class TestSyncWithBody:
    def test_post_with_body(self):
        r = build(SyncBodyModule).post(
            "/body/create", json={"name": "Widget", "price": 9.99}
        )
        assert r.status_code == 200
        assert r.json() == {"name": "Widget", "price": 9.99}

    def test_put_with_path_and_body(self):
        r = build(SyncBodyModule).put(
            "/body/update/7", json={"name": "Gadget", "price": 19.99}
        )
        assert r.status_code == 200
        assert r.json() == {"id": 7, "name": "Gadget"}

    def test_patch(self):
        r = build(SyncBodyModule).patch(
            "/body/patch/3", json={"name": "Thingamajig", "price": 1.5}
        )
        assert r.json() == {"id": 3, "updated": True, "name": "Thingamajig"}

    def test_delete(self):
        r = build(SyncBodyModule).delete("/body/delete/99")
        assert r.json() == {"deleted": 99}


# ---------------------------------------------------------------------------
# Mixed: sync and async handlers in the same controller
# ---------------------------------------------------------------------------


@controller("/mixed")
class MixedController:
    @get("/sync")
    def sync_handler(self) -> dict:
        return {"type": "sync"}

    @get("/async")
    async def async_handler(self) -> dict:
        return {"type": "async"}

    @get("/sync-response")
    def sync_response(self) -> Response:
        return Response(body=b'{"custom":true}', status=201, headers={})

    @get("/async-response")
    async def async_response(self) -> Response:
        return Response(body=b'{"custom":false}', status=202, headers={})


@module(controllers=[MixedController])
class MixedModule:
    pass


class TestMixedSyncAsync:
    def test_sync_endpoint(self):
        r = build(MixedModule).get("/mixed/sync")
        assert r.json() == {"type": "sync"}

    def test_async_endpoint(self):
        r = build(MixedModule).get("/mixed/async")
        assert r.json() == {"type": "async"}

    def test_sync_custom_response(self):
        r = build(MixedModule).get("/mixed/sync-response")
        assert r.status_code == 201
        assert r.json() == {"custom": True}

    def test_async_custom_response(self):
        r = build(MixedModule).get("/mixed/async-response")
        assert r.status_code == 202


# ---------------------------------------------------------------------------
# Sync handler that raises an exception
# ---------------------------------------------------------------------------


@controller("/errors")
class SyncErrorController:
    @get("/boom")
    def boom(self) -> dict:
        raise ValueError("sync error")

    @get("/value-error")
    def value_error(self, x: Query[int]) -> dict:
        if x < 0:
            raise ValueError("negative")
        return {"x": x}


@module(controllers=[SyncErrorController])
class SyncErrorModule:
    pass


class TestSyncExceptions:
    def test_uncaught_exception_returns_500(self):
        r = build(SyncErrorModule).get("/errors/boom")
        assert r.status_code == 500

    def test_no_exception_on_valid_input(self):
        r = build(SyncErrorModule).get("/errors/value-error?x=5")
        assert r.json() == {"x": 5}

    def test_exception_on_invalid_input(self):
        r = build(SyncErrorModule).get("/errors/value-error?x=-1")
        assert r.status_code == 500


# ---------------------------------------------------------------------------
# Sync handlers returning various coercible types
# ---------------------------------------------------------------------------


@controller("/coerce")
class SyncCoerceController:
    @get("/int")
    def returns_int(self) -> int:
        return 42

    @get("/str")
    def returns_str(self) -> str:
        return "hello"

    @get("/list")
    def returns_list(self) -> list:
        return [1, 2, 3]

    @get("/none")
    def returns_none(self) -> None:
        return None


@module(controllers=[SyncCoerceController])
class SyncCoerceModule:
    pass


class TestSyncReturnCoercion:
    def test_returns_int(self):
        r = build(SyncCoerceModule).get("/coerce/int")
        assert r.status_code == 200
        assert r.json() == 42

    def test_returns_str(self):
        # str return → plain-text response (not JSON)
        r = build(SyncCoerceModule).get("/coerce/str")
        assert r.status_code == 200
        assert r.text == "hello"

    def test_returns_list(self):
        r = build(SyncCoerceModule).get("/coerce/list")
        assert r.status_code == 200
        assert r.json() == [1, 2, 3]

    def test_returns_none(self):
        # None return → 204 No Content
        r = build(SyncCoerceModule).get("/coerce/none")
        assert r.status_code == 204
