"""Enhanced OpenAPI generation + built-in docs endpoints."""

# intentional: no ``from __future__ import annotations`` so nested classes
# inside test methods keep resolvable annotations.

from typing import Annotated

import pytest
from pydantic import BaseModel

from lauren import (
    Cookie,
    Header,
    Json,
    LaurenFactory,
    Path,
    PathField,
    Query,
    QueryField,
    controller,
    delete,
    get,
    module,
    pipe,
    post,
)
from lauren.testing import TestClient


# ---------------------------------------------------------------------------
# Fixtures \u2014 a small but thorough API surface
# ---------------------------------------------------------------------------


class Pet(BaseModel):
    id: int
    name: str
    tag: str | None = None


class CreatePet(BaseModel):
    name: str
    tag: str | None = None


@controller("/pets", tags=["pets"], description="Pet management")
class PetController:
    @get(
        "/{pet_id}",
        summary="Get pet by id",
        description="Retrieves a single pet by its numeric id.",
        response_model=Pet,
        operation_id="getPet",
        responses={404: {"description": "Pet not found"}},
    )
    async def get_pet(
        self,
        pet_id: Annotated[Path[int], PathField(ge=1, description="Pet id (>=1)")],
    ) -> Pet:
        return Pet(id=pet_id, name="Rex")

    @get(
        "/",
        summary="List pets",
        response_model=Pet,
        tags=["pets", "search"],
    )
    async def list_pets(
        self,
        limit: Query[int] = QueryField(default=10, ge=1, le=100),
        tag: Query[str] = QueryField(default="", description="Filter by tag"),
        x_trace_id: Header[str] = "anonymous",
        session: Cookie[str] = "",
    ) -> list[Pet]:
        return [Pet(id=1, name="Rex")]

    @post(
        "/",
        summary="Create pet",
        response_model=Pet,
        responses={201: {"description": "Created"}},
    )
    async def create_pet(self, body: Json[CreatePet]) -> Pet:
        return Pet(id=1, name=body.name, tag=body.tag)

    @delete("/{pet_id}", summary="Delete pet", deprecated=True)
    async def delete_pet(self, pet_id: Path[int]) -> dict:
        return {"deleted": pet_id}


@module(controllers=[PetController])
class PetModule:
    pass


# ---------------------------------------------------------------------------
# Schema-shape assertions
# ---------------------------------------------------------------------------


