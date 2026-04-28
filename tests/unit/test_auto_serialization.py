"""Tests for automatic handler-return serialization."""

from __future__ import annotations

import dataclasses
import datetime
import enum
import uuid
from decimal import Decimal

import pytest
from pydantic import BaseModel

from lauren import (
    LaurenFactory,
    Response,
    controller,
    get,
    module,
    post,
)
from lauren.testing import TestClient


class Color(enum.Enum):
    RED = "red"
    BLUE = "blue"


class UserOut(BaseModel):
    id: int
    name: str
    joined: datetime.datetime
    color: Color


@dataclasses.dataclass
class Point:
    x: int
    y: int


@controller("/serialize")
class SerializeController:
    # ---------- Primitives ------------------------------------------------
    @get("/str")
    async def return_str(self) -> str:
        return "hello"

    @get("/int")
    async def return_int(self) -> int:
        return 42

    @get("/bool")
    async def return_bool(self) -> bool:
        return True

    @get("/none")
    async def return_none(self) -> None:
        return None

    @get("/bytes")
    async def return_bytes(self) -> bytes:
        return b"\x01\x02\x03"

    # ---------- Collections ----------------------------------------------
    @get("/dict")
    async def return_dict(self) -> dict:
        return {"ok": True, "count": 7}

    @get("/list")
    async def return_list(self) -> list:
        return [1, 2, 3]

    # ---------- Pydantic --------------------------------------------------
    @get("/model")
    async def return_model(self):
        return UserOut(
            id=1,
            name="Alice",
            joined=datetime.datetime(2024, 1, 1, 12, 0, 0),
            color=Color.RED,
        )

    @get("/models")
    async def return_models(self):
        return [
            UserOut(
                id=1,
                name="Alice",
                joined=datetime.datetime(2024, 1, 1, 12, 0, 0),
                color=Color.RED,
            ),
            UserOut(
                id=2,
                name="Bob",
                joined=datetime.datetime(2024, 1, 2, 12, 0, 0),
                color=Color.BLUE,
            ),
        ]

    # ---------- Dataclass -------------------------------------------------
    @get("/dataclass")
    async def return_dataclass(self):
        return Point(x=1, y=2)

    # ---------- Tuple form: (body, status) -------------------------------
    @post("/created")
    async def create_with_status(self):
        return {"id": 99, "name": "new"}, 201

    @post("/accepted")
    async def accepted_with_headers(self):
        return {"status": "queued"}, 202, {"x-queue": "jobs"}

    # ---------- Raw Response still honored --------------------------------
    @get("/custom")
    async def custom(self) -> Response:
        return Response.text("raw").with_header("x-custom", "1")

    # ---------- Rich types in dicts --------------------------------------
    @get("/rich")
    async def rich(self):
        return {
            "color": Color.RED,
            "when": datetime.datetime(2024, 6, 1, 10, 30),
            "uuid": uuid.UUID("12345678-1234-5678-1234-567812345678"),
            "amount": Decimal("3.14"),
            "tags": {"a", "b"},
        }


@module(controllers=[SerializeController])
class SerializeModule:
    pass


@pytest.fixture(scope="module")
def client():
    app = LaurenFactory.create(SerializeModule)
    return TestClient(app)


class TestPrimitives:
    def test_string(self, client):
        r = client.get("/serialize/str")
        assert r.status_code == 200
        assert r.text == "hello"
        assert "text/plain" in (r.header("content-type") or "")

    def test_int(self, client):
        assert client.get("/serialize/int").json() == 42

    def test_bool(self, client):
        assert client.get("/serialize/bool").json() is True

    def test_none(self, client):
        r = client.get("/serialize/none")
        assert r.status_code == 204

    def test_bytes(self, client):
        r = client.get("/serialize/bytes")
        assert r.body == b"\x01\x02\x03"


class TestCollections:
    def test_dict(self, client):
        assert client.get("/serialize/dict").json() == {"ok": True, "count": 7}

    def test_list(self, client):
        assert client.get("/serialize/list").json() == [1, 2, 3]


class TestPydantic:
    def test_model(self, client):
        body = client.get("/serialize/model").json()
        assert body["id"] == 1
        assert body["name"] == "Alice"
        # model_dump(mode="json") serializes datetime to ISO string
        assert body["joined"].startswith("2024-01-01")
        # Enum serialized to its value
        assert body["color"] == "red"

    def test_list_of_models(self, client):
        body = client.get("/serialize/models").json()
        assert isinstance(body, list)
        assert len(body) == 2
        assert {m["name"] for m in body} == {"Alice", "Bob"}


class TestDataclass:
    def test_dataclass_instance(self, client):
        assert client.get("/serialize/dataclass").json() == {"x": 1, "y": 2}


class TestTupleForm:
    def test_body_and_status(self, client):
        r = client.post("/serialize/created")
        assert r.status_code == 201
        assert r.json() == {"id": 99, "name": "new"}

    def test_body_status_headers(self, client):
        r = client.post("/serialize/accepted")
        assert r.status_code == 202
        assert r.header("x-queue") == "jobs"


class TestCustomResponse:
    def test_raw_response_passes_through(self, client):
        r = client.get("/serialize/custom")
        assert r.text == "raw"
        assert r.header("x-custom") == "1"


class TestRichTypesInDicts:
    def test_rich_dict(self, client):
        body = client.get("/serialize/rich").json()
        assert body["color"] == "red"
        assert body["when"] == "2024-06-01T10:30:00"
        assert body["uuid"] == "12345678-1234-5678-1234-567812345678"
        assert body["amount"] == "3.14"
        assert sorted(body["tags"]) == ["a", "b"]
