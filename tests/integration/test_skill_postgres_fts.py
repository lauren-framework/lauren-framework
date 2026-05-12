"""Integration tests for PostgreSQL full-text search pattern (Skill 14).

Uses SQLite + LIKE fallback since PostgreSQL is not available in CI.
All tests go through the TestClient / HTTP layer.
"""

from __future__ import annotations

from sqlalchemy import Column, Integer, String, Text, create_engine, text
from sqlalchemy.orm import DeclarativeBase, Session

from lauren import (
    LaurenFactory,
    Json,
    Query,
    Scope,
    controller,
    get,
    injectable,
    module,
    post,
    post_construct,
)
from lauren.testing import TestClient
from pydantic import BaseModel


# ---------------------------------------------------------------------------
# ORM model
# ---------------------------------------------------------------------------


class Base(DeclarativeBase):
    pass


class DocumentModel(Base):
    __tablename__ = "documents"
    id = Column(Integer, primary_key=True, autoincrement=True)
    title = Column(String(255), nullable=False)
    body = Column(Text, nullable=False)


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


@injectable(scope=Scope.SINGLETON)
class SearchService:
    def __init__(self) -> None:
        self._engine = create_engine("sqlite:///:memory:")

    @post_construct
    async def setup(self) -> None:
        Base.metadata.create_all(self._engine)

    def index_document(self, title: str, body: str) -> dict:
        with Session(self._engine) as s:
            doc = DocumentModel(title=title, body=body)
            s.add(doc)
            s.commit()
            s.refresh(doc)
            return {"id": doc.id, "title": doc.title}

    def search(self, query: str) -> list[dict]:
        with Session(self._engine) as s:
            rows = s.execute(
                text("SELECT id, title, body FROM documents WHERE title LIKE :q OR body LIKE :q"),
                {"q": f"%{query}%"},
            ).fetchall()
            return [{"id": r[0], "title": r[1], "body": r[2]} for r in rows]

    def count(self) -> int:
        with Session(self._engine) as s:
            return s.query(DocumentModel).count()


# ---------------------------------------------------------------------------
# Controller
# ---------------------------------------------------------------------------


class IndexDocumentBody(BaseModel):
    title: str
    body: str


@controller("/search")
class SearchController:
    def __init__(self, svc: SearchService) -> None:
        self._svc = svc

    @post("/documents")
    async def index_document(self, body: Json[IndexDocumentBody]) -> dict:
        return self._svc.index_document(body.title, body.body)

    @get("/documents")
    async def search_documents(self, q: Query[str] = "") -> list:
        return self._svc.search(q)

    @get("/documents/count")
    async def count(self) -> dict:
        return {"count": self._svc.count()}


@module(controllers=[SearchController], providers=[SearchService])
class SearchModule:
    pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def build_app() -> TestClient:
    return TestClient(LaurenFactory.create(SearchModule))


# ---------------------------------------------------------------------------
# Tests — all through TestClient
# ---------------------------------------------------------------------------


class TestPostgresFTS:
    def test_index_document_returns_id_and_title(self) -> None:
        client = build_app()
        r = client.post(
            "/search/documents",
            json={"title": "Python Guide", "body": "Learn Python fast"},
        )
        assert r.status_code == 200
        data = r.json()
        assert "id" in data
        assert data["title"] == "Python Guide"

    def test_search_by_title_keyword(self) -> None:
        client = build_app()
        client.post(
            "/search/documents",
            json={"title": "Python Guide", "body": "Learn Python fast"},
        )
        client.post(
            "/search/documents",
            json={"title": "Rust Handbook", "body": "Systems programming in Rust"},
        )
        r = client.get("/search/documents?q=Python")
        assert r.status_code == 200
        results = r.json()
        assert len(results) == 1
        assert results[0]["title"] == "Python Guide"

    def test_search_by_body_keyword(self) -> None:
        client = build_app()
        client.post(
            "/search/documents",
            json={"title": "Tech Article", "body": "All about asyncio event loops"},
        )
        r = client.get("/search/documents?q=asyncio")
        assert len(r.json()) == 1

    def test_search_no_match_returns_empty(self) -> None:
        client = build_app()
        client.post(
            "/search/documents",
            json={"title": "Python Guide", "body": "Learn Python fast"},
        )
        r = client.get("/search/documents?q=golang")
        assert r.json() == []

    def test_count_documents(self) -> None:
        client = build_app()
        client.post("/search/documents", json={"title": "Doc 1", "body": "content one"})
        client.post("/search/documents", json={"title": "Doc 2", "body": "content two"})
        r = client.get("/search/documents/count")
        assert r.json()["count"] == 2

    def test_multiple_results_for_common_keyword(self) -> None:
        client = build_app()
        client.post(
            "/search/documents",
            json={
                "title": "Intro to Databases",
                "body": "SQL and NoSQL databases explained",
            },
        )
        client.post(
            "/search/documents",
            json={
                "title": "Advanced Databases",
                "body": "Sharding and replication in databases",
            },
        )
        r = client.get("/search/documents?q=databases")
        assert len(r.json()) == 2