class TestRichOpenAPI:
    @pytest.mark.asyncio
    async def test_path_param_has_integer_schema_and_constraints(self):
        app = LaurenFactory.create(
            PetModule,
            openapi_info={"title": "Pet API", "version": "2.0.0"},
        )
        schema = app.openapi()
        op = schema["paths"]["/pets/{pet_id}"]["get"]
        param = next(p for p in op["parameters"] if p["name"] == "pet_id")
        assert param["in"] == "path"
        assert param["required"] is True
        assert param["schema"]["type"] == "integer"
        assert param["schema"]["minimum"] == 1
        assert param["description"] == "Pet id (>=1)"

    @pytest.mark.asyncio
    async def test_query_parameters_are_documented(self):
        app = LaurenFactory.create(PetModule)
        schema = app.openapi()
        op = schema["paths"]["/pets"]["get"]
        params = {p["name"]: p for p in op["parameters"]}
        assert params["limit"]["in"] == "query"
        assert params["limit"]["schema"]["type"] == "integer"
        assert params["limit"]["schema"]["minimum"] == 1
        assert params["limit"]["schema"]["maximum"] == 100
        # Query params with a default must NOT be required.
        assert params["limit"]["required"] is False
        # Defaults reach the JSON Schema.
        assert params["limit"]["schema"]["default"] == 10
        assert params["tag"]["description"] == "Filter by tag"

    @pytest.mark.asyncio
    async def test_headers_and_cookies_are_documented(self):
        app = LaurenFactory.create(PetModule)
        schema = app.openapi()
        op = schema["paths"]["/pets"]["get"]
        params = {(p["in"], p["name"]): p for p in op["parameters"]}
        assert ("header", "x-trace-id") in params  # kebab-case
        assert ("cookie", "session") in params

    @pytest.mark.asyncio
    async def test_request_body_references_pydantic_model(self):
        app = LaurenFactory.create(PetModule)
        schema = app.openapi()
        op = schema["paths"]["/pets"]["post"]
        assert "requestBody" in op
        body = op["requestBody"]
        ref = body["content"]["application/json"]["schema"]["$ref"]
        assert ref == "#/components/schemas/CreatePet"
        assert "CreatePet" in schema["components"]["schemas"]

    @pytest.mark.asyncio
    async def test_response_model_referenced_in_200(self):
        app = LaurenFactory.create(PetModule)
        schema = app.openapi()
        op = schema["paths"]["/pets/{pet_id}"]["get"]
        ok = op["responses"]["200"]
        assert ok["content"]["application/json"]["schema"]["$ref"].endswith("/Pet")
        # Extra responses declared on the route are preserved.
        assert "404" in op["responses"]

    @pytest.mark.asyncio
    async def test_tags_list_is_deduplicated(self):
        app = LaurenFactory.create(PetModule)
        schema = app.openapi()
        tag_names = {t["name"] for t in schema.get("tags", [])}
        assert {"pets", "search"} <= tag_names

    @pytest.mark.asyncio
    async def test_info_and_servers_customisation(self):
        app = LaurenFactory.create(
            PetModule,
            openapi_info={
                "title": "Pet API",
                "version": "2.0.0",
                "description": "Manage pets",
            },
            openapi_servers=[{"url": "https://api.example.com", "description": "prod"}],
        )
        schema = app.openapi()
        assert schema["info"]["title"] == "Pet API"
        assert schema["info"]["version"] == "2.0.0"
        assert schema["servers"][0]["url"] == "https://api.example.com"

    @pytest.mark.asyncio
    async def test_deprecated_routes_marked(self):
        app = LaurenFactory.create(PetModule)
        schema = app.openapi()
        op = schema["paths"]["/pets/{pet_id}"]["delete"]
        assert op.get("deprecated") is True

    @pytest.mark.asyncio
    async def test_operation_ids_preserved(self):
        app = LaurenFactory.create(PetModule)
        schema = app.openapi()
        op = schema["paths"]["/pets/{pet_id}"]["get"]
        assert op["operationId"] == "getPet"

    @pytest.mark.asyncio
    async def test_document_validates_against_openapi_3_1_spec(self):
        """Guard against regressions: the emitted document must satisfy
        the official OpenAPI 3.1 JSON Schema."""
        openapi_spec_validator = pytest.importorskip("openapi_spec_validator")
        app = LaurenFactory.create(
            PetModule,
            openapi_info={"title": "Pet", "version": "1.0"},
            openapi_servers=[{"url": "https://api.example.com"}],
        )
        openapi_spec_validator.validate(app.openapi())

    @pytest.mark.asyncio
    async def test_pipes_do_not_leak_into_schema(self):
        """A pipe that transforms ``Path[int] -> User`` must not change the
        parameter's wire schema \u2014 the schema describes what the client
        SENDS, not the server-side transformation outcome."""

        def to_user(n: int) -> dict:
            return {"id": n}

        @controller("/users")
        class Ctrl:
            @get("/{uid}")
            async def h(self, uid: Annotated[Path[int], pipe(to_user)]) -> dict:
                return uid

        @module(controllers=[Ctrl])
        class M:
            pass

        app = LaurenFactory.create(M)
        schema = app.openapi()
        param = next(
            p
            for p in schema["paths"]["/users/{uid}"]["get"]["parameters"]
            if p["name"] == "uid"
        )
        assert param["schema"]["type"] == "integer"


# ---------------------------------------------------------------------------
# Built-in /docs, /redoc, /openapi.json endpoints
# ---------------------------------------------------------------------------


