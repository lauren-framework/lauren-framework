"""Integration tests that run with pydantic explicitly disabled."""

import sys
import dataclasses
from typing import Literal, TypedDict
import pytest


@pytest.fixture(autouse=True, scope="module")
def disable_pydantic():
    """Block pydantic for the entire module."""
    original = {k: v for k, v in sys.modules.items() if "pydantic" in k}
    for k in list(original.keys()):
        del sys.modules[k]
    sys.modules["pydantic"] = None  # type: ignore[assignment]
    sys.modules["pydantic_core"] = None  # type: ignore[assignment]
    yield
    for k in list(sys.modules.keys()):
        if "pydantic" in k:
            del sys.modules[k]
    sys.modules.update(original)


@dataclasses.dataclass
class Item:
    name: str
    price: float
    qty: int = 1


@dataclasses.dataclass
class ItemOut:
    id: int
    name: str


class EventDeposit(TypedDict):
    event: Literal["deposit"]
    amount: float


class EventWithdraw(TypedDict):
    event: Literal["withdraw"]
    amount: float


@pytest.fixture(scope="module")
def app(disable_pydantic):
    from lauren import Lauren, Json, Discriminated, StreamingResponse

    app = Lauren()

    @app.post("/items")
    async def create_item(body: Item) -> ItemOut:
        return ItemOut(id=1, name=body.name)

    @app.get("/items")
    async def list_items() -> list[Item]:
        return [Item(name="apple", price=1.5)]

    @app.post("/events")
    async def handle_event(body: Json[Discriminated[EventDeposit | EventWithdraw, "event"]]) -> dict:  # noqa: F821
        return {"event": body["event"]}

    @app.get("/stream")
    async def stream_items() -> StreamingResponse[Item]:
        async def gen():
            yield Item(name="a", price=1.0)
            yield Item(name="b", price=2.0)

        return StreamingResponse(gen())

    return app


@pytest.fixture(scope="module")
def client(app):
    from lauren.testing import TestClient

    return TestClient(app)


class TestDataclassEndpoints:
    def test_post_item_returns_200(self, client):
        resp = client.post("/items", json={"name": "apple", "price": 1.5})
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == 1
        assert data["name"] == "apple"

    def test_post_item_missing_required_field_returns_422(self, client):
        resp = client.post("/items", json={"price": 1.5})
        assert resp.status_code == 422
        assert "name" in resp.text

    def test_get_items_returns_list(self, client):
        resp = client.get("/items")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)


class TestDiscriminatedUnionEndpoint:
    def test_deposit_routes_correctly(self, client):
        resp = client.post("/events", json={"event": "deposit", "amount": 100.0})
        assert resp.status_code == 200
        assert resp.json()["event"] == "deposit"

    def test_withdraw_routes_correctly(self, client):
        resp = client.post("/events", json={"event": "withdraw", "amount": 50.0})
        assert resp.status_code == 200
        assert resp.json()["event"] == "withdraw"

    def test_unknown_event_returns_422(self, client):
        resp = client.post("/events", json={"event": "transfer", "amount": 10.0})
        assert resp.status_code == 422

    def test_missing_event_field_returns_422(self, client):
        resp = client.post("/events", json={"amount": 10.0})
        assert resp.status_code == 422


class TestOpenAPIWithoutPydantic:
    def test_openapi_endpoint_returns_200(self, client):
        resp = client.get("/openapi.json")
        assert resp.status_code == 200

    def test_item_in_components(self, client):
        spec = client.get("/openapi.json").json()
        assert "Item" in spec["components"]["schemas"]

    def test_item_schema_has_properties(self, client):
        spec = client.get("/openapi.json").json()
        item = spec["components"]["schemas"]["Item"]
        assert item["type"] == "object"
        assert "name" in item["properties"]
