"""Integration tests for Phase 3 cross-file changes."""

from __future__ import annotations

import dataclasses
from typing import TypedDict

import pytest

from lauren import LaurenFactory, controller, get, module, post
from lauren.testing import TestClient


@dataclasses.dataclass
class Order:
    """A customer order."""

    customer: str
    total: float
    items: int = 0


class ShipAddress(TypedDict):
    street: str
    city: str


@controller("/orders")
class OrderController:
    @post("/")
    async def create_order(self, body: Order) -> dict:
        return {"customer": body.customer, "total": body.total, "items": body.items}

    @get("/{order_id}")
    async def get_order(self, order_id: int) -> dict:
        return {"customer": "alice", "total": 0.0, "items": order_id}


@controller("/ship")
class ShipController:
    @post("/")
    async def ship(self, body: ShipAddress) -> dict:
        return {"shipped_to": body["city"]}


@module(controllers=[OrderController, ShipController])
class OrderModule:
    pass


@pytest.fixture(scope="module")
def client():
    return TestClient(LaurenFactory.create(OrderModule, openapi_url="/openapi.json"))


class TestAutoPromotion:
    """_asgi/__init__.py: @dataclass and TypedDict params auto-promoted to JSON body."""

    def test_is_json_body_type_covers_dataclass(self):
        from lauren._validation import is_json_body_type

        assert is_json_body_type(Order)

    def test_is_json_body_type_covers_typeddict(self):
        from lauren._validation import is_json_body_type

        assert is_json_body_type(ShipAddress)

    def test_dataclass_body_promoted_returns_200(self, client):
        resp = client.post("/orders/", json={"customer": "bob", "total": 9.99})
        assert resp.status_code == 200

    def test_typeddict_body_promoted_returns_200(self, client):
        resp = client.post("/ship/", json={"street": "1 Main", "city": "NYC"})
        assert resp.status_code == 200
        assert resp.json()["shipped_to"] == "NYC"


class TestOpenAPIIntegration:
    """_asgi/_openapi.py: schemas generated for @dataclass and TypedDict."""

    def test_openapi_json_reachable(self, client):
        resp = client.get("/openapi.json")
        assert resp.status_code == 200

    def test_order_in_components(self, client):
        spec = client.get("/openapi.json").json()
        assert "Order" in spec.get("components", {}).get("schemas", {})

    def test_ship_address_in_components(self, client):
        spec = client.get("/openapi.json").json()
        assert "ShipAddress" in spec.get("components", {}).get("schemas", {})


class TestSerializationShim:
    def test_pydantic_encoder_import_path_preserved(self):
        from lauren.serialization import PydanticEncoder  # noqa: F401

    def test_pydantic_encoder_not_in_serialization_module_body(self):
        import inspect

        import lauren.serialization as mod

        src = inspect.getsource(mod)
        assert "class PydanticEncoder" not in src


class TestPydanticRegressionIntegration:
    """Verify pydantic BaseModel bodies still work after Phase 3."""

    def test_pydantic_basemodel_body(self):
        pytest.importorskip("pydantic")
        from pydantic import BaseModel

        @dataclasses.dataclass
        class _Unused:
            pass

        class PydItem(BaseModel):
            name: str
            price: float

        @controller("/pyd")
        class PydController:
            @post("/items")
            async def create(self, body: PydItem) -> dict:
                return {"name": body.name, "price": body.price}

        @module(controllers=[PydController])
        class PydModule:
            pass

        c = TestClient(LaurenFactory.create(PydModule))
        resp = c.post("/pyd/items", json={"name": "widget", "price": 9.99})
        assert resp.status_code == 200
        assert resp.json()["name"] == "widget"

    def test_pydantic_422_on_bad_type(self):
        pytest.importorskip("pydantic")
        from pydantic import BaseModel

        class PydItem(BaseModel):
            name: str
            price: float

        @controller("/pyd2")
        class PydController2:
            @post("/items")
            async def create(self, body: PydItem) -> dict:
                return {}

        @module(controllers=[PydController2])
        class PydModule2:
            pass

        c = TestClient(LaurenFactory.create(PydModule2))
        resp = c.post("/pyd2/items", json={"name": "x", "price": "bad"})
        assert resp.status_code == 422
