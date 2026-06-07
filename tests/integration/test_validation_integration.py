"""Phase 2 integration tests: _validation.py wired into a live lauren App."""

from __future__ import annotations

import dataclasses
from typing import TypedDict

import pytest

from lauren import Json, LaurenFactory, controller, module, post
from lauren.testing import TestClient


# ---------------------------------------------------------------------------
# Sample types
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class CreateItem:
    name: str
    qty: int = 1


class CreateItemTD(TypedDict):
    name: str
    qty: int


@dataclasses.dataclass
class NestedAddress:
    street: str


@dataclasses.dataclass
class CreateUserWithAddress:
    username: str
    address: NestedAddress


# ---------------------------------------------------------------------------
# App fixtures
# ---------------------------------------------------------------------------


@controller("/items")
class ItemController:
    @post("/dc")
    async def create_dc(self, body: Json[CreateItem]) -> dict:
        return {"name": body.name, "qty": body.qty}

    @post("/td")
    async def create_td(self, body: Json[CreateItemTD]) -> dict:
        return dict(body)


@module(controllers=[ItemController])
class ItemModule:
    pass


@pytest.fixture(scope="module")
def client():
    return TestClient(LaurenFactory.create(ItemModule))


# ---------------------------------------------------------------------------
# Dataclass body
# ---------------------------------------------------------------------------


def test_dataclass_body_accepted(client):
    resp = client.post("/items/dc", json={"name": "Widget", "qty": 5})
    assert resp.status_code == 200
    assert resp.json() == {"name": "Widget", "qty": 5}


def test_dataclass_body_default_applied(client):
    resp = client.post("/items/dc", json={"name": "Gadget"})
    assert resp.status_code == 200
    assert resp.json()["qty"] == 1


def test_dataclass_body_missing_required_returns_422(client):
    resp = client.post("/items/dc", json={})
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# TypedDict body
# ---------------------------------------------------------------------------


def test_typeddict_body_accepted(client):
    resp = client.post("/items/td", json={"name": "Bolt", "qty": 100})
    assert resp.status_code == 200
    data = resp.json()
    assert data["name"] == "Bolt"


def test_typeddict_body_missing_required_returns_422(client):
    resp = client.post("/items/td", json={"qty": 10})
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# validate_as called directly (unit-level wiring check)
# ---------------------------------------------------------------------------


def test_validate_as_dataclass_via_api():
    from lauren._validation import validate_as

    result = validate_as(CreateItem, {"name": "X", "qty": 3})
    assert result.name == "X"
    assert result.qty == 3


def test_validate_as_typeddict_via_api():
    from lauren._validation import validate_as

    result = validate_as(CreateItemTD, {"name": "Y", "qty": 7})
    assert result == {"name": "Y", "qty": 7}


# ---------------------------------------------------------------------------
# json_schema_for round-trip
# ---------------------------------------------------------------------------


def test_json_schema_dataclass_round_trip():
    from lauren._validation import json_schema_for

    schema = json_schema_for(CreateItem)
    assert schema["type"] == "object"
    assert "name" in schema["properties"]
    assert schema["properties"]["name"] == {"type": "string"}


def test_json_schema_typeddict_round_trip():
    from lauren._validation import json_schema_for

    schema = json_schema_for(CreateItemTD)
    assert "name" in schema["properties"]
    assert "qty" in schema["properties"]
