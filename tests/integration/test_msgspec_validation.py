"""Integration: ``msgspec.Struct`` bodies decode once and report structured issues."""

from __future__ import annotations

from typing import Annotated, Any

import pytest

msgspec = pytest.importorskip("msgspec")

from lauren import Json, LaurenFactory, Query, controller, get, module, post  # noqa: E402
from lauren.testing import TestClient  # noqa: E402


class Item(msgspec.Struct):
    name: str
    qty: Annotated[int, msgspec.Meta(ge=0)] = 0
    tags: list[str] = []


@controller("/m")
class StructEndpoints:
    @post("/body")
    async def body(self, body: Json[Item]) -> dict:
        return {"name": body.name, "qty": body.qty}

    @get("/query")
    async def query(self, f: Query[Item]) -> dict:
        return {"qty": f.qty, "tags": f.tags}


@module(controllers=[StructEndpoints])
class StructModule:
    pass


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(LaurenFactory.create(StructModule))


def test_struct_bodies_are_decoded_once(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    import lauren._schema._params as params

    def fail(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("the stdlib JSON parser must not run for msgspec bodies")

    monkeypatch.setattr(params._jsonlib, "loads", fail)

    assert client.post("/m/body", json={"name": "a", "qty": 2}).json() == {"name": "a", "qty": 2}


def test_struct_errors_are_translated(client: TestClient) -> None:
    error = client.post("/m/body", json={"name": "a", "qty": -1}).json()["error"]

    assert error["detail"]["errors"] == ["Expected `int` >= 0 - at `$.qty`"]
    assert error["detail"]["issues"] == [
        {
            "loc": ["body", "qty"],
            "type": "greater_than_equal",
            "msg": "Input should be greater than or equal to 0",
            "ctx": {"ge": 0},
        }
    ]


def test_malformed_json_is_a_json_invalid_issue(client: TestClient) -> None:
    error = client.post("/m/body", content=b"{bad", headers={"content-type": "application/json"}).json()[
        "error"
    ]

    assert error["message"].startswith("invalid JSON")
    assert error["detail"]["issues"][0]["type"] == "json_invalid"


def test_query_structs_read_repeated_keys(client: TestClient) -> None:
    assert client.get("/m/query?name=a&qty=4&tags=x").json() == {"qty": 4, "tags": ["x"]}
    issues = client.get("/m/query?name=a&qty=-4").json()["error"]["detail"]["issues"]
    assert issues[0]["loc"] == ["query", "qty"]
