"""Integration tests for the Alembic migration pattern (Skill 11).

All tests go through the TestClient / HTTP layer.
"""

from __future__ import annotations

from sqlalchemy import Column, Integer, String, create_engine, text
from sqlalchemy.orm import DeclarativeBase

from lauren import (
    LaurenFactory,
    Json,
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


class ItemModel(Base):
    __tablename__ = "items_migration"
    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(100), nullable=False)


# ---------------------------------------------------------------------------
# Service — wraps the engine so migration operations are injectable
# ---------------------------------------------------------------------------


@injectable(scope=Scope.SINGLETON)
class MigrationDemoService:
    def __init__(self) -> None:
        self._engine = create_engine("sqlite:///:memory:")

    @post_construct
    async def setup(self) -> None:
        Base.metadata.create_all(self._engine)

    def get_tables(self) -> list[str]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                text("SELECT name FROM sqlite_master WHERE type='table'")
            )
            return [r[0] for r in rows]

    def get_columns(self, table: str = "items_migration") -> list[str]:
        with self._engine.connect() as conn:
            rows = conn.execute(text(f"PRAGMA table_info({table})"))
            return [r[1] for r in rows]

    def run_upgrade(self) -> None:
        """Simulate an Alembic upgrade() — adds the description column."""
        with self._engine.connect() as conn:
            conn.execute(
                text("ALTER TABLE items_migration ADD COLUMN description TEXT")
            )
            conn.commit()

    def insert_item(self, name: str, description: str | None = None) -> dict:
        with self._engine.connect() as conn:
            if description is not None:
                conn.execute(
                    text(
                        "INSERT INTO items_migration (name, description) VALUES (:n, :d)"
                    ),
                    {"n": name, "d": description},
                )
            else:
                conn.execute(
                    text("INSERT INTO items_migration (name) VALUES (:n)"),
                    {"n": name},
                )
            conn.commit()
            row = conn.execute(
                text(
                    "SELECT id, name FROM items_migration WHERE name = :n ORDER BY id DESC LIMIT 1"
                ),
                {"n": name},
            ).fetchone()
            return {"id": row[0], "name": row[1]}


# ---------------------------------------------------------------------------
# Controller
# ---------------------------------------------------------------------------


class InsertItemBody(BaseModel):
    name: str
    description: str | None = None


@controller("/schema")
class SchemaController:
    def __init__(self, svc: MigrationDemoService) -> None:
        self._svc = svc

    @get("/tables")
    async def tables(self) -> dict:
        return {"tables": self._svc.get_tables()}

    @get("/columns")
    async def columns(self) -> dict:
        return {"columns": self._svc.get_columns()}

    @post("/upgrade")
    async def upgrade(self) -> dict:
        self._svc.run_upgrade()
        return {"upgraded": True, "columns": self._svc.get_columns()}

    @post("/items")
    async def insert(self, body: Json[InsertItemBody]) -> dict:
        return self._svc.insert_item(body.name, body.description)


@module(controllers=[SchemaController], providers=[MigrationDemoService])
class MigrationModule:
    pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def build_app() -> TestClient:
    return TestClient(LaurenFactory.create(MigrationModule))


# ---------------------------------------------------------------------------
# Tests — all through TestClient
# ---------------------------------------------------------------------------


class TestAlembicPattern:
    def test_tables_created_at_startup(self) -> None:
        client = build_app()
        r = client.get("/schema/tables")
        assert r.status_code == 200
        assert "items_migration" in r.json()["tables"]

    def test_initial_columns_exist(self) -> None:
        client = build_app()
        r = client.get("/schema/columns")
        assert r.status_code == 200
        cols = r.json()["columns"]
        assert "id" in cols
        assert "name" in cols

    def test_initial_no_description_column(self) -> None:
        client = build_app()
        r = client.get("/schema/columns")
        assert "description" not in r.json()["columns"]

    def test_upgrade_adds_description_column(self) -> None:
        client = build_app()
        r = client.post("/schema/upgrade")
        assert r.status_code == 200
        assert r.json()["upgraded"] is True
        assert "description" in r.json()["columns"]

    def test_columns_after_upgrade_via_get(self) -> None:
        client = build_app()
        client.post("/schema/upgrade")
        r = client.get("/schema/columns")
        assert "description" in r.json()["columns"]

    def test_insert_item_before_migration(self) -> None:
        client = build_app()
        r = client.post("/schema/items", json={"name": "Widget"})
        assert r.status_code == 200
        assert r.json()["name"] == "Widget"
        assert "id" in r.json()

    def test_insert_item_with_description_after_migration(self) -> None:
        client = build_app()
        client.post("/schema/upgrade")
        r = client.post(
            "/schema/items", json={"name": "Gadget", "description": "A fine gadget"}
        )
        assert r.status_code == 200
        assert r.json()["name"] == "Gadget"
