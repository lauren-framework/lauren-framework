"""A complete end-to-end application test — exercises routing, modules,
DI, protocols, middleware, guards, lifecycle, extractors, OpenAPI, and
error mapping in a single realistic application.
"""

from __future__ import annotations

import asyncio
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, Field

from lauren import (
    CallNext,
    ExecutionContext,
    Json,
    LaurenFactory,
    Path,
    Query,
    QueryField,
    Request,
    Response,
    controller,
    delete,
    get,
    injectable,
    middleware,
    module,
    post,
    post_construct,
    pre_destruct,
    set_metadata,
    use_guards,
    use_middlewares,
)
from lauren.exceptions import UnauthorizedError
from lauren.testing import TestClient


# ---------------------------------------------------------------------------
# Domain + repository
# ---------------------------------------------------------------------------


class Product(BaseModel):
    id: int
    name: str
    price: float = Field(gt=0)


class CreateProduct(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    price: float = Field(gt=0)


@runtime_checkable
class ProductRepository(Protocol):
    def list(self) -> list[Product]: ...
    def get(self, pid: int) -> Product | None: ...
    def create(self, data: CreateProduct) -> Product: ...
    def delete(self, pid: int) -> bool: ...


@injectable(provides=[ProductRepository])
class InMemoryProductRepository:
    def __init__(self) -> None:
        self._items: dict[int, Product] = {}
        self._next_id = 1
        self._started = False
        self._stopped = False

    @post_construct
    async def startup(self) -> None:
        self._started = True
        # seed
        self._items[self._next_id] = Product(id=self._next_id, name="Widget", price=9.99)
        self._next_id += 1

    @pre_destruct
    async def shutdown(self) -> None:
        self._stopped = True
        self._items.clear()

    def list(self):
        return list(self._items.values())

    def get(self, pid):
        return self._items.get(pid)

    def create(self, data):
        p = Product(id=self._next_id, **data.model_dump())
        self._items[self._next_id] = p
        self._next_id += 1
        return p

    def delete(self, pid):
        return self._items.pop(pid, None) is not None


# ---------------------------------------------------------------------------
# Cross-cutting concerns
# ---------------------------------------------------------------------------


@middleware()
class RequestIdMiddleware:
    async def dispatch(self, request: Request, call_next: CallNext) -> Response:
        rid = request.headers.get("x-request-id", "auto")
        request.state.request_id = rid
        response = await call_next(request)
        return response.with_header("x-request-id", rid)


@middleware()
class AuthMiddleware:
    async def dispatch(self, request: Request, call_next: CallNext) -> Response:
        token = request.headers.get("authorization", "")
        if not token.startswith("Bearer "):
            raise UnauthorizedError("missing bearer token")
        request.state.user = token[len("Bearer ") :]
        return await call_next(request)


class AdminGuard:
    async def can_activate(self, ctx: ExecutionContext) -> bool:
        required = ctx.get_metadata("required_role", "user")
        user = ctx.request.state.get("user", "")
        if required == "admin":
            return user == "admin"
        return bool(user)


# ---------------------------------------------------------------------------
# Controllers
# ---------------------------------------------------------------------------


@controller("/api/products", tags=["products"])
class ProductController:
    def __init__(self, repo: ProductRepository):  # type: ignore[valid-type]
        self.repo = repo

    @get("/", summary="List products")
    async def list_(
        self,
        limit: Query[int] = QueryField(default=10, ge=1, le=100),
    ) -> Response:
        items = self.repo.list()[:limit]
        return Response.json([p.model_dump() for p in items])

    @get("/{pid}", summary="Get product", response_model=Product)
    async def get_one(self, pid: Path[int]) -> Response:
        p = self.repo.get(pid)
        if p is None:
            return Response.json(
                {"error": {"code": "not_found", "message": "Product not found"}},
                status=404,
            )
        return Response.json(p.model_dump())

    @post("/", summary="Create product")
    @use_middlewares(AuthMiddleware)
    async def create(self, body: Json[CreateProduct]) -> Response:
        p = self.repo.create(body)
        return Response.created(p.model_dump(), location=f"/api/products/{p.id}")

    @delete("/{pid}")
    @use_middlewares(AuthMiddleware)
    @use_guards(AdminGuard)
    @set_metadata("required_role", "admin")
    async def delete(self, pid: Path[int]) -> Response:
        if not self.repo.delete(pid):
            return Response.empty(404)
        return Response.no_content()


@controller("/", tags=["meta"])
class MetaController:
    @get("/health")
    async def health(self) -> dict:
        return {"status": "ok"}


# ---------------------------------------------------------------------------
# Modules
# ---------------------------------------------------------------------------


@module(
    providers=[InMemoryProductRepository],
    exports=[InMemoryProductRepository],
)
class RepoModule:
    pass


@module(
    controllers=[ProductController, MetaController],
    imports=[RepoModule],
)
class AppModule:
    pass


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def build():
    app = LaurenFactory.create(AppModule, global_middlewares=[RequestIdMiddleware])
    return app, TestClient(app)


class TestEndToEnd:
    def test_health(self):
        _, c = build()
        r = c.get("/health", headers={"x-request-id": "req-1"})
        assert r.status_code == 200
        assert r.json() == {"status": "ok"}
        assert r.header("x-request-id") == "req-1"

    def test_list_products_seeded(self):
        _, c = build()
        r = c.get("/api/products/")
        assert r.status_code == 200
        data = r.json()
        assert len(data) >= 1
        assert data[0]["name"] == "Widget"

    def test_get_single(self):
        _, c = build()
        r = c.get("/api/products/1")
        assert r.status_code == 200
        assert r.json()["name"] == "Widget"

    def test_get_missing(self):
        _, c = build()
        r = c.get("/api/products/999")
        assert r.status_code == 404

    def test_create_requires_auth(self):
        _, c = build()
        r = c.post("/api/products/", json={"name": "Gadget", "price": 19.99})
        assert r.status_code == 401

    def test_create_authorized(self):
        _, c = build()
        r = c.post(
            "/api/products/",
            json={"name": "Gadget", "price": 19.99},
            headers={"Authorization": "Bearer alice"},
        )
        assert r.status_code == 201
        assert (r.header("location") or "").startswith("/api/products/")
        assert r.json()["name"] == "Gadget"

    def test_create_validation(self):
        _, c = build()
        r = c.post(
            "/api/products/",
            json={"name": "", "price": -5},
            headers={"Authorization": "Bearer alice"},
        )
        assert r.status_code == 422

    def test_delete_requires_admin(self):
        _, c = build()
        r = c.delete(
            "/api/products/1",
            headers={"Authorization": "Bearer bob"},
        )
        assert r.status_code == 403

    def test_delete_as_admin(self):
        _, c = build()
        r = c.delete(
            "/api/products/1",
            headers={"Authorization": "Bearer admin"},
        )
        assert r.status_code == 204
        # Now gone
        r2 = c.get("/api/products/1")
        assert r2.status_code == 404

    def test_query_validation(self):
        _, c = build()
        r = c.get("/api/products/?limit=0")
        assert r.status_code == 422
        r = c.get("/api/products/?limit=500")
        assert r.status_code == 422
        r = c.get("/api/products/?limit=5")
        assert r.status_code == 200

    def test_openapi(self):
        app, _ = build()
        schema = app.openapi()
        assert "/api/products/{pid}" in schema["paths"]
        assert "Product" in schema["components"]["schemas"]
        # Tags preserved
        get_op = schema["paths"]["/api/products/{pid}"]["get"]
        assert "products" in get_op["tags"]

    def test_full_lifecycle(self):
        async def run():
            app = LaurenFactory.create(AppModule, global_middlewares=[RequestIdMiddleware])
            await app.startup()
            repo = await app.container.resolve(InMemoryProductRepository)
            assert repo._started is True
            assert repo._stopped is False
            await app.shutdown()
            assert repo._stopped is True

        asyncio.run(run())

    def test_method_not_allowed_has_allow_header(self):
        _, c = build()
        r = c.request("PATCH", "/api/products/1")
        assert r.status_code == 405
        allow = r.header("allow")
        assert allow is not None
        assert "GET" in allow and "DELETE" in allow
