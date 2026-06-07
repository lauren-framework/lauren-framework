"""E2E tests for Phase 3: all 7 changed files exercised via TestClient."""

from __future__ import annotations

import dataclasses
from typing import TypedDict

import pytest

from lauren import LaurenFactory, controller, get, module, post
from lauren.testing import TestClient


@dataclasses.dataclass
class Item:
    name: str
    price: float
    qty: int = 1


class Address(TypedDict):
    street: str
    city: str


@controller("/items")
class ItemController:
    @post("/")
    async def create_item(self, body: Item) -> dict:
        return {"name": body.name, "price": body.price, "qty": body.qty}

    @get("/")
    async def list_items(self) -> list:
        return [{"name": "pen", "price": 1.0, "qty": 10}]


@controller("/addresses")
class AddressController:
    @post("/")
    async def create_address(self, body: Address) -> dict:
        return {"city": body["city"]}


@module(controllers=[ItemController, AddressController])
class Phase3Module:
    pass


@pytest.fixture(scope="module")
def client():
    return TestClient(LaurenFactory.create(Phase3Module, openapi_url="/openapi.json"))


class TestDataclassBodyHTTP:
    def test_post_item_200(self, client):
        resp = client.post("/items/", json={"name": "ruler", "price": 0.5})
        assert resp.status_code == 200

    def test_post_item_response_has_correct_fields(self, client):
        resp = client.post("/items/", json={"name": "ruler", "price": 0.5})
        data = resp.json()
        assert data["name"] == "ruler"
        assert data["price"] == 0.5
        assert data["qty"] == 1

    def test_post_item_missing_field_422(self, client):
        resp = client.post("/items/", json={"price": 0.5})
        assert resp.status_code == 422

    def test_post_item_422_detail_mentions_missing_field(self, client):
        resp = client.post("/items/", json={"price": 0.5})
        assert "name" in resp.text


class TestTypedDictBodyHTTP:
    def test_post_address_200(self, client):
        resp = client.post("/addresses/", json={"street": "1 Main St", "city": "NYC"})
        assert resp.status_code == 200
        assert resp.json()["city"] == "NYC"

    def test_post_address_missing_required_422(self, client):
        resp = client.post("/addresses/", json={"street": "1 Main St"})
        assert resp.status_code == 422


class TestResponseSerializationHTTP:
    def test_list_response_works(self, client):
        resp = client.get("/items/")
        assert resp.status_code == 200
        items = resp.json()
        assert isinstance(items, list)
        assert items[0]["name"] == "pen"


class TestOpenAPIHTTP:
    def test_openapi_json_returns_200(self, client):
        assert client.get("/openapi.json").status_code == 200

    def test_openapi_json_has_item_schema(self, client):
        spec = client.get("/openapi.json").json()
        schemas = spec.get("components", {}).get("schemas", {})
        assert "Item" in schemas

    def test_openapi_json_has_address_schema(self, client):
        spec = client.get("/openapi.json").json()
        schemas = spec.get("components", {}).get("schemas", {})
        assert "Address" in schemas

    def test_item_schema_has_correct_properties(self, client):
        spec = client.get("/openapi.json").json()
        item_schema = spec["components"]["schemas"]["Item"]
        assert "name" in item_schema.get("properties", {})
        assert "price" in item_schema.get("properties", {})


class TestPydanticRegressionHTTP:
    """Verify pydantic BaseModel endpoints have zero regression."""

    @pytest.fixture(scope="class")
    def pydantic_client(self):
        pytest.importorskip("pydantic")
        from pydantic import BaseModel

        class PydItem(BaseModel):
            name: str
            price: float

        @controller("/pyd-items")
        class PydController:
            @post("/")
            async def create(self, body: PydItem) -> dict:
                return {"name": body.name, "price": body.price}

        @module(controllers=[PydController])
        class PydModule:
            pass

        return TestClient(LaurenFactory.create(PydModule))

    def test_pydantic_basemodel_body_still_works(self, pydantic_client):
        resp = pydantic_client.post("/pyd-items/", json={"name": "widget", "price": 9.99})
        assert resp.status_code == 200
        assert resp.json()["name"] == "widget"

    def test_pydantic_422_on_bad_type(self, pydantic_client):
        resp = pydantic_client.post("/pyd-items/", json={"name": "x", "price": "bad"})
        assert resp.status_code == 422
