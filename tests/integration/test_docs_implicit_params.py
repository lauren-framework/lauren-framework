"""Integration tests for docs/guides/implicit-params.md.

Each test class maps to a section of the guide and exercises the exact code
snippets shown there, so that any drift between docs and implementation is
caught by CI.
"""

from __future__ import annotations

from typing import Optional

import pytest
from pydantic import BaseModel

from lauren import (
    Json,
    LaurenFactory,
    Query,
    controller,
    delete,
    get,
    injectable,
    module,
    patch,
    post,
    put,
)
from lauren.exceptions import UnresolvableParameterError
from lauren.testing import TestClient


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _app(*controllers: type, providers: list | None = None) -> TestClient:
    @module(controllers=list(controllers), providers=providers or [])
    class M:
        pass

    return TestClient(LaurenFactory.create(M))


# ===========================================================================
# § Quick example — the four-handler ItemController from the guide
# ===========================================================================


class CreateItem(BaseModel):
    name: str
    price: float


class TestQuickExample:
    """The complete ItemController example from the guide's 'Quick example' section."""

    @pytest.fixture(autouse=True)
    def _build(self):
        @controller("/items")
        class ItemController:
            @get("/{item_id}")
            async def get_item(
                self,
                item_id: int,
                format: str = "json",
            ) -> dict:
                return {"item_id": item_id, "format": format}

            @post("/")
            async def create(
                self,
                warehouse: str,
                item: CreateItem,
            ) -> dict:
                return {"warehouse": warehouse, "name": item.name}, 201

            @put("/{item_id}")
            async def update(
                self,
                item_id: int,
                item: CreateItem,  # auto Json — must come before defaults
                notify: bool = False,  # auto Query with default
            ) -> dict:
                return {"item_id": item_id, "notify": notify, "name": item.name}

            @delete("/{item_id}")
            async def remove(
                self,
                item_id: int,
            ) -> dict:
                return {"deleted": item_id}

        self.client = _app(ItemController)

    def test_get_with_path_and_optional_query(self):
        """item_id auto-Path, format auto-Query with default."""
        r = self.client.get("/items/42?format=yaml")
        assert r.status_code == 200
        assert r.json() == {"item_id": 42, "format": "yaml"}

    def test_get_uses_default_when_query_absent(self):
        r = self.client.get("/items/42")
        assert r.status_code == 200
        assert r.json()["format"] == "json"

    def test_post_query_and_body(self):
        """warehouse auto-Query, item auto-Json body; returns 201."""
        r = self.client.post(
            "/items/?warehouse=EU",
            json={"name": "widget", "price": 9.99},
        )
        assert r.status_code == 201
        assert r.json() == {"warehouse": "EU", "name": "widget"}

    def test_put_path_query_and_body(self):
        """item_id auto-Path, notify auto-Query bool, item auto-Json body."""
        r = self.client.put(
            "/items/7?notify=true",
            json={"name": "gadget", "price": 2.5},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["item_id"] == 7
        assert body["notify"] is True
        assert body["name"] == "gadget"

    def test_delete_path_only(self):
        r = self.client.delete("/items/99")
        assert r.status_code == 200
        assert r.json() == {"deleted": 99}


# ===========================================================================
# § Scalar query params in detail
# ===========================================================================


class TestScalarQueryParams:
    """All the primitive types the guide lists under 'Scalar query params in detail'."""

    def test_int(self):
        @controller("/")
        class C:
            @get("/")
            async def h(self, page: int) -> dict:
                return {"page": page}

        r = _app(C).get("/?page=5")
        assert r.json() == {"page": 5}

    def test_str(self):
        @controller("/")
        class C:
            @get("/")
            async def h(self, name: str) -> dict:
                return {"name": name}

        r = _app(C).get("/?name=alice")
        assert r.json() == {"name": "alice"}

    def test_float(self):
        @controller("/")
        class C:
            @get("/")
            async def h(self, ratio: float) -> dict:
                return {"ratio": ratio}

        r = _app(C).get("/?ratio=3.14")
        assert r.json()["ratio"] == pytest.approx(3.14)

    def test_bool_true_variants(self):
        """'true', '1', 'yes', 'on' all coerce to True."""

        @controller("/")
        class C:
            @get("/")
            async def h(self, active: bool) -> dict:
                return {"active": active}

        client = _app(C)
        for truthy in ("true", "True", "TRUE", "1", "yes", "YES", "on", "ON"):
            r = client.get(f"/?active={truthy}")
            assert r.json() == {"active": True}, f"Expected True for {truthy!r}"

    def test_bool_false_variants(self):
        """Anything not in the truthy set coerces to False."""

        @controller("/")
        class C:
            @get("/")
            async def h(self, active: bool) -> dict:
                return {"active": active}

        client = _app(C)
        for falsy in ("false", "False", "0", "no", "off", "anything"):
            r = client.get(f"/?active={falsy}")
            assert r.json() == {"active": False}, f"Expected False for {falsy!r}"

    def test_optional_absent_resolves_to_none(self):
        @controller("/")
        class C:
            @get("/")
            async def h(self, q: Optional[str] = None) -> dict:
                return {"q": q}

        r = _app(C).get("/")
        assert r.json() == {"q": None}

    def test_optional_present(self):
        @controller("/")
        class C:
            @get("/")
            async def h(self, q: Optional[str] = None) -> dict:
                return {"q": q}

        r = _app(C).get("/?q=hello")
        assert r.json() == {"q": "hello"}

    def test_default_value_used_when_absent(self):
        @controller("/")
        class C:
            @get("/")
            async def h(self, page: int = 1, page_size: int = 20) -> dict:
                return {"page": page, "page_size": page_size}

        r = _app(C).get("/")
        assert r.json() == {"page": 1, "page_size": 20}

    def test_default_value_overridden(self):
        @controller("/")
        class C:
            @get("/")
            async def h(self, page: int = 1) -> dict:
                return {"page": page}

        r = _app(C).get("/?page=3")
        assert r.json() == {"page": 3}

    def test_required_absent_returns_422(self):
        @controller("/")
        class C:
            @get("/")
            async def h(self, page: int) -> dict:
                return {"page": page}

        r = _app(C).get("/")
        assert r.status_code == 422

    def test_list_str_multi_value(self):
        """?tags=a&tags=b → ['a', 'b']  (repeated key)."""

        @controller("/")
        class C:
            @get("/")
            async def h(self, tags: list[str]) -> dict:
                return {"tags": tags}

        r = _app(C).get("/?tags=a&tags=b")
        assert r.status_code == 200
        assert sorted(r.json()["tags"]) == ["a", "b"]

    def test_list_int_multi_value(self):
        @controller("/")
        class C:
            @get("/")
            async def h(self, ids: list[int]) -> dict:
                return {"ids": ids}

        r = _app(C).get("/?ids=1&ids=2&ids=3")
        assert r.status_code == 200
        assert sorted(r.json()["ids"]) == [1, 2, 3]

    def test_tuple_multi_value(self):
        """tuple[scalar, ...] is also auto-promoted to query params."""

        @controller("/")
        class C:
            @get("/")
            async def h(self, scores: tuple[float, ...]) -> dict:
                return {"scores": list(scores)}

        r = _app(C).get("/?scores=1.5&scores=2.5")
        assert r.status_code == 200
        assert r.json()["scores"] == pytest.approx([1.5, 2.5])

    def test_bytes_scalar(self):
        """bytes auto-promotes to a query param; the string is UTF-8 encoded."""

        @controller("/")
        class C:
            @get("/")
            async def h(self, data: bytes) -> dict:
                return {"data": data.decode("utf-8")}

        r = _app(C).get("/?data=hello")
        assert r.status_code == 200
        assert r.json() == {"data": "hello"}

    def test_complex_scalar(self):
        """complex auto-promotes to a query param and coerces the string."""

        @controller("/")
        class C:
            @get("/")
            async def h(self, z: complex) -> dict:
                return {"real": z.real, "imag": z.imag}

        r = _app(C).get("/?z=1%2B2j")  # URL-encoded "1+2j"
        assert r.status_code == 200
        assert r.json() == {"real": 1.0, "imag": 2.0}


# ===========================================================================
# § Pydantic models in detail
# ===========================================================================


class Address(BaseModel):
    city: str
    country: str


class CreateUser(BaseModel):
    name: str
    email: str
    address: Address


class PatchUser(BaseModel):
    name: str | None = None
    email: str | None = None


class TestPydanticBodyParams:
    """'Pydantic models in detail' section of the guide."""

    def test_simple_model_auto_extracted_from_body(self):
        @controller("/users")
        class UserController:
            @post("/")
            async def create(self, user: CreateUser) -> dict:
                return {"name": user.name, "city": user.address.city}, 201

        r = _app(UserController).post(
            "/users/",
            json={
                "name": "Alice",
                "email": "a@example.com",
                "address": {"city": "Paris", "country": "FR"},
            },
        )
        assert r.status_code == 201
        assert r.json() == {"name": "Alice", "city": "Paris"}

    def test_optional_body_present(self):
        @controller("/users")
        class UserController:
            @patch("/{user_id}")
            async def update(self, user_id: int, body: Optional[PatchUser] = None) -> dict:
                if body is None:
                    return {"user_id": user_id, "changed": False}
                return {"user_id": user_id, "name": body.name}

        r = _app(UserController).patch(
            "/users/5",
            json={"name": "Bob"},
        )
        assert r.status_code == 200
        assert r.json() == {"user_id": 5, "name": "Bob"}

    def test_optional_body_absent_resolves_to_none(self):
        """When no body is sent and the default is None, the param is None."""

        @controller("/users")
        class UserController:
            @patch("/{user_id}")
            async def update(self, user_id: int, body: Optional[PatchUser] = None) -> dict:
                if body is None:
                    return {"user_id": user_id, "changed": False}
                return {"user_id": user_id, "changed": True}

        r = _app(UserController).patch("/users/5")
        assert r.status_code == 200
        assert r.json() == {"user_id": 5, "changed": False}

    def test_required_body_absent_returns_422(self):
        """When body is required (no default), an absent body returns 422."""

        @controller("/")
        class C:
            @post("/")
            async def create(self, item: CreateItem) -> dict:
                return {"name": item.name}

        r = _app(C).post("/")
        assert r.status_code == 422


# ===========================================================================
# § Mixing sources
# ===========================================================================


class OrderBody(BaseModel):
    product_id: int
    quantity: int


class TestMixingSources:
    """'Mixing sources' section — all three auto-detected sources in one handler."""

    def test_path_query_and_body_together(self):
        @controller("/")
        class C:
            @post("/{customer_id}/orders")
            async def place_order(
                self,
                customer_id: int,
                order: OrderBody,  # auto Json — before defaults
                priority: str = "low",
                dry_run: bool = False,
            ) -> dict:
                return {
                    "customer": customer_id,
                    "priority": priority,
                    "dry_run": dry_run,
                    "product": order.product_id,
                }

        r = _app(C).post(
            "/1/orders?priority=high&dry_run=true",
            json={"product_id": 7, "quantity": 3},
        )
        assert r.status_code == 200
        assert r.json() == {
            "customer": 1,
            "priority": "high",
            "dry_run": True,
            "product": 7,
        }

    def test_default_priority_and_dry_run(self):
        @controller("/")
        class C:
            @post("/{customer_id}/orders")
            async def place_order(
                self,
                customer_id: int,
                order: OrderBody,
                priority: str = "low",
                dry_run: bool = False,
            ) -> dict:
                return {
                    "customer": customer_id,
                    "priority": priority,
                    "dry_run": dry_run,
                }

        r = _app(C).post(
            "/2/orders",
            json={"product_id": 1, "quantity": 1},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["priority"] == "low"
        assert body["dry_run"] is False


# ===========================================================================
# § Query[Model] — model fields from query string
# ===========================================================================


class Filters(BaseModel):
    active: bool = True
    min_price: float = 0.0
    tags: list[str] = []


class TestQueryModelExtraction:
    """'Extracting a model from the query string' section."""

    def test_model_fields_from_query_string(self):
        @controller("/")
        class C:
            @get("/")
            async def list_items(self, f: Query[Filters]) -> dict:
                return {
                    "active": f.active,
                    "min_price": f.min_price,
                }

        r = _app(C).get("/?active=false&min_price=5.0")
        assert r.status_code == 200
        assert r.json() == {"active": False, "min_price": 5.0}

    def test_query_model_uses_defaults_when_absent(self):
        @controller("/")
        class C:
            @get("/")
            async def list_items(self, f: Query[Filters]) -> dict:
                return {"active": f.active, "min_price": f.min_price}

        r = _app(C).get("/")
        assert r.status_code == 200
        assert r.json() == {"active": True, "min_price": 0.0}


# ===========================================================================
# § Explicit markers still work alongside implicit
# ===========================================================================


class TestExplicitMarkersCoexistWithImplicit:
    """'Explicit markers still work' section."""

    def test_explicit_header_alongside_implicit_path_and_query(self):

        @controller("/")
        class C:
            @post("/{item_id}")
            async def update(
                self,
                item_id: int,  # implicit path
                body: CreateItem,  # implicit body (no default, before defaults)
                q: str = "",  # implicit query
            ) -> dict:
                return {
                    "item_id": item_id,
                    "q": q,
                    "name": body.name,
                }

        r = _app(C).post(
            "/42?q=test",
            json={"name": "widget", "price": 1.0},
        )
        assert r.status_code == 200
        assert r.json() == {"item_id": 42, "q": "test", "name": "widget"}

    def test_explicit_json_scalar(self):
        """Json[int] forces a scalar to come from the body instead of query."""

        @controller("/")
        class C:
            @post("/")
            async def h(self, count: Json[int]) -> dict:
                return {"count": count}

        r = _app(C).post("/", content=b"7", headers={"Content-Type": "application/json"})
        assert r.status_code == 200
        assert r.json() == {"count": 7}


# ===========================================================================
# § DI still runs first
# ===========================================================================


class TestDIRunsFirst:
    """'DI still runs first' section — a Pydantic model registered as a DI
    provider is injected via DI, NOT auto-promoted to a body parameter."""

    def test_pydantic_model_injected_via_di_not_body(self):
        @injectable()
        class Settings(BaseModel):
            debug: bool = False

        @controller("/")
        class C:
            @get("/")
            async def h(self, s: Settings) -> dict:
                return {"debug": s.debug}

        r = _app(C, providers=[Settings]).get("/")
        assert r.status_code == 200
        assert r.json() == {"debug": False}

    def test_pydantic_model_without_di_registration_becomes_body(self):
        """Same model, not registered → auto-promoted to JSON body."""

        class Payload(BaseModel):
            value: int

        @controller("/")
        class C:
            @post("/")
            async def h(self, p: Payload) -> dict:
                return {"value": p.value}

        r = _app(C).post("/", json={"value": 42})
        assert r.status_code == 200
        assert r.json() == {"value": 42}


# ===========================================================================
# § "What does NOT auto-promote" — error cases
# ===========================================================================


class TestNonPromotableTypes:
    """Negative cases from the 'What does NOT auto-promote' table."""

    def test_unannotated_parameter_raises_at_startup(self):
        with pytest.raises(Exception):  # UnresolvableParameterError or similar

            @controller("/")
            class C:
                @get("/")
                async def h(self, x) -> dict:  # no annotation
                    return {}

            _app(C)

    def test_list_of_non_scalar_raises_at_startup(self):
        """list[MyService] — element type is not scalar → UnresolvableParameterError."""

        class MyService:
            pass

        with pytest.raises(UnresolvableParameterError):

            @controller("/")
            class C:
                @get("/")
                async def h(self, items: list[MyService]) -> dict:
                    return {}

            _app(C)

    def test_custom_non_pydantic_class_raises_at_startup(self):
        """A non-injectable, non-Pydantic class → UnresolvableParameterError."""

        class MyClass:
            pass

        with pytest.raises(UnresolvableParameterError):

            @controller("/")
            class C:
                @get("/")
                async def h(self, obj: MyClass) -> dict:
                    return {}

            _app(C)
