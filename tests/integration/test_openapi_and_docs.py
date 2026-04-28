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
