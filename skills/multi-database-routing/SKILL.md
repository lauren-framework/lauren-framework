---
name: multi-database-routing
description: Sets up a DatabaseRouter service that directs writes to a primary database and reads to a replica. Use when you need read/write splitting across multiple database connections in a Lauren app.
---

> Use `codemap find "SymbolName"` to locate any symbol before reading — it gives
> exact file + line range and is faster than grep across the whole repo.

# Multi-Database Read/Write Routing

## Overview

In production, writes go to a primary PostgreSQL instance and reads go to one
or more read replicas. A `DatabaseRouter` service encapsulates both engines and
exposes `write_engine` / `read_engine` properties. All repositories receive
the router via DI and choose the correct engine per operation.

## DatabaseRouter service

```python
from lauren import injectable, Scope, post_construct, pre_destruct
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
import os

@injectable(scope=Scope.SINGLETON)
class DatabaseRouter:
    def __init__(self) -> None:
        write_url = os.getenv("DATABASE_WRITE_URL", "postgresql+psycopg2://localhost/mydb")
        read_url = os.getenv("DATABASE_READ_URL", write_url)  # fallback to write
        self._write_engine = create_engine(write_url, pool_pre_ping=True)
        self._read_engine = create_engine(read_url, pool_pre_ping=True)

    @post_construct
    async def setup(self) -> None:
        # Verify connectivity at startup — catches misconfiguration early
        with self._write_engine.connect():
            pass
        with self._read_engine.connect():
            pass

    @pre_destruct
    def shutdown(self) -> None:
        self._write_engine.dispose()
        self._read_engine.dispose()

    @property
    def write_engine(self):
        return self._write_engine

    @property
    def read_engine(self):
        return self._read_engine

    def write_session(self) -> Session:
        return Session(self._write_engine)

    def read_session(self) -> Session:
        return Session(self._read_engine)
```

## Repository using the router

```python
from sqlalchemy.orm import Session
from lauren import injectable, Scope

@injectable(scope=Scope.SINGLETON)
class ItemRepository:
    def __init__(self, router: DatabaseRouter) -> None:
        self._router = router

    def create(self, name: str) -> dict:
        with self._router.write_session() as s:
            item = ItemModel(name=name)
            s.add(item)
            s.commit()
            s.refresh(item)
            return {"id": item.id, "name": item.name}

    def find_all(self) -> list[dict]:
        """Reads from replica."""
        with self._router.read_session() as s:
            rows = s.query(ItemModel).all()
            return [{"id": r.id, "name": r.name} for r in rows]
```

## Controller

```python
from lauren import controller, get, post, module, Json
from pydantic import BaseModel

class CreateItemBody(BaseModel):
    name: str

@controller("/items")
class ItemController:
    def __init__(self, repo: ItemRepository) -> None:
        self._repo = repo

    @get("/")
    async def list_items(self) -> list:
        return self._repo.find_all()

    @post("/")
    async def create_item(self, body: Json[CreateItemBody]) -> dict:
        return self._repo.create(body.name)

@module(
    controllers=[ItemController],
    providers=[ItemController, ItemRepository, DatabaseRouter],
)
class ItemModule:
    pass
```

## Testing with two in-memory SQLite DBs

Use separate `create_engine("sqlite:///:memory:")` calls — each is an
independent in-memory database, simulating separate primary/replica instances.

```python
from sqlalchemy import create_engine

write_engine = create_engine("sqlite:///:memory:")
read_engine = create_engine("sqlite:///:memory:")
Base.metadata.create_all(write_engine)
Base.metadata.create_all(read_engine)
```

## Common mistakes

- Using the same engine for both reads and writes defeats the purpose — always
  point `read_engine` at a separate URL in production.
- Session must be closed after use — prefer context managers (`with session: ...`)
  rather than calling `session.close()` manually.
- `SINGLETON` `DatabaseRouter` is safe because SQLAlchemy engines are thread-safe
  and manage their own connection pools.
- Do not mix `write_session` for reads: replica lag means writes may not yet
  appear on the replica, and using the write engine for reads adds load.
