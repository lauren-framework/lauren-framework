"""Full regression test when pydantic IS installed."""

import pytest

pydantic = pytest.importorskip("pydantic")
from pydantic import BaseModel, Field  # noqa: E402
from typing import Annotated, Literal, Union  # noqa: E402


class Item(BaseModel):
    name: str
    price: float
    qty: int = 1


class Cat(BaseModel):
    kind: Literal["cat"] = "cat"
    name: str


class Dog(BaseModel):
    kind: Literal["dog"] = "dog"
    name: str


Animal = Annotated[Union[Cat, Dog], Field(discriminator="kind")]


@pytest.fixture(scope="module")
def app():
    from lauren import Lauren, Json

    app = Lauren()

    @app.post("/items")
    async def create(body: Item) -> Item:
        return body

    @app.post("/animals")
    async def create_animal(body: Json[Animal]) -> dict:
        return {"type": type(body).__name__.lower(), "name": body.name}

    return app


@pytest.fixture(scope="module")
def client(app):
    from lauren.testing import TestClient

    return TestClient(app)


class TestPydanticBodyValidation:
    def test_valid_body(self, client):
        resp = client.post("/items", json={"name": "apple", "price": 1.5})
        assert resp.status_code == 200
        assert resp.json()["name"] == "apple"

    def test_missing_required_field(self, client):
        resp = client.post("/items", json={"price": 1.5})
        assert resp.status_code == 422

    def test_wrong_type(self, client):
        resp = client.post("/items", json={"name": "apple", "price": "not-a-number"})
        assert resp.status_code == 422


class TestPydanticDiscriminatedUnion:
    def test_cat_routes_correctly(self, client):
        resp = client.post("/animals", json={"kind": "cat", "name": "Mittens"})
        assert resp.status_code == 200
        assert resp.json()["type"] == "cat"

    def test_dog_routes_correctly(self, client):
        resp = client.post("/animals", json={"kind": "dog", "name": "Rex"})
        assert resp.status_code == 200
        assert resp.json()["type"] == "dog"


class TestPydanticEncoderBackwardsCompat:
    def test_pydantic_encoder_importable(self):
        from lauren.serialization import PydanticEncoder

        assert PydanticEncoder is not None

    def test_pydantic_encoder_encodes_basemodel(self):
        from lauren.serialization import PydanticEncoder

        enc = PydanticEncoder()
        result = enc.encode(Item(name="apple", price=1.5))
        import json

        data = json.loads(result)
        assert data["name"] == "apple"
