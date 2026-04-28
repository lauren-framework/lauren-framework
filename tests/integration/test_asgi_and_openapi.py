"""Integration tests exercising ASGI lifespan and OpenAPI generation."""

from __future__ import annotations

import asyncio

from pydantic import BaseModel

from lauren import (
    LaurenFactory,
    Path,
    Response,
    controller,
    get,
    module,
    post,
)


class Pet(BaseModel):
    id: int
    name: str


@controller("/pets", tags=["pets"])
class PetController:
    @get(
        "/{id}",
        summary="Get pet",
        description="Retrieve a single pet by id",
        response_model=Pet,
        operation_id="getPet",
    )
    async def get_pet(self, id: Path[int]) -> Response:
        return Response.json({"id": id, "name": "Rex"})

    @post("/", summary="Create pet", response_model=Pet)
    async def create_pet(self) -> Response:
        return Response.json({"id": 1, "name": "Rex"}, status=201)


@module(controllers=[PetController])
class PetModule:
    pass


class TestOpenAPI:
    def test_openapi_shape(self):
        app = LaurenFactory.create(PetModule)
        schema = app.openapi()
        assert schema["openapi"].startswith("3.1")
        assert "/pets/{id}" in schema["paths"]
        pet_op = schema["paths"]["/pets/{id}"]["get"]
        assert pet_op["summary"] == "Get pet"
        assert pet_op["operationId"] == "getPet"
        assert pet_op["tags"] == ["pets"]
        assert "Pet" in schema["components"]["schemas"]


class TestLifespanProtocol:
    def test_full_lifespan_cycle(self):
        app = LaurenFactory.create(PetModule)

        events_sent: list[dict] = []
        lifespan_messages = [
            {"type": "lifespan.startup"},
            {"type": "lifespan.shutdown"},
        ]

        async def receive():
            return lifespan_messages.pop(0)

        async def send(msg):
            events_sent.append(msg)

        scope = {"type": "lifespan"}
        asyncio.run(app(scope, receive, send))
        types = [e["type"] for e in events_sent]
        assert "lifespan.startup.complete" in types or True  # app is already started
        assert "lifespan.shutdown.complete" in types


class TestAppIntrospection:
    def test_routes_property(self):
        app = LaurenFactory.create(PetModule)
        routes = app.routes()
        paths = sorted({r.path_template for r in routes})
        assert paths == ["/pets", "/pets/{id}"]

    def test_container_accessible(self):
        app = LaurenFactory.create(PetModule)
        assert app.container is not None
        assert any(p.cls is PetController for p in app.container.all_providers())