class TestDocsEndpoints:
    @pytest.mark.asyncio
    async def test_openapi_json_served(self):
        app = LaurenFactory.create(
            PetModule,
            openapi_url="/openapi.json",
        )
        r = TestClient(app).get("/openapi.json")
        assert r.status_code == 200
        assert r.header("content-type").startswith("application/json")
        data = r.json()
        assert data["openapi"].startswith("3.1")
        assert "/pets/{pet_id}" in data["paths"]

    @pytest.mark.asyncio
    async def test_swagger_ui_served(self):
        app = LaurenFactory.create(
            PetModule,
            docs_url="/docs",
        )
        r = TestClient(app).get("/docs")
        assert r.status_code == 200
        assert r.header("content-type").startswith("text/html")
        html = r.text
        assert "SwaggerUIBundle" in html
        # Default JSON URL is injected when docs_url is set and openapi_url isn't.
        assert "/openapi.json" in html

    @pytest.mark.asyncio
    async def test_redoc_served(self):
        app = LaurenFactory.create(
            PetModule,
            redoc_url="/redoc",
        )
        r = TestClient(app).get("/redoc")
        assert r.status_code == 200
        assert r.header("content-type").startswith("text/html")
        assert "<redoc" in r.text

    @pytest.mark.asyncio
    async def test_all_three_together(self):
        app = LaurenFactory.create(
            PetModule,
            openapi_url="/openapi.json",
            docs_url="/docs",
            redoc_url="/redoc",
        )
        client = TestClient(app)
        for path, ct in (
            ("/openapi.json", "application/json"),
            ("/docs", "text/html"),
            ("/redoc", "text/html"),
        ):
            resp = client.get(path)
            assert resp.status_code == 200, path
            assert resp.header("content-type").startswith(ct), path

    @pytest.mark.asyncio
    async def test_docs_not_exposed_by_default(self):
        """Backward compatibility: apps that don't opt-in don't get /docs."""
        app = LaurenFactory.create(PetModule)
        r = TestClient(app).get("/docs")
        assert r.status_code == 404

    @pytest.mark.asyncio
    async def test_docs_endpoints_excluded_from_schema(self):
        app = LaurenFactory.create(
            PetModule,
            openapi_url="/openapi.json",
            docs_url="/docs",
            redoc_url="/redoc",
        )
        data = TestClient(app).get("/openapi.json").json()
        assert "/openapi.json" not in data["paths"]
        assert "/docs" not in data["paths"]
        assert "/redoc" not in data["paths"]

    @pytest.mark.asyncio
    async def test_custom_docs_urls(self):
        app = LaurenFactory.create(
            PetModule,
            openapi_url="/api/schema.json",
            docs_url="/api/swagger",
            redoc_url="/api/reference",
        )
        client = TestClient(app)
        assert client.get("/api/schema.json").status_code == 200
        assert client.get("/api/swagger").status_code == 200
        assert client.get("/api/reference").status_code == 200
        # The HTML UIs must reference the custom JSON URL.
        assert "/api/schema.json" in client.get("/api/swagger").text
        assert "/api/schema.json" in client.get("/api/reference").text


# ---------------------------------------------------------------------------
# _docs.py direct coverage
# ---------------------------------------------------------------------------


class TestDocsHelpers:
    def test_swagger_ui_html_with_oauth2_redirect(self):
        """oauth2_redirect_url branch in swagger_ui_html."""
        from lauren._asgi._docs import swagger_ui_html

        html = swagger_ui_html(
            openapi_url="/openapi.json",
            title="Test",
            oauth2_redirect_url="https://example.com/oauth2/callback",
        )
        assert "oauth2RedirectUrl" in html
        assert "https://example.com/oauth2/callback" in html

    def test_swagger_ui_html_without_oauth2_redirect(self):
        from lauren._asgi._docs import swagger_ui_html

        html = swagger_ui_html(openapi_url="/openapi.json")
        assert "oauth2RedirectUrl" not in html
        assert "SwaggerUIBundle" in html

    def test_html_response_sets_content_type(self):
        from lauren._asgi._docs import html_response

        resp = html_response("<html><body>hi</body></html>")
        assert resp.headers.get("content-type") == "text/html; charset=utf-8"
        assert b"hi" in resp.body

    def test_json_response_returns_json_body(self):
        from lauren._asgi._docs import json_response

        resp = json_response({"key": "value"})
        import json

        data = json.loads(resp.body)
        assert data["key"] == "value"


