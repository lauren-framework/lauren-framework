"""Integration: validation does no type reflection on the request path (Golden Rule 1)."""

from __future__ import annotations

import dataclasses
import inspect
import typing
from typing import Annotated, Any, Literal

import pytest

import lauren._typing.forwardref as forwardref
from lauren import Discriminated, Form, Json, LaurenFactory, Query, controller, get, module, post
from lauren.testing import TestClient
from lauren.validation import Le, ValidationPolicy


@dataclasses.dataclass
class Filters:
    q: str
    page: int = 1
    tags: list[str] = dataclasses.field(default_factory=list)


@dataclasses.dataclass
class Body:
    name: str
    age: Annotated[int, Le(150)]


@dataclasses.dataclass
class Cat:
    kind: Literal["cat"]


@controller("/r")
class ReflectionFreeController:
    @get("/q")
    async def query(self, f: Query[Filters], n: int = 0, flag: bool = False) -> dict:
        return {"page": f.page}

    @post("/body")
    async def body(self, body: Json[Body]) -> dict:
        return {"age": body.age}

    @post("/pet")
    async def pet(self, body: Json[Discriminated[Cat, "kind"]]) -> dict:  # noqa: F821
        return {"type": type(body).__name__}

    @post("/form")
    async def form(self, body: Form[Filters]) -> dict:
        return {"type": type(body).__name__}


@module(controllers=[ReflectionFreeController])
class ReflectionFreeModule:
    pass


@pytest.mark.parametrize("mode", ["legacy", "warn", "enforce"])
def test_requests_never_resolve_type_hints(monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    client = TestClient(
        LaurenFactory.create(ReflectionFreeModule, validation=ValidationPolicy(stdlib_models=mode))
    )
    client.get("/r/q?q=warmup")

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("reflection on the request path")

    monkeypatch.setattr(typing, "get_type_hints", forbidden)
    monkeypatch.setattr(inspect, "signature", forbidden)
    monkeypatch.setattr(forwardref, "resolve_type_hints", forbidden)

    form = {"content-type": "application/x-www-form-urlencoded"}
    assert client.get("/r/q?q=x&page=2&n=3&flag=on&tags=a").status_code == 200
    assert client.get("/r/q?page=x&n=y").status_code == 422
    assert client.post("/r/body", json={"name": "a", "age": "2"}).status_code == 200
    assert client.post("/r/body", json={"name": "a", "age": 200}).status_code == 422
    assert client.post("/r/pet", json={"kind": "cat"}).json() == {"type": "Cat"}
    assert client.post("/r/form", content=b"q=x&page=2", headers=form).status_code == 200
