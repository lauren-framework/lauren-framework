---
name: sqlalchemy-async
description: Shows the async SQLAlchemy pattern (create_async_engine + AsyncSession) for Lauren. Use when building async-first database access; pairs with aiosqlite for SQLite or asyncpg for PostgreSQL.
---

> Use `codemap find "post_construct"` to locate lifecycle hooks before reading.

# SQLAlchemy Async Engine & Session Configuration

## Overview

The async SQLAlchemy pattern uses `create_async_engine` and `AsyncSession`.
A `SINGLETON` `AsyncDatabaseService` owns the engine. A `REQUEST`-scoped
`SessionProvider` yields an `AsyncSession` per-request, ensuring proper
isolation and automatic rollback on error.

## Dependencies

```
sqlalchemy[asyncio]
aiosqlite          # for SQLite (dev/test)
asyncpg            # for PostgreSQL (production)
```

## Async Engine Setup

```python
from __future__ import annotations

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy import Column, Integer, String, select

from lauren import Scope, injectable, post_construct, pre_destruct, module


class Base(DeclarativeBase):
    pass


class UserModel(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(100), nullable=False)
    email = Column(String(255), unique=True, nullable=False)


@injectable(scope=Scope.SINGLETON)
class AsyncDatabaseService:
    """Owns the async engine and session factory."""

    def __init__(self) -> None:
        self._engine = create_async_engine(
            "sqlite+aiosqlite:///:memory:", echo=False
        )
        self._session_factory = async_sessionmaker(
            self._engine, expire_on_commit=False
        )

    @post_construct
    async def create_tables(self) -> None:
        async with self._engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    @pre_destruct
    async def drop_tables(self) -> None:
        async with self._engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
        await self._engine.dispose()

    def session(self) -> AsyncSession:
        return self._session_factory()
```

## Request-Scoped Session Provider

```python
from lauren import injectable, Scope, pre_destruct

@injectable(scope=Scope.REQUEST)
class SessionProvider:
    """One AsyncSession per request; auto-closed after the request."""

    def __init__(self, db: AsyncDatabaseService) -> None:
        self._session: AsyncSession = db.session()

    @property
    def session(self) -> AsyncSession:
        return self._session

    @pre_destruct
    async def close(self) -> None:
        await self._session.close()
```

## Controller Usage

```python
from lauren import controller, get, post, Json, Path, module
from pydantic import BaseModel
from sqlalchemy import select


class CreateUserBody(BaseModel):
    name: str
    email: str


@controller("/users")
class UserController:
    def __init__(self, session_provider: SessionProvider) -> None:
        self._sp = session_provider

    @post("/")
    async def create_user(self, body: Json[CreateUserBody]) -> dict:
        session = self._sp.session
        user = UserModel(name=body.name, email=body.email)
        session.add(user)
        await session.commit()
        await session.refresh(user)
        return {"id": user.id, "name": user.name}

    @get("/")
    async def list_users(self) -> list[dict]:
        session = self._sp.session
        result = await session.execute(select(UserModel))
        users = result.scalars().all()
        return [{"id": u.id, "name": u.name} for u in users]


@module(
    controllers=[UserController],
    providers=[AsyncDatabaseService, SessionProvider],
)
class AsyncDatabaseModule:
    pass
```

## Sync Equivalent (for Tests Without aiosqlite)

For unit tests that don't need the async path, use the synchronous pattern
from `skills/sqlalchemy-models` — it avoids the `aiosqlite` dependency and
runs without an event loop.

## Key Points

- `expire_on_commit=False` on the session factory prevents `DetachedInstanceError`
  when accessing model attributes after `commit()`.
- `REQUEST`-scoped `SessionProvider` means each HTTP request gets a fresh session;
  `pre_destruct` closes it cleanly even if the handler raises.
- Never share an `AsyncSession` between concurrent coroutines — SQLAlchemy sessions
  are not thread-safe or coroutine-safe under concurrent access.
- For PostgreSQL production: `create_async_engine("postgresql+asyncpg://user:pw@host/db")`.
