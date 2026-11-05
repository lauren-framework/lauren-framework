"""Phase 2 e2e tests: _validation module via full HTTP round-trips."""

from __future__ import annotations

import dataclasses
from typing import Literal, TypedDict

import pytest

from lauren import Discriminated, Json, LaurenFactory, controller, module, post
from lauren.testing import TestClient
from lauren.validation import ValidationPolicy


# ---------------------------------------------------------------------------
# Domain types
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class Book:
    title: str
    author: str
    pages: int = 0


@dataclasses.dataclass
class NestedOrder:
    item: str
    book: Book


class SearchQuery(TypedDict):
    q: str
    limit: int


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------


@controller("/v")
class ValidationController:
    @post("/book")
    async def create_book(self, body: Json[Book]) -> dict:
        return {"title": body.title, "author": body.author, "pages": body.pages}

    @post("/search")
    async def search(self, body: Json[SearchQuery]) -> dict:
        return {"q": body["q"], "limit": body["limit"]}


@module(controllers=[ValidationController])
class ValidationModule:
    pass


@pytest.fixture(scope="module")
def client():
    return TestClient(LaurenFactory.create(ValidationModule, openapi_url="/openapi.json"))


# ---------------------------------------------------------------------------
# Dataclass e2e
# ---------------------------------------------------------------------------


class TestBookEndpoint:
    def test_full_payload(self, client):
        resp = client.post("/v/book", json={"title": "Dune", "author": "Herbert", "pages": 412})
        assert resp.status_code == 200
        body = resp.json()
        assert body["title"] == "Dune"
        assert body["pages"] == 412

    def test_default_pages(self, client):
        resp = client.post("/v/book", json={"title": "1984", "author": "Orwell"})
        assert resp.status_code == 200
        assert resp.json()["pages"] == 0

    def test_missing_author_422(self, client):
        resp = client.post("/v/book", json={"title": "NoAuthor"})
        assert resp.status_code == 422

    def test_missing_title_422(self, client):
        resp = client.post("/v/book", json={"author": "Someone"})
        assert resp.status_code == 422

    def test_empty_body_422(self, client):
        resp = client.post("/v/book", json={})
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# TypedDict e2e
# ---------------------------------------------------------------------------


class TestSearchEndpoint:
    def test_valid_search(self, client):
        resp = client.post("/v/search", json={"q": "python", "limit": 10})
        assert resp.status_code == 200
        assert resp.json() == {"q": "python", "limit": 10}

    def test_missing_q_422(self, client):
        resp = client.post("/v/search", json={"limit": 5})
        assert resp.status_code == 422

    def test_missing_limit_422(self, client):
        resp = client.post("/v/search", json={"q": "go"})
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# OpenAPI schema inclusion
# ---------------------------------------------------------------------------


class TestOpenAPISchema:
    def test_openapi_json_reachable(self, client):
        resp = client.get("/openapi.json")
        assert resp.status_code == 200

    def test_book_schema_in_openapi(self, client):
        openapi = client.get("/openapi.json").json()
        paths = openapi.get("paths", {})
        assert "/v/book" in paths or any("/v/book" in p for p in paths)


# ---------------------------------------------------------------------------
# Native validation under ValidationPolicy(stdlib_models="enforce") (F02)
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class Card:
    method: Literal["card"]
    last4: str


@dataclasses.dataclass
class Cash:
    method: Literal["cash"]
    amount: float


def _enforced_app(*, with_pydantic: bool) -> TestClient:
    @controller("/e")
    class EnforcedController:
        @post("/order")
        async def order(self, body: Json[NestedOrder]) -> dict:
            return {"book_type": type(body.book).__name__, "pages": body.book.pages}

        @post("/search")
        async def search(self, body: Json[SearchQuery]) -> dict:
            return dict(body)

        @post("/pay")
        async def pay(self, body: Json[Discriminated[Card | Cash, "method"]]) -> dict:  # noqa: F821
            return {"type": type(body).__name__}

    controllers: list[type] = [EnforcedController]
    if with_pydantic:
        import pydantic

        class Review(pydantic.BaseModel):
            stars: int = pydantic.Field(ge=1, le=5)
            text: str

        @controller("/p")
        class PydanticController:
            @post("/review")
            async def review(self, body: Json[Review]) -> dict:
                return body.model_dump()

        controllers.append(PydanticController)

    @module(controllers=controllers)
    class EnforcedModule:
        pass

    return TestClient(
        LaurenFactory.create(EnforcedModule, validation=ValidationPolicy(stdlib_models="enforce"))
    )


def _issues(response) -> list[tuple[list, str]]:
    assert response.status_code == 422
    return [(i["loc"], i["type"]) for i in response.json()["error"]["detail"]["issues"]]


STDLIB_CASES = [
    (
        "/e/order",
        {"item": "x", "book": {"title": "T", "author": "A", "pages": "many"}},
        [(["body", "book", "pages"], "int_parsing")],
    ),
    (
        "/e/order",
        {"book": {"title": 1}},
        [
            (["body", "item"], "missing"),
            (["body", "book", "title"], "string_type"),
            (["body", "book", "author"], "missing"),
        ],
    ),
    ("/e/search", {"q": "x", "limit": "ten"}, [(["body", "limit"], "int_parsing")]),
    ("/e/pay", {"method": "card"}, [(["body", "card", "last4"], "missing")]),
    ("/e/pay", {"method": "cheque"}, [(["body"], "union_tag_invalid")]),
]


@pytest.mark.parametrize(("path", "payload", "expected"), STDLIB_CASES)
def test_enforced_stdlib_issues(path: str, payload: dict, expected: list) -> None:
    assert _issues(_enforced_app(with_pydantic=False).post(path, json=payload)) == expected


@pytest.mark.parametrize(("path", "payload", "expected"), STDLIB_CASES)
def test_enforced_stdlib_issues_without_pydantic(
    monkeypatch: pytest.MonkeyPatch, _preload_lauren: None, path: str, payload: dict, expected: list
) -> None:
    import sys

    import lauren.streaming

    monkeypatch.setitem(sys.modules, "pydantic", None)  # type: ignore[arg-type]
    monkeypatch.setattr(lauren.streaming, "_PYDANTIC_AVAILABLE", False)

    assert _issues(_enforced_app(with_pydantic=False).post(path, json=payload)) == expected


def test_enforced_nested_models_are_instances() -> None:
    response = _enforced_app(with_pydantic=False).post(
        "/e/order", json={"item": "x", "book": {"title": "T", "author": "A", "pages": "3"}}
    )

    assert response.json() == {"book_type": "Book", "pages": 3}


def test_pydantic_issues_share_the_shape() -> None:
    pytest.importorskip("pydantic")
    client = _enforced_app(with_pydantic=True)

    assert _issues(client.post("/p/review", json={"stars": 9})) == [
        (["body", "stars"], "less_than_equal"),
        (["body", "text"], "missing"),
    ]
