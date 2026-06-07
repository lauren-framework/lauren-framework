"""E2E tests: /openapi.json served correctly via TestClient."""

import dataclasses
from typing import Literal, Optional, TypedDict

import pytest


@dataclasses.dataclass
class Product:
    """A physical product."""

    sku: str
    price: float
    weight_kg: Optional[float] = None


@dataclasses.dataclass
class CreateProductRequest:
    sku: str
    price: float


class OrderLine(TypedDict):
    product_sku: str
    qty: int


@pytest.fixture(scope="module")
def client():
    from lauren import Lauren, Discriminated, Json
    from lauren.testing import TestClient

    @dataclasses.dataclass
    class CatReq:
        kind: Literal["cat"] = "cat"
        name: str = ""

    @dataclasses.dataclass
    class DogReq:
        kind: Literal["dog"] = "dog"
        name: str = ""

    app = Lauren()

    @app.post("/products")
    async def create_product(body: CreateProductRequest) -> Product:
        return Product(sku=body.sku, price=body.price)

    @app.get("/products")
    async def list_products() -> list[Product]:
        return []

    @app.post("/orders")
    async def create_order(body: OrderLine) -> dict:
        return {}

    @app.post("/animals")
    async def create_animal(
        body: Json[Discriminated[CatReq | DogReq, "kind"]],  # noqa: F821
    ) -> dict:
        return {}

    return TestClient(app)


class TestOpenAPIEndpointHTTP:
    def test_returns_200(self, client):
        assert client.get("/openapi.json").status_code == 200

    def test_returns_json_content_type(self, client):
        resp = client.get("/openapi.json")
        assert resp.header("content-type").startswith("application/json")

    def test_openapi_version_field(self, client):
        spec = client.get("/openapi.json").json()
        assert spec["openapi"].startswith("3.")

    def test_paths_contains_all_routes(self, client):
        spec = client.get("/openapi.json").json()
        assert "/products" in spec["paths"]
        assert "/orders" in spec["paths"]
        assert "/animals" in spec["paths"]


class TestDataclassSchemaHTTP:
    def test_product_schema_in_components(self, client):
        spec = client.get("/openapi.json").json()
        assert "Product" in spec["components"]["schemas"]

    def test_product_schema_correct_type(self, client):
        spec = client.get("/openapi.json").json()
        assert spec["components"]["schemas"]["Product"]["type"] == "object"

    def test_product_schema_has_description(self, client):
        spec = client.get("/openapi.json").json()
        product = spec["components"]["schemas"]["Product"]
        assert "A physical product" in product.get("description", "")

    def test_product_schema_required_fields(self, client):
        spec = client.get("/openapi.json").json()
        product = spec["components"]["schemas"]["Product"]
        assert "sku" in product.get("required", [])
        assert "price" in product.get("required", [])
        assert "weight_kg" not in product.get("required", [])

    def test_list_products_response_is_array_ref(self, client):
        spec = client.get("/openapi.json").json()
        schema = spec["paths"]["/products"]["get"]["responses"]["200"]["content"]["application/json"][
            "schema"
        ]
        assert schema["type"] == "array"
        assert schema["items"] == {"$ref": "#/components/schemas/Product"}


class TestTypedDictSchemaHTTP:
    def test_order_line_in_components(self, client):
        spec = client.get("/openapi.json").json()
        assert "OrderLine" in spec["components"]["schemas"]

    def test_order_line_schema_has_both_fields(self, client):
        spec = client.get("/openapi.json").json()
        schema = spec["components"]["schemas"]["OrderLine"]
        assert "product_sku" in schema["properties"]
        assert "qty" in schema["properties"]


class TestDiscriminatedSchemaHTTP:
    def test_animals_has_oneof(self, client):
        spec = client.get("/openapi.json").json()
        schema = spec["paths"]["/animals"]["post"]["requestBody"]["content"]["application/json"]["schema"]
        assert "oneOf" in schema

    def test_animals_discriminator_property(self, client):
        spec = client.get("/openapi.json").json()
        schema = spec["paths"]["/animals"]["post"]["requestBody"]["content"]["application/json"]["schema"]
        assert schema["discriminator"]["propertyName"] == "kind"


class TestSpecValidatorHTTP:
    def test_spec_passes_openapi_validator(self, client):
        openapi_spec_validator = pytest.importorskip("openapi_spec_validator")
        spec = client.get("/openapi.json").json()
        openapi_spec_validator.validate(spec)
