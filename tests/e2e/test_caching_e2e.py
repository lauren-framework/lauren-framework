"""End-to-end: one MemoryStore behind sessions, the cache, response caching and idempotency."""

from __future__ import annotations

from dataclasses import dataclass

from lauren import (
    Json,
    LaurenFactory,
    Session,
    SessionConfig,
    controller,
    get,
    injectable,
    module,
    post,
)
from lauren.cache import Cache, CacheModule, cache_response, idempotent
from lauren.stores import MemoryStore, StoresModule
from lauren.testing import TestClient


@dataclass
class Product:
    id: int
    name: str
    price: int


@dataclass
class PaymentIn:
    product_id: int
    quantity: int


@injectable()
class Counters:
    def __init__(self) -> None:
        self.catalog_queries = 0
        self.product_queries = 0
        self.payments = 0


@injectable()
class ProductRepository:
    def __init__(self, counters: Counters) -> None:
        self._counters = counters
        self._rows = {1: Product(1, "pen", 3), 2: Product(2, "ink", 7)}

    async def all(self) -> list[Product]:
        self._counters.catalog_queries += 1
        return list(self._rows.values())

    async def find(self, product_id: int) -> Product:
        self._counters.product_queries += 1
        return self._rows[product_id]

    async def reprice(self, product_id: int, price: int) -> None:
        row = self._rows[product_id]
        self._rows[product_id] = Product(row.id, row.name, price)


@injectable()
class ProductService:
    def __init__(self, cache: Cache, repo: ProductRepository) -> None:
        self._cache = cache
        self._repo = repo

    async def get(self, product_id: int) -> Product:
        return await self._cache.get_or_set(
            f"product:{product_id}",
            lambda: self._repo.find(product_id),
            ttl=60,
            tags=(f"product:{product_id}", "catalog"),
            type=Product,
        )

    async def reprice(self, product_id: int, price: int) -> None:
        await self._repo.reprice(product_id, price)
        await self._cache.invalidate_tags(f"product:{product_id}", "catalog")


@controller("/shop")
class ShopController:
    def __init__(self, service: ProductService, repo: ProductRepository, counters: Counters) -> None:
        self._service = service
        self._repo = repo
        self._counters = counters

    @get("/catalog")
    @cache_response(ttl=30, tags=("catalog",))
    async def catalog(self) -> list[Product]:
        return await self._repo.all()

    @get("/products/{product_id}")
    async def product(self, product_id: int) -> Product:
        return await self._service.get(product_id)

    @post("/products/{product_id}/price")
    async def reprice(self, product_id: int, body: Json[dict]) -> dict:
        await self._service.reprice(product_id, int(body["price"]))
        return {"ok": True}

    @post("/cart")
    async def add_to_cart(self, session: Session, body: Json[dict]) -> dict:
        session["cart"] = [*session.get("cart", []), body["product_id"]]
        return {"cart": session["cart"]}

    @get("/cart")
    async def cart(self, session: Session) -> dict:
        return {"cart": session.get("cart", [])}

    @post("/payments")
    @idempotent(ttl=3600)
    async def pay(self, body: Json[PaymentIn]) -> dict:
        self._counters.payments += 1
        product = await self._service.get(body.product_id)
        return {"payment": self._counters.payments, "total": product.price * body.quantity}


def test_one_store_serves_sessions_cache_responses_and_idempotency() -> None:
    store = MemoryStore(max_entries=10_000)

    @module(
        imports=[StoresModule.for_root(default=store), CacheModule.for_root(default_ttl=120)],
        providers=[Counters, ProductRepository, ProductService],
        controllers=[ShopController],
    )
    class ShopModule:
        pass

    app = LaurenFactory.create(
        ShopModule,
        sessions=SessionConfig(
            secret="e2e-secret" * 4, store=store, revocation_store=store, secure=False, max_age=600
        ),
    )
    with TestClient(app) as client:
        counters = client.resolve(Counters)

        # Response caching: the catalog query runs once.
        first = client.get("/shop/catalog").json()
        assert client.get("/shop/catalog").json() == first
        assert counters.catalog_queries == 1

        # Cache.get_or_set with typed decode: one repository query per product.
        assert client.get("/shop/products/1").json() == {"id": 1, "name": "pen", "price": 3}
        assert client.get("/shop/products/1").json()["price"] == 3
        assert counters.product_queries == 1

        # A write invalidates both the value entry and the cached response by tag.
        assert client.post("/shop/products/1/price", json={"price": 5}).status_code == 200
        assert client.get("/shop/products/1").json()["price"] == 5
        assert next(p for p in client.get("/shop/catalog").json() if p["id"] == 1)["price"] == 5
        assert (counters.product_queries, counters.catalog_queries) == (2, 2)

        # Sessions live in the same store.
        client.post("/shop/cart", json={"product_id": 1})
        assert client.get("/shop/cart").json() == {"cart": [1]}

        # Idempotent payments: a retried request is not charged twice.
        payment = {"product_id": 1, "quantity": 2}
        charged = client.post("/shop/payments", json=payment, headers={"Idempotency-Key": "order-1"})
        retried = client.post("/shop/payments", json=payment, headers={"Idempotency-Key": "order-1"})
        assert charged.json() == retried.json() == {"payment": 1, "total": 10}
        assert retried.headers["idempotent-replayed"] == "true"
        assert counters.payments == 1

    # Every feature wrote into the one shared, bounded store.
    assert len(store) <= store.max_entries
