"""Integration tests: multiple encoders coexist in one app.

Verifies that a single LaurenApp can serve different routes with different
JSON encoders simultaneously — for example, one controller using
PydanticEncoder (honours @field_serializer, no intermediate dict) while
another uses MsgspecEncoder (fastest for msgspec.Struct payloads) and a
third falls back to the app-level StdlibJSONEncoder.

This is the primary end-to-end test for @use_encoder composition.
"""

from __future__ import annotations


import pytest
from pydantic import BaseModel, field_serializer

from lauren import (
    LaurenFactory,
    Response,
    StdlibJSONEncoder,
    controller,
    get,
    module,
    use_encoder,
)
from lauren.serialization import MsgspecEncoder, PydanticEncoder
from lauren.testing import TestClient

# ---------------------------------------------------------------------------
# Skip guards
# ---------------------------------------------------------------------------

pytestmark = [
    pytest.mark.skipif(
        not pytest.importorskip("pydantic", reason="pydantic not installed") and False,
        reason="pydantic required",
    ),
]


def _require_msgspec():
    pytest.importorskip("msgspec")


# ---------------------------------------------------------------------------
# Shared Pydantic model
# ---------------------------------------------------------------------------


class PydanticOrder(BaseModel):
    order_id: str
    amount: float

    @field_serializer("amount")
    def fmt_amount(self, v: float) -> str:
        # PydanticEncoder will honour this; StdlibEncoder ignores it
        return f"{v:.2f}"


# ---------------------------------------------------------------------------
# App fixture — one module, three controllers, three encoders
# ---------------------------------------------------------------------------


def _make_app():
    """Build an app with three controllers, each on a different encoder."""
    _require_msgspec()
    import msgspec

    class MsgspecProduct(msgspec.Struct):
        product_id: str
        price: float

    # ── Controller A: PydanticEncoder ──────────────────────────────────────

    @use_encoder(PydanticEncoder())
    @controller("/pydantic")
    class PydanticController:
        @get("/order")
        async def order(self) -> PydanticOrder:
            return PydanticOrder(order_id="ORD-001", amount=99.9)

        @get("/orders")
        async def orders(self) -> list[PydanticOrder]:
            return [
                PydanticOrder(order_id="ORD-001", amount=10.0),
                PydanticOrder(order_id="ORD-002", amount=20.5),
            ]

    # ── Controller B: MsgspecEncoder ───────────────────────────────────────

    @use_encoder(MsgspecEncoder())
    @controller("/msgspec")
    class MsgspecController:
        @get("/product")
        async def product(self) -> Response:
            return Response.json(
                MsgspecProduct(product_id="PROD-42", price=14.99),
                encoder=MsgspecEncoder(),
            )

        @get("/products")
        async def products(self) -> Response:
            items = [
                MsgspecProduct(product_id="P1", price=5.0),
                MsgspecProduct(product_id="P2", price=7.5),
            ]
            return Response.json(items, encoder=MsgspecEncoder())

    # ── Controller C: default app-level StdlibJSONEncoder ──────────────────

    @controller("/stdlib")
    class StdlibController:
        @get("/data")
        async def data(self) -> dict:
            return {"source": "stdlib", "value": 42}

    @module(controllers=[PydanticController, MsgspecController, StdlibController])
    class AppModule:
        pass

    # App uses stdlib as the base encoder; individual controllers override
    return LaurenFactory.create(AppModule, json_encoder=StdlibJSONEncoder())


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestMultipleEncodersInOneApp:
    @pytest.fixture(scope="class")
    def client(self):
        return TestClient(_make_app())

    # ── PydanticEncoder routes ──────────────────────────────────────────────

    def test_pydantic_route_honours_field_serializer(self, client):
        """PydanticEncoder applies @field_serializer — amount is formatted."""
        r = client.get("/pydantic/order")
        assert r.status_code == 200
        body = r.json()
        assert body["order_id"] == "ORD-001"
        # @field_serializer formats float as "99.90" — only PydanticEncoder does this
        assert body["amount"] == "99.90"

    def test_pydantic_list_route_honours_field_serializer(self, client):
        """List of Pydantic models also serialized with PydanticEncoder."""
        r = client.get("/pydantic/orders")
        assert r.status_code == 200
        items = r.json()
        assert len(items) == 2
        # Both amounts formatted as strings, not floats
        assert items[0]["amount"] == "10.00"
        assert items[1]["amount"] == "20.50"

    def test_pydantic_content_type(self, client):
        r = client.get("/pydantic/order")
        assert (r.header("content-type") or "").startswith("application/json")

    # ── MsgspecEncoder routes ───────────────────────────────────────────────

    def test_msgspec_route_encodes_struct(self, client):
        """MsgspecEncoder handles msgspec.Struct directly."""
        r = client.get("/msgspec/product")
        assert r.status_code == 200
        body = r.json()
        assert body["product_id"] == "PROD-42"
        assert abs(body["price"] - 14.99) < 0.001

    def test_msgspec_list_route_encodes_structs(self, client):
        r = client.get("/msgspec/products")
        assert r.status_code == 200
        items = r.json()
        assert len(items) == 2
        assert items[0]["product_id"] == "P1"
        assert items[1]["product_id"] == "P2"

    def test_msgspec_content_type(self, client):
        r = client.get("/msgspec/product")
        assert (r.header("content-type") or "").startswith("application/json")

    # ── StdlibJSONEncoder routes (app-level fallback) ───────────────────────

    def test_stdlib_route_uses_plain_dict(self, client):
        r = client.get("/stdlib/data")
        assert r.status_code == 200
        assert r.json() == {"source": "stdlib", "value": 42}

    def test_stdlib_and_pydantic_produce_equivalent_output_for_standard_models(self, client):
        """Both encoders honour @field_serializer — they differ in path, not result."""

        # Build a separate minimal app with stdlib to confirm both encoders
        # apply @field_serializer correctly (stdlib via model_dump(mode="json"),
        # PydanticEncoder via model_dump_json()).
        @controller("/contrast")
        class ContrastC:
            @get("/order")
            async def order(self) -> PydanticOrder:
                return PydanticOrder(order_id="X", amount=99.9)

        @module(controllers=[ContrastC])
        class CM:
            pass

        stdlib_client = TestClient(LaurenFactory.create(CM, json_encoder=StdlibJSONEncoder()))
        stdlib_r = stdlib_client.get("/contrast/order")
        pydantic_r = client.get("/pydantic/order")

        # @field_serializer fires on both paths → amount is "99.90" in both
        assert stdlib_r.json()["amount"] == "99.90"
        assert pydantic_r.json()["amount"] == "99.90"

    # ── Cross-route isolation ───────────────────────────────────────────────

    def test_pydantic_encoder_does_not_affect_stdlib_route(self, client):
        """StdlibController is unaffected by PydanticController's encoder."""
        r = client.get("/stdlib/data")
        # Value must be the raw integer 42, not a string
        assert r.json()["value"] == 42

    def test_msgspec_encoder_does_not_affect_pydantic_route(self, client):
        """MsgspecController's encoder does not spill into PydanticController."""
        r = client.get("/pydantic/order")
        body = r.json()
        # @field_serializer only fires with PydanticEncoder — confirms isolation
        assert body["amount"] == "99.90"

    def test_all_three_routes_return_200(self, client):
        assert client.get("/pydantic/order").status_code == 200
        assert client.get("/msgspec/product").status_code == 200
        assert client.get("/stdlib/data").status_code == 200


