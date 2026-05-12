"""Integration tests for the pluggable search engine layer (Skill 15).

Uses InMemorySearchIndex — no Elasticsearch or Meilisearch required.
All tests go through the TestClient / HTTP layer.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from lauren import (
    LaurenFactory,
    Json,
    Path,
    Query,
    controller,
    get,
    module,
    post,
    use_value,
)
from lauren import delete as http_delete
from lauren.testing import TestClient
from pydantic import BaseModel


# ---------------------------------------------------------------------------
# Abstract interface + in-memory implementation
# ---------------------------------------------------------------------------


class SearchIndex(ABC):
    @abstractmethod
    def index(self, index_name: str, doc_id: str, body: dict) -> None: ...

    @abstractmethod
    def search(self, index_name: str, query: str) -> list[dict]: ...

    @abstractmethod
    def delete(self, index_name: str, doc_id: str) -> None: ...


class InMemorySearchIndex(SearchIndex):
    def __init__(self) -> None:
        self._store: dict[str, dict[str, dict]] = {}

    def index(self, index_name: str, doc_id: str, body: dict) -> None:
        self._store.setdefault(index_name, {})[doc_id] = body

    def search(self, index_name: str, query: str) -> list[dict]:
        docs = self._store.get(index_name, {}).values()
        q = query.lower()
        return [d for d in docs if any(q in str(v).lower() for v in d.values())]

    def delete(self, index_name: str, doc_id: str) -> None:
        self._store.get(index_name, {}).pop(doc_id, None)


# ---------------------------------------------------------------------------
# Controller
# ---------------------------------------------------------------------------


class IndexDocBody(BaseModel):
    doc_id: str
    data: dict


@controller("/index")
class SearchEngineController:
    def __init__(self, idx: SearchIndex) -> None:
        self._idx = idx

    @post("/{index_name}")
    async def index_document(self, index_name: Path[str], body: Json[IndexDocBody]) -> dict:
        self._idx.index(index_name, body.doc_id, body.data)
        return {"indexed": body.doc_id, "index": index_name}

    @get("/{index_name}/search")
    async def search(self, index_name: Path[str], q: Query[str] = "") -> list:
        return self._idx.search(index_name, q)

    @http_delete("/{index_name}/{doc_id}")
    async def delete_document(self, index_name: Path[str], doc_id: Path[str]) -> dict:
        self._idx.delete(index_name, doc_id)
        return {"deleted": doc_id, "index": index_name}


# ---------------------------------------------------------------------------
# Module factory (creates a fresh in-memory index per build_app() call)
# ---------------------------------------------------------------------------


def build_app() -> TestClient:
    fresh_index = InMemorySearchIndex()

    @module(
        controllers=[SearchEngineController],
        providers=[use_value(provide=SearchIndex, value=fresh_index)],
    )
    class SearchModule:
        pass

    return TestClient(LaurenFactory.create(SearchModule))


# ---------------------------------------------------------------------------
# Tests — all through TestClient
# ---------------------------------------------------------------------------


class TestSearchEngineIntegration:
    def test_index_and_search(self) -> None:
        client = build_app()
        client.post(
            "/index/docs",
            json={"doc_id": "1", "data": {"title": "Python tutorial", "lang": "en"}},
        )
        r = client.get("/index/docs/search?q=python")
        assert r.status_code == 200
        results = r.json()
        assert len(results) == 1
        assert results[0]["title"] == "Python tutorial"

    def test_search_returns_empty_when_no_match(self) -> None:
        client = build_app()
        client.post("/index/docs", json={"doc_id": "1", "data": {"title": "Python tutorial"}})
        r = client.get("/index/docs/search?q=golang")
        assert r.json() == []

    def test_search_is_case_insensitive(self) -> None:
        client = build_app()
        client.post("/index/docs", json={"doc_id": "1", "data": {"title": "PYTHON Guide"}})
        r = client.get("/index/docs/search?q=python")
        assert len(r.json()) == 1

    def test_delete_removes_document(self) -> None:
        client = build_app()
        client.post("/index/docs", json={"doc_id": "1", "data": {"title": "To be deleted"}})
        client.delete("/index/docs/1")
        r = client.get("/index/docs/search?q=deleted")
        assert r.json() == []

    def test_delete_nonexistent_is_safe(self) -> None:
        client = build_app()
        r = client.delete("/index/docs/nonexistent")
        assert r.status_code == 200

    def test_multiple_indexes_are_isolated(self) -> None:
        client = build_app()
        client.post(
            "/index/articles",
            json={"doc_id": "a1", "data": {"title": "Article about Python"}},
        )
        client.post("/index/products", json={"doc_id": "p1", "data": {"name": "Python book"}})
        articles = client.get("/index/articles/search?q=python").json()
        products = client.get("/index/products/search?q=python").json()
        assert len(articles) == 1
        assert articles[0]["title"] == "Article about Python"
        assert len(products) == 1

    def test_index_returns_doc_id(self) -> None:
        client = build_app()
        r = client.post("/index/test", json={"doc_id": "doc-42", "data": {"x": "value"}})
        assert r.status_code == 200
        assert r.json()["indexed"] == "doc-42"
