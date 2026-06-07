"""Integration tests: generate_openapi() for dataclass / TypedDict / Discriminated models."""

import dataclasses
from typing import Literal, TypedDict

import pytest


@dataclasses.dataclass
class Item:
    """A shop item."""

    name: str
    price: float
    qty: int = 1


class DepositEvent(TypedDict):
    event: Literal["deposit"]
    amount: float


class WithdrawEvent(TypedDict):
    event: Literal["withdraw"]
    amount: float


@pytest.fixture()
def client():
    from lauren import Lauren, Json, Discriminated
    from lauren.testing import TestClient

    _app = Lauren()

    @_app.post("/items")
    async def create_item(body: Item) -> Item:
        return body

    @_app.get("/items")
    async def list_items() -> list[Item]:
        return []

    @_app.post("/events")
    async def handle_event(
        body: Json[Discriminated[DepositEvent | WithdrawEvent, "event"]],  # noqa: F821
    ) -> dict:
        return {}

    return TestClient(_app)


def _get_spec(client):
    return client.get("/openapi.json").json()


class TestDataclassSchema:
    def test_item_in_components(self, client):
        assert "Item" in _get_spec(client)["components"]["schemas"]

    def test_item_schema_has_correct_type(self, client):
        schema = _get_spec(client)["components"]["schemas"]["Item"]
        assert schema["type"] == "object"

    def test_item_schema_has_properties(self, client):
        props = _get_spec(client)["components"]["schemas"]["Item"]["properties"]
        assert "name" in props
        assert "price" in props
        assert "qty" in props

    def test_item_required_excludes_default(self, client):
        schema = _get_spec(client)["components"]["schemas"]["Item"]
        assert "name" in schema["required"]
        assert "price" in schema["required"]
        assert "qty" not in schema.get("required", [])

    def test_item_description_from_docstring(self, client):
        schema = _get_spec(client)["components"]["schemas"]["Item"]
        assert schema.get("description") == "A shop item."

    def test_post_items_request_ref(self, client):
        spec = _get_spec(client)
        body_schema = spec["paths"]["/items"]["post"]["requestBody"]["content"]["application/json"]["schema"]
        assert body_schema == {"$ref": "#/components/schemas/Item"}

    def test_post_items_response_ref(self, client):
        spec = _get_spec(client)
        resp_schema = spec["paths"]["/items"]["post"]["responses"]["200"]["content"]["application/json"][
            "schema"
        ]
        assert resp_schema == {"$ref": "#/components/schemas/Item"}

    def test_get_items_response_array(self, client):
        spec = _get_spec(client)
        resp_schema = spec["paths"]["/items"]["get"]["responses"]["200"]["content"]["application/json"][
            "schema"
        ]
        assert resp_schema["type"] == "array"
        assert resp_schema["items"] == {"$ref": "#/components/schemas/Item"}


class TestDiscriminatedSchema:
    def test_events_oneof(self, client):
        spec = _get_spec(client)
        body_schema = spec["paths"]["/events"]["post"]["requestBody"]["content"]["application/json"]["schema"]
        assert "oneOf" in body_schema
        assert len(body_schema["oneOf"]) == 2

    def test_events_discriminator_property(self, client):
        spec = _get_spec(client)
        body_schema = spec["paths"]["/events"]["post"]["requestBody"]["content"]["application/json"]["schema"]
        assert body_schema["discriminator"]["propertyName"] == "event"

    def test_variant_schemas_in_components(self, client):
        schemas = _get_spec(client)["components"]["schemas"]
        assert "DepositEvent" in schemas
        assert "WithdrawEvent" in schemas


class TestSchemaWithoutPydantic:
    def test_generates_spec_without_pydantic(self, monkeypatch):
        import sys

        monkeypatch.setitem(sys.modules, "pydantic", None)
        from lauren import Lauren  # noqa: PLC0415
        from lauren.testing import TestClient  # noqa: PLC0415

        @dataclasses.dataclass
        class Item2:
            name: str
            value: int

        _app = Lauren()

        @_app.post("/item2s")
        async def create_item2(body: Item2) -> Item2:
            return body

        spec = TestClient(_app).get("/openapi.json").json()
        assert "Item2" in spec["components"]["schemas"]
        assert spec["components"]["schemas"]["Item2"]["type"] == "object"
