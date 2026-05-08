---
name: postgres-fts
description: Implements full-text search using PostgreSQL tsvector/to_tsquery. Use when you need keyword search across document content with relevance ranking in a Lauren app. Falls back to LIKE for SQLite in tests.
---

> Use `codemap find "SymbolName"` to locate any symbol before reading — it gives
> exact file + line range and is faster than grep across the whole repo.

# PostgreSQL Full-Text Search Index Management

## Overview

PostgreSQL's built-in FTS uses `tsvector` columns and `to_tsquery` queries.
The `SearchService` singleton owns the SQLAlchemy engine and exposes
`index_document` and `search` methods. In production, a `GIN` index on the
`tsvector` column makes searches fast. In tests, SQLite `LIKE` provides a
compatible fallback.

## SQLAlchemy model with tsvector column

```python
from sqlalchemy import Column, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import TSVECTOR
from sqlalchemy.orm import DeclarativeBase

class Base(DeclarativeBase):
    pass

class DocumentModel(Base):
    __tablename__ = "documents"
    id = Column(Integer, primary_key=True, autoincrement=True)
    title = Column(String(255), nullable=False)
    body = Column(Text, nullable=False)
    search_vector = Column(TSVECTOR)

    __table_args__ = (
        Index("ix_documents_search", "search_vector", postgresql_using="gin"),
    )
```

## SearchService (PostgreSQL)

```python
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session
from lauren import injectable, Scope, post_construct
import os

@injectable(scope=Scope.SINGLETON)
class SearchService:
    def __init__(self) -> None:
        self._engine = create_engine(
            os.getenv("DATABASE_URL", "postgresql+psycopg2://localhost/mydb"),
            pool_pre_ping=True,
        )

    @post_construct
    async def setup(self) -> None:
        Base.metadata.create_all(self._engine)

    def index_document(self, title: str, body: str) -> dict:
        with Session(self._engine) as s:
            doc = DocumentModel(title=title, body=body)
            s.add(doc)
            s.flush()
            # Update tsvector column with weighted vectors
            s.execute(
                text(
                    "UPDATE documents SET search_vector = "
                    "setweight(to_tsvector('english', :title), 'A') || "
                    "setweight(to_tsvector('english', :body), 'B') "
                    "WHERE id = :id"
                ),
                {"title": title, "body": body, "id": doc.id},
            )
            s.commit()
            return {"id": doc.id, "title": doc.title}

    def search(self, query: str) -> list[dict]:
        with Session(self._engine) as s:
            results = s.execute(
                text(
                    "SELECT id, title, body, "
                    "ts_rank(search_vector, to_tsquery('english', :q)) AS rank "
                    "FROM documents "
                    "WHERE search_vector @@ to_tsquery('english', :q) "
                    "ORDER BY rank DESC"
                ),
                {"q": query},
            ).fetchall()
            return [{"id": r[0], "title": r[1], "body": r[2], "rank": r[3]}
                    for r in results]
```

## SQLite-compatible SearchService (for tests)

Replace the `index_document` and `search` methods with LIKE-based fallbacks:

```python
def index_document(self, title: str, body: str) -> dict:
    with Session(self._engine) as s:
        doc = DocumentModel(title=title, body=body)
        s.add(doc)
        s.commit()
        s.refresh(doc)
        return {"id": doc.id, "title": doc.title}

def search(self, query: str) -> list[dict]:
    with Session(self._engine) as s:
        results = s.execute(
            text(
                "SELECT id, title, body FROM documents "
                "WHERE title LIKE :q OR body LIKE :q"
            ),
            {"q": f"%{query}%"},
        ).fetchall()
        return [{"id": r[0], "title": r[1], "body": r[2]} for r in results]
```

## Controller

```python
from lauren import controller, get, post, Query, Json, module
from pydantic import BaseModel

class IndexBody(BaseModel):
    title: str
    body: str

@controller("/search")
class SearchController:
    def __init__(self, svc: SearchService) -> None:
        self._svc = svc

    @post("/index")
    async def index(self, body: Json[IndexBody]) -> dict:
        return self._svc.index_document(body.title, body.body)

    @get("/")
    async def search(self, q: Query[str]) -> list:
        return self._svc.search(q)

@module(controllers=[SearchController], providers=[SearchService])
class SearchModule:
    pass
```

## GIN index creation (one-time migration)

```sql
CREATE INDEX CONCURRENTLY ix_documents_search
ON documents USING GIN (search_vector);
```

## Common mistakes

- Forgetting to call `UPDATE … SET search_vector = …` after inserts —
  the `tsvector` column is not maintained automatically; use a trigger or
  update it in the same transaction.
- Using `plainto_tsquery` for user-facing search (it strips operators) vs
  `to_tsquery` for structured queries — prefer `websearch_to_tsquery` for
  user input as it accepts `"phrase"` and `-exclusion` syntax safely.
- Not specifying a language — `to_tsvector('english', …)` stems correctly;
  `to_tsvector(…)` uses the database default which may differ.
