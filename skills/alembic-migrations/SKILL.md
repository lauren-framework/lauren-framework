---
name: alembic-migrations
description: Integrates Alembic database migrations with a Lauren app. Use when you need schema versioning, migration scripts, or want to run database migrations alongside a Lauren service.
---

> Use `codemap find "SymbolName"` to locate any symbol before reading — it gives
> exact file + line range and is faster than grep across the whole repo.

# Alembic Migration Creation & Execution

## Overview

Alembic handles schema versioning for SQLAlchemy models. The key pattern for
Lauren apps is that the `DatabaseService` singleton owns the engine, and the
Alembic `env.py` reads the connection URL from the same source so there is one
canonical configuration.

## Project layout

```
myapp/
├── alembic/
│   ├── env.py
│   ├── script.py.mako
│   └── versions/
│       └── 001_add_description_to_items.py
├── alembic.ini
├── models.py
└── services.py
```

## DatabaseService singleton

```python
from lauren import injectable, post_construct, pre_destruct, Scope
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

@injectable(scope=Scope.SINGLETON)
class DatabaseService:
    def __init__(self) -> None:
        self._url = "postgresql+psycopg2://user:pass@localhost/mydb"
        self._engine = create_engine(self._url, pool_pre_ping=True)

    @property
    def engine(self):
        return self._engine

    @property
    def url(self) -> str:
        return self._url

    def session(self) -> Session:
        return Session(self._engine)

    @pre_destruct
    def shutdown(self) -> None:
        self._engine.dispose()
```

## SQLAlchemy models

```python
from sqlalchemy import Column, Integer, String
from sqlalchemy.orm import DeclarativeBase

class Base(DeclarativeBase):
    pass

class ItemModel(Base):
    __tablename__ = "items"
    id = Column(Integer, primary_key=True)
    name = Column(String(100), nullable=False)
```

## alembic/env.py — reading URL from service config

```python
from alembic import context
from sqlalchemy import engine_from_config, pool
import os

# Pull URL from env var or a shared config module — not from the
# DI container (Alembic runs outside the ASGI process).
DATABASE_URL = os.environ["DATABASE_URL"]

config = context.config
config.set_main_option("sqlalchemy.url", DATABASE_URL)

from models import Base
target_metadata = Base.metadata

def run_migrations_offline():
    context.configure(url=DATABASE_URL, target_metadata=target_metadata,
                      literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()

def run_migrations_online():
    connectable = engine_from_config(
        config.get_section(config.config_ini_section),
        prefix="sqlalchemy.", poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()

if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
```

## Sample migration file

```python
# alembic/versions/001_add_description_to_items.py
"""Add description column to items."""
from alembic import op
import sqlalchemy as sa

revision = "001"
down_revision = None
branch_labels = None
depends_on = None

def upgrade() -> None:
    op.add_column("items", sa.Column("description", sa.Text(), nullable=True))

def downgrade() -> None:
    op.drop_column("items", "description")
```

## Running migrations programmatically

```python
from alembic.config import Config
from alembic import command

def run_migrations(db_url: str, script_location: str = "alembic") -> None:
    cfg = Config()
    cfg.set_main_option("script_location", script_location)
    cfg.set_main_option("sqlalchemy.url", db_url)
    command.upgrade(cfg, "head")
```

## Common mistakes

- Running Alembic inside a `@post_construct` hook blocks startup — run migrations
  before calling `LaurenFactory.create()`, in a separate migration step.
- Never import `Base.metadata` from a module that triggers DI at import time.
- SQLite does not support `DROP COLUMN` or `ALTER TABLE … ALTER COLUMN` — use
  table recreation (`op.batch_alter_table`) for SQLite-compatible migrations.
