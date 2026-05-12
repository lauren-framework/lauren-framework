"""Integration tests for the SQLAlchemy async engine skill.

These tests use the synchronous SQLAlchemy pattern (mirroring the async
pattern from the SKILL.md) to avoid requiring aiosqlite in CI.  The async
SKILL.md documents the async engine/session wiring; the sync equivalent
validates the same CRUD semantics in a simpler test setup.
"""

from __future__ import annotations

from sqlalchemy import Column, Integer, String, create_engine
from sqlalchemy.orm import DeclarativeBase, Session

from lauren import (
    Json,
    LaurenFactory,
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
from lauren.testing import TestClient
from pydantic import BaseModel


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class AsyncBase(DeclarativeBase):
    pass


class ProductModel(AsyncBase):
    __tablename__ = "products_async_skill"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(100), nullable=False)
    price_cents = Column(Integer, nullable=False)


# ---------------------------------------------------------------------------
# Sync-equivalent database service (mirrors async pattern)
# ---------------------------------------------------------------------------


@injectable(scope=Scope.SINGLETON)
class ProductDatabase:
    """Synchronous equivalent of the async DatabaseService for testing."""

    def __init__(self) -> None:
        self._engine = create_engine("sqlite:///:memory:", echo=False)

    @post_construct
    async def initialize(self) -> None:
        AsyncBase.metadata.create_all(self._engine)

    @pre_destruct
    async def teardown(self) -> None:
        AsyncBase.metadata.drop_all(self._engine)

    def _session(self) -> Session:
        return Session(self._engine)

    def create_product(self, name: str, price_cents: int) -> ProductModel:
        with self._session() as s:
            prod = ProductModel(name=name, price_cents=price_cents)
            s.add(prod)
            s.commit()
            s.refresh(prod)
            return prod

    def get_product(self, product_id: int) -> ProductModel | None:
        with self._session() as s:
            return s.get(ProductModel, product_id)

    def list_products(self) -> list[ProductModel]:
        with self._session() as s:
            return list(s.query(ProductModel).all())

    def update_price(self, product_id: int, new_price: int) -> ProductModel | None:
        with self._session() as s:
            prod = s.get(ProductModel, product_id)
            if prod is None:
                return None
            prod.price_cents = new_price
            s.commit()
            s.refresh(prod)
            return prod

    def delete_product(self, product_id: int) -> bool:
        with self._session() as s:
            prod = s.get(ProductModel, product_id)
            if prod is None:
                return False
            s.delete(prod)
            s.commit()
            return True


# ---------------------------------------------------------------------------
# API layer
# ---------------------------------------------------------------------------


class CreateProductBody(BaseModel):
    name: str
    price_cents: int


class UpdatePriceBody(BaseModel):
    price_cents: int


@controller("/products")
class ProductController:
    def __init__(self, db: ProductDatabase) -> None:
        self._db = db

    @get("/")
    async def list_products(self) -> list[dict]:
        return [{"id": p.id, "name": p.name, "price_cents": p.price_cents} for p in self._db.list_products()]

    @post("/")
    async def create_product(self, body: Json[CreateProductBody]) -> dict:
        p = self._db.create_product(body.name, body.price_cents)
        return {"id": p.id, "name": p.name, "price_cents": p.price_cents}

    @get("/{product_id}")
    async def get_product(self, product_id: Path[int]) -> dict:
        from lauren.exceptions import RouteNotFoundError

        p = self._db.get_product(product_id)
        if p is None:
            raise RouteNotFoundError(f"Product {product_id} not found")
        return {"id": p.id, "name": p.name, "price_cents": p.price_cents}


@module(controllers=[ProductController], providers=[ProductDatabase])
class ProductModule:
    pass


def build_app() -> TestClient:
    return TestClient(LaurenFactory.create(ProductModule))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestSQLAlchemyAsync:
    def test_create_product(self):
        client = build_app()
        r = client.post("/products/", json={"name": "Widget", "price_cents": 999})
        assert r.status_code == 200
        data = r.json()
        assert data["name"] == "Widget"
        assert data["price_cents"] == 999
        assert isinstance(data["id"], int)

    def test_get_product_by_id(self):
        client = build_app()
        create_r = client.post("/products/", json={"name": "Gadget", "price_cents": 4999})
        pid = create_r.json()["id"]

        r = client.get(f"/products/{pid}")
        assert r.status_code == 200
        assert r.json()["name"] == "Gadget"

    def test_list_products_empty_initially(self):
        client = build_app()
        r = client.get("/products/")
        assert r.status_code == 200
        assert r.json() == []

    def test_list_products_after_create(self):
        client = build_app()
        client.post("/products/", json={"name": "A", "price_cents": 100})
        client.post("/products/", json={"name": "B", "price_cents": 200})
        r = client.get("/products/")
        assert r.status_code == 200
        assert len(r.json()) == 2

    def test_get_nonexistent_product_returns_404(self):
        client = build_app()
        r = client.get("/products/99999")
        assert r.status_code == 404

    def test_post_construct_runs_before_first_request(self):
        """Tables must exist before any request is processed."""
        client = build_app()
        r = client.get("/products/")
        assert r.status_code == 200