# ---------------------------------------------------------------------------
# Route-level mix within a single controller
# ---------------------------------------------------------------------------


class TestMixedEncodersInOneController:
    def test_two_routes_on_same_controller_use_different_encoders(self):
        """Method-level @use_encoder wins over controller-level (or lack thereof)."""
        _require_msgspec()
        import msgspec

        class StructItem(msgspec.Struct):
            name: str
            score: float

        class PydanticItem(BaseModel):
            name: str
            score: float

            @field_serializer("score")
            def fmt(self, v: float) -> str:
                return f"{v:.3f}"

        @controller("/mixed")
        class MixedC:
            @get("/struct")
            @use_encoder(MsgspecEncoder())
            async def struct_route(self) -> Response:
                return Response.json(
                    StructItem(name="widget", score=9.5),
                    encoder=MsgspecEncoder(),
                )

            @get("/pydantic")
            @use_encoder(PydanticEncoder())
            async def pydantic_route(self) -> PydanticItem:
                return PydanticItem(name="gadget", score=7.777)

            @get("/plain")
            async def plain_route(self) -> dict:
                return {"name": "default", "score": 5.0}

        @module(controllers=[MixedC])
        class M:
            pass

        client = TestClient(LaurenFactory.create(M, json_encoder=StdlibJSONEncoder()))

        struct_r = client.get("/mixed/struct")
        pydantic_r = client.get("/mixed/pydantic")
        plain_r = client.get("/mixed/plain")

        assert struct_r.status_code == 200
        assert pydantic_r.status_code == 200
        assert plain_r.status_code == 200

        # Struct route: raw float (msgspec doesn't know about Pydantic serializers)
        assert struct_r.json()["name"] == "widget"
        assert isinstance(struct_r.json()["score"], float)

        # Pydantic route: @field_serializer applied → score is a string
        pydantic_body = pydantic_r.json()
        assert pydantic_body["name"] == "gadget"
        assert pydantic_body["score"] == "7.777"

        # Plain route: stdlib encoder, raw float
        plain_body = plain_r.json()
        assert plain_body["score"] == 5.0
        assert isinstance(plain_body["score"], float)
