---
name: search-engine-integration
description: Integrates Elasticsearch or Meilisearch via a common SearchIndex interface. Use when you need a pluggable search backend with index, search, and delete operations, swappable between providers.
---

> Use `codemap find "SymbolName"` to locate any symbol before reading — it gives
> exact file + line range and is faster than grep across the whole repo.

# Elasticsearch / Meilisearch Integration

## Overview

Define a `SearchIndex` abstract base class with `index`, `search`, and `delete`
methods. Provide concrete implementations for Elasticsearch and Meilisearch, and
an `InMemorySearchIndex` for tests. Wire the desired implementation via
`use_value` or `use_class` in the module's `providers` list.

## Abstract interface

```python
from abc import ABC, abstractmethod

class SearchIndex(ABC):
    @abstractmethod
    def index(self, index_name: str, doc_id: str, body: dict) -> None: ...

    @abstractmethod
    def search(self, index_name: str, query: str) -> list[dict]: ...

    @abstractmethod
    def delete(self, index_name: str, doc_id: str) -> None: ...
```

## In-memory implementation (tests)

```python
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
```

## Elasticsearch implementation

```python
from elasticsearch import Elasticsearch

class ElasticsearchIndex(SearchIndex):
    def __init__(self, hosts: list[str] | None = None) -> None:
        self._es = Elasticsearch(hosts or ["http://localhost:9200"])

    def index(self, index_name: str, doc_id: str, body: dict) -> None:
        self._es.index(index=index_name, id=doc_id, document=body)

    def search(self, index_name: str, query: str) -> list[dict]:
        resp = self._es.search(
            index=index_name,
            body={"query": {"multi_match": {"query": query, "fields": ["*"]}}},
        )
        return [hit["_source"] for hit in resp["hits"]["hits"]]

    def delete(self, index_name: str, doc_id: str) -> None:
        self._es.delete(index=index_name, id=doc_id, ignore=[404])
```

## Meilisearch implementation

```python
import meilisearch

class MeilisearchIndex(SearchIndex):
    def __init__(self, url: str = "http://localhost:7700", api_key: str = "") -> None:
        self._client = meilisearch.Client(url, api_key)

    def index(self, index_name: str, doc_id: str, body: dict) -> None:
        self._client.index(index_name).add_documents([{"id": doc_id, **body}])

    def search(self, index_name: str, query: str) -> list[dict]:
        result = self._client.index(index_name).search(query)
        return result["hits"]

    def delete(self, index_name: str, doc_id: str) -> None:
        self._client.index(index_name).delete_document(doc_id)
```

## SearchIndexService wrapper

```python
from lauren import injectable, Scope

@injectable(scope=Scope.SINGLETON)
class SearchIndexService:
    def __init__(self, backend: SearchIndex) -> None:
        self._backend = backend

    def index(self, index_name: str, doc_id: str, body: dict) -> None:
        self._backend.index(index_name, doc_id, body)

    def search(self, index_name: str, query: str) -> list[dict]:
        return self._backend.search(index_name, query)

    def delete(self, index_name: str, doc_id: str) -> None:
        self._backend.delete(index_name, doc_id)
```

## Wiring the backend

```python
from lauren import module, use_value

# Tests — in-memory backend
mem = InMemorySearchIndex()

@module(
    controllers=[SearchController],
    providers=[
        use_value(provide=SearchIndex, value=mem),
        SearchIndexService,
    ],
)
class SearchModule:
    pass

# Production — Elasticsearch
es = ElasticsearchIndex(hosts=["http://es:9200"])

@module(
    controllers=[SearchController],
    providers=[
        use_value(provide=SearchIndex, value=es),
        SearchIndexService,
    ],
)
class SearchModule:
    pass
```

## Controller

```python
from lauren import controller, get, post, delete, Json, Path, Query, module
from pydantic import BaseModel

class IndexBody(BaseModel):
    title: str
    content: str

@controller("/search")
class SearchController:
    def __init__(self, svc: SearchIndexService) -> None:
        self._svc = svc

    @post("/{index}/{doc_id}")
    async def index_doc(self, index: Path[str], doc_id: Path[str],
                        body: Json[IndexBody]) -> dict:
        self._svc.index(index, doc_id, body.model_dump())
        return {"indexed": doc_id}

    @get("/{index}")
    async def search(self, index: Path[str], q: Query[str]) -> list:
        return self._svc.search(index, q)

    @delete("/{index}/{doc_id}")
    async def delete_doc(self, index: Path[str], doc_id: Path[str]) -> dict:
        self._svc.delete(index, doc_id)
        return {"deleted": doc_id}
```

## Common mistakes

- Returning the raw backend response instead of a normalised list — callers
  should not know which backend is active.
- Not handling 404 on `delete` — Elasticsearch raises if the document is
  missing; pass `ignore=[404]` or catch the exception.
- Forgetting to create the Meilisearch index before indexing — call
  `client.create_index(name, {"primaryKey": "id"})` once at startup.
