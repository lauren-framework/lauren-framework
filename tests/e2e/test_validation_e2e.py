"""Phase 2 e2e tests: _validation module via full HTTP round-trips."""

from __future__ import annotations

import dataclasses
from typing import TypedDict

import pytest

from lauren import Json, LaurenFactory, controller, module, post
from lauren.testing import TestClient


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
