---
name: sqlalchemy-models
description: Shows how to define SQLAlchemy ORM models and wire a synchronous DatabaseService into a Lauren app. Use when adding a relational database with CRUD operations to a Lauren application.
---

> Use `codemap find "post_construct"` to locate lifecycle hooks before reading.

# SQLAlchemy Model Definition with Lauren Framework

## Overview

A `DatabaseService` singleton owns the SQLAlchemy engine and exposes
CRUD helpers. Tables are created during `@post_construct` (startup) and
torn down during `@pre_destruct` (shutdown). Each CRUD method opens a
short-lived `Session` as a context manager.

## Dependencies

```
sqlalchemy
```

## Core Pattern

```python
from __future__ import annotations

from sqlalchemy import Column, Integer, String, create_engine
from sqlalchemy.orm import DeclarativeBase, Session

from lauren import (
    Json,
    Path,
    Scope,
    controller,
    get,
    injectable,
    module,
    post,
    post_construct,
    pre_destruct,
)
from pydantic import BaseModel


class Base(DeclarativeBase):
    pass


class UserModel(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(100), nullable=False)
    email = Column(String(255), unique=True, nullable=False)


@injectable(scope=Scope.SINGLETON)
class DatabaseService:
    """Manages the SQLAlchemy engine lifecycle and provides CRUD helpers."""

    def __init__(self) -> None:
        self._engine = create_engine("sqlite:///:memory:", echo=False)

    @post_construct
    async def create_tables(self) -> None:
        Base.metadata.create_all(self._engine)

    @pre_destruct
    async def drop_tables(self) -> None:
        Base.metadata.drop_all(self._engine)

    def get_session(self) -> Session:
        return Session(self._engine)

    def create_user(self, name: str, email: str) -> UserModel:
        with self.get_session() as session:
            user = UserModel(name=name, email=email)
            session.add(user)
            session.commit()
            session.refresh(user)
            return user

    def get_user(self, user_id: int) -> UserModel | None:
        with self.get_session() as session:
            return session.get(UserModel, user_id)

    def list_users(self) -> list[UserModel]:
        with self.get_session() as session:
            return session.query(UserModel).all()

    def delete_user(self, user_id: int) -> bool:
        with self.get_session() as session:
            user = session.get(UserModel, user_id)
            if user is None:
                return False
            session.delete(user)
            session.commit()
            return True


class CreateUserBody(BaseModel):
    name: str
    email: str


@controller("/users")
class UserController:
    def __init__(self, db: DatabaseService) -> None:
        self._db = db

    @get("/")
    async def list_users(self) -> list[dict]:
        users = self._db.list_users()
        return [{"id": u.id, "name": u.name, "email": u.email} for u in users]

    @post("/")
    async def create_user(self, body: Json[CreateUserBody]) -> dict:
        user = self._db.create_user(body.name, body.email)
        return {"id": user.id, "name": user.name, "email": user.email}

    @get("/{user_id}")
    async def get_user(self, user_id: Path[int]) -> dict:
        from lauren.exceptions import RouteNotFoundError
        user = self._db.get_user(user_id)
        if user is None:
            raise RouteNotFoundError(f"User {user_id} not found")
        return {"id": user.id, "name": user.name, "email": user.email}


@module(controllers=[UserController], providers=[DatabaseService])
class DatabaseModule:
    pass
```

## Key Points

- `@post_construct` fires once at startup (not per request) because `DatabaseService` is `SINGLETON`.
- Each CRUD method uses a fresh `Session` context manager — no long-lived session shared between requests.
- For production use `create_engine("postgresql+psycopg://...")` and a connection pool.
- Sync handlers that call the database are automatically offloaded to a thread pool by Lauren.
- To avoid `DetachedInstanceError`, load relationships eagerly or access them inside the `with` block.