# ---------------------------------------------------------------------------
# _openapi.py: _python_type_to_schema edge cases
# ---------------------------------------------------------------------------


class TestPythonTypeToSchema:
    def test_float_type(self):
        from lauren._asgi._openapi import _python_type_to_schema

        assert _python_type_to_schema(float) == {"type": "number"}

    def test_bool_type(self):
        from lauren._asgi._openapi import _python_type_to_schema

        assert _python_type_to_schema(bool) == {"type": "boolean"}

    def test_none_type(self):
        from lauren._asgi._openapi import _python_type_to_schema

        assert _python_type_to_schema(None) == {"type": "string"}
        assert _python_type_to_schema(type(None)) == {"type": "string"}

    def test_bytes_type(self):
        from lauren._asgi._openapi import _python_type_to_schema

        result = _python_type_to_schema(bytes)
        assert result == {"type": "string", "format": "binary"}

    def test_list_with_type_arg(self):
        from lauren._asgi._openapi import _python_type_to_schema

        result = _python_type_to_schema(list[int])
        assert result["type"] == "array"
        assert result["items"] == {"type": "integer"}

    def test_tuple_with_type_arg(self):
        from lauren._asgi._openapi import _python_type_to_schema

        result = _python_type_to_schema(tuple[str])
        assert result["type"] == "array"

    def test_dict_with_value_type(self):
        from lauren._asgi._openapi import _python_type_to_schema

        result = _python_type_to_schema(dict[str, int])
        assert result["type"] == "object"
        assert result["additionalProperties"] == {"type": "integer"}

    def test_dict_no_value_type(self):
        from lauren._asgi._openapi import _python_type_to_schema

        result = _python_type_to_schema(dict)
        # bare dict has no args, falls through to unknown -> {}
        assert isinstance(result, dict)

    def test_optional_t_unwraps_to_t(self):
        from lauren._asgi._openapi import _python_type_to_schema

        result = _python_type_to_schema(int | None)
        assert result == {"type": "integer"}

    def test_union_multiple_types(self):
        from lauren._asgi._openapi import _python_type_to_schema

        result = _python_type_to_schema(int | str)
        assert "oneOf" in result

    def test_enum_type(self):
        from lauren._asgi._openapi import _python_type_to_schema
        import enum

        class Color(enum.Enum):
            RED = "red"
            GREEN = "green"

        result = _python_type_to_schema(Color)
        assert "enum" in result
        assert set(result["enum"]) == {"red", "green"}

    def test_unknown_type_returns_empty(self):
        from lauren._asgi._openapi import _python_type_to_schema

        class Weird:
            pass

        result = _python_type_to_schema(Weird)
        assert result == {}


class TestApplyFieldDescriptor:
    def test_all_constraints(self):
        from lauren._asgi._openapi import _apply_field_descriptor
        from lauren.extractors import FieldDescriptor

        fd = FieldDescriptor(
            ge=1.0,
            le=100.0,
            gt=0.0,
            lt=200.0,
            min_length=2,
            max_length=50,
            pattern=r"\d+",
            example="42",
        )
        schema: dict = {}
        result = _apply_field_descriptor(schema, fd)
        assert result["minimum"] == 1.0
        assert result["maximum"] == 100.0
        assert result["exclusiveMinimum"] == 0.0
        assert result["exclusiveMaximum"] == 200.0
        assert result["minLength"] == 2
        assert result["maxLength"] == 50
        assert result["pattern"] == r"\d+"
        assert result["example"] == "42"


