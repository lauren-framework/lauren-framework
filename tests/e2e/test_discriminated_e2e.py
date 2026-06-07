"""E2E tests: Discriminated union endpoint via TestClient."""

import dataclasses
from typing import Literal, TypedDict

import pytest

from lauren import Discriminated


@dataclasses.dataclass
class CatVariant:
    kind: Literal["cat"] = "cat"
    name: str = ""
    indoor: bool = True


@dataclasses.dataclass
class DogVariant:
    kind: Literal["dog"] = "dog"
    name: str = ""
    breed: str = "mixed"


class DepositEvent(TypedDict):
    event: Literal["deposit"]
    amount: float


class WithdrawEvent(TypedDict):
    event: Literal["withdraw"]
    amount: float


@pytest.fixture(scope="module")
def client():
    from lauren import Lauren, Json
    from lauren.testing import TestClient

    app = Lauren()

    @app.post("/animals")
    async def create_animal(body: Json[Discriminated[CatVariant | DogVariant, "kind"]]) -> dict:  # noqa: F821
        return {"type": type(body).__name__, "name": body.name}

    @app.post("/banking/events")
    async def handle_event(body: Json[Discriminated[DepositEvent | WithdrawEvent, "event"]]) -> dict:  # noqa: F821
        return {"event": body["event"], "amount": body["amount"]}

    return TestClient(app)


class TestDiscriminatedDataclassE2E:
    def test_cat_routes_correctly(self, client):
        resp = client.post("/animals", json={"kind": "cat", "name": "Mittens"})
        assert resp.status_code == 200
        assert resp.json()["type"] == "CatVariant"
        assert resp.json()["name"] == "Mittens"

    def test_dog_routes_correctly(self, client):
        resp = client.post("/animals", json={"kind": "dog", "name": "Rex", "breed": "lab"})
        assert resp.status_code == 200
        assert resp.json()["type"] == "DogVariant"

    def test_unknown_kind_returns_422(self, client):
        resp = client.post("/animals", json={"kind": "fish"})
        assert resp.status_code == 422

    def test_missing_kind_field_returns_422(self, client):
        resp = client.post("/animals", json={"name": "Ghost"})
        assert resp.status_code == 422

    def test_422_error_includes_discriminator_key_name(self, client):
        resp = client.post("/animals", json={"name": "Ghost"})
        assert "kind" in resp.text

    def test_422_error_includes_valid_tag_values(self, client):
        resp = client.post("/animals", json={"kind": "fish"})
        assert "cat" in resp.text or "dog" in resp.text


class TestDiscriminatedTypedDictE2E:
    def test_deposit_event(self, client):
        resp = client.post("/banking/events", json={"event": "deposit", "amount": 100.0})
        assert resp.status_code == 200
        assert resp.json()["event"] == "deposit"
        assert resp.json()["amount"] == 100.0

    def test_withdraw_event(self, client):
        resp = client.post("/banking/events", json={"event": "withdraw", "amount": 25.0})
        assert resp.status_code == 200
        assert resp.json()["event"] == "withdraw"

    def test_invalid_event_type_returns_422(self, client):
        resp = client.post("/banking/events", json={"event": "transfer", "amount": 10.0})
        assert resp.status_code == 422


class TestDiscriminatedOpenAPIE2E:
    def test_animals_endpoint_in_openapi(self, client):
        spec = client.get("/openapi.json").json()
        assert "/animals" in spec["paths"]

    def test_animals_request_body_has_oneof(self, client):
        spec = client.get("/openapi.json").json()
        schema = spec["paths"]["/animals"]["post"]["requestBody"]["content"]["application/json"]["schema"]
        assert "oneOf" in schema
        assert len(schema["oneOf"]) == 2

    def test_animals_discriminator_property_name(self, client):
        spec = client.get("/openapi.json").json()
        schema = spec["paths"]["/animals"]["post"]["requestBody"]["content"]["application/json"]["schema"]
        assert schema["discriminator"]["propertyName"] == "kind"

    def test_variant_schemas_in_components(self, client):
        spec = client.get("/openapi.json").json()
        schemas = spec["components"]["schemas"]
        assert "CatVariant" in schemas
        assert "DogVariant" in schemas


class TestDiscriminatedMsgspecE2E:
    """Runs only when msgspec is installed."""

    @pytest.fixture(scope="class")
    def msgspec_client(self):
        pytest.importorskip("msgspec")
        import msgspec
        from lauren import Lauren, Json
        from lauren.testing import TestClient

        class MsgCat(msgspec.Struct, tag_field="kind", tag="cat"):
            name: str

        class MsgDog(msgspec.Struct, tag_field="kind", tag="dog"):
            name: str

        app = Lauren()

        @app.post("/msg-animals")
        async def create(body: Json[Discriminated[MsgCat | MsgDog, "kind"]]) -> dict:  # noqa: F821
            return {"type": type(body).__name__, "name": body.name}

        return TestClient(app)

    def test_msgspec_cat_routes(self, msgspec_client):
        resp = msgspec_client.post("/msg-animals", json={"kind": "cat", "name": "Luna"})
        assert resp.status_code == 200
        assert resp.json()["name"] == "Luna"