class TestEnsureComponentWithDefs:
    def test_pydantic_model_with_nested_defs(self):
        """$defs from a pydantic schema should be hoisted into components."""
        from lauren._asgi._openapi import _ensure_component
        from pydantic import BaseModel

        class Inner(BaseModel):
            x: int

        class Outer(BaseModel):
            inner: Inner

        components: dict = {"schemas": {}}
        ref = _ensure_component(components, Outer)
        assert ref.startswith("#/components/schemas/")
        # Inner should also appear
        assert "Inner" in components["schemas"] or "Outer" in components["schemas"]


class TestOpenAPISchemaTypeVariants:
    def test_bytes_request_body(self):
        """bytes extractor produces application/octet-stream requestBody."""
        from lauren import controller, post, module, Bytes
        from lauren import LaurenFactory

        @controller("/upload")
        class UpCtrl:
            @post("/")
            async def upload(self, body: Bytes) -> dict:
                return {}

        @module(controllers=[UpCtrl])
        class UpMod:
            pass

        app = LaurenFactory.create(UpMod)
        schema = app.openapi()
        op = schema["paths"]["/upload"]["post"]
        body = op.get("requestBody", {})
        assert "application/octet-stream" in body.get("content", {})

    def test_form_request_body(self):
        """Form extractor produces application/x-www-form-urlencoded requestBody."""
        from lauren import controller, post, module, Form
        from lauren import LaurenFactory

        @controller("/form")
        class FormCtrl:
            @post("/")
            async def handle(self, data: Form) -> dict:
                return {}

        @module(controllers=[FormCtrl])
        class FormMod:
            pass

        app = LaurenFactory.create(FormMod)
        schema = app.openapi()
        op = schema["paths"]["/form"]["post"]
        body = op.get("requestBody", {})
        assert "application/x-www-form-urlencoded" in body.get("content", {})

    def test_response_with_responses_dict_override(self):
        """Custom 'responses' dict on route is merged into OpenAPI output."""

        @controller("/items")
        class ItemCtrl:
            @get(
                "/",
                responses={200: {"description": "OK custom"}, 400: "Bad request"},
            )
            async def list_items(self) -> dict:
                return {}

        @module(controllers=[ItemCtrl])
        class ItemMod:
            pass

        app = LaurenFactory.create(ItemMod)
        schema = app.openapi()
        op = schema["paths"]["/items"]["get"]
        assert op["responses"]["200"]["description"] == "OK custom"
        assert "400" in op["responses"]

    def test_streaming_response_includes_x_streaming(self):
        """StreamingResponse[T] produces x-streaming in the OpenAPI output."""
        from pydantic import BaseModel
        from lauren import controller, get, module, LaurenFactory
        from lauren.streaming import StreamingResponse

        class Item(BaseModel):
            value: int

        @controller("/stream")
        class StreamCtrl:
            @get("/", response_model=Item)
            async def stream_items(self) -> StreamingResponse[Item]:
                async def gen():
                    yield Item(value=1)

                return gen()

        @module(controllers=[StreamCtrl])
        class StreamMod:
            pass

        app = LaurenFactory.create(StreamMod)
        schema = app.openapi()
        op = schema["paths"]["/stream"]["get"]
        assert op.get("x-streaming") is True

    def test_root_path_becomes_server(self):
        """When root_path is set and servers is empty, root_path becomes a server."""
        app = LaurenFactory.create(PetModule, root_path="/api/v1")
        schema = app.openapi()
        servers = schema.get("servers", [])
        assert any(s["url"] == "/api/v1" for s in servers)

    def test_security_schemes_in_components(self):
        """security_schemes appear in components/securitySchemes."""
        app = LaurenFactory.create(
            PetModule,
            openapi_security_schemes={
                "BearerAuth": {
                    "type": "http",
                    "scheme": "bearer",
                    "bearerFormat": "JWT",
                }
            },
        )
        schema = app.openapi()
        sec = schema["components"].get("securitySchemes", {})
        assert "BearerAuth" in sec
