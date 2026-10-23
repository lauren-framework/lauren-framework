"""Integration: ``status_code=`` on route decorators and typed results, end to end."""

from __future__ import annotations

import dataclasses
from typing import Annotated

import pytest

from lauren import (
    Accepted,
    Created,
    DecoratorUsageError,
    Lauren,
    LaurenFactory,
    NoContent,
    Ok,
    Response,
    Status,
    controller,
    delete,
    get,
    module,
    post,
    put,
)
from lauren.reflect import get_route_metadata, reflect_routes
from lauren.testing import TestClient


@dataclasses.dataclass
class Item:
    id: int
    name: str


@controller("/items")
class ItemController:
    @post("/", status_code=201)
    async def create(self) -> Item:
        return Item(1, "a")

    @post("/accept", status_code=202)
    async def accept(self) -> None:
        return None

    @post("/tuple", status_code=201)
    async def tuple_wins(self) -> Item:
        return Item(1, "a"), 299  # type: ignore[return-value]

    @post("/raw", status_code=201)
    async def raw(self) -> Response:
        return Response.text("teapot", status=418)

    @delete("/{id}")
    async def remove(self, id: int) -> None:
        return None

    @post("/created")
    async def created(self) -> Created[Item]:
        return Created(Item(2, "b"), location="/items/2")

    @put("/{id}")
    async def upsert(self, id: int) -> Ok[Item] | Created[Item]:
        if id == 1:
            return Ok(Item(1, "a"))
        return Created(Item(id, "new"), location=f"/items/{id}")

    @delete("/gone/{id}")
    async def gone(self, id: int) -> NoContent:
        return NoContent(headers={"X-Deleted": str(id)})

    @get("/partial")
    async def partial(self) -> Annotated[Item, Status(206, description="Partial")]:
        return Item(3, "c")

    @post("/job")
    async def job(self) -> Accepted[Item]:
        return Accepted(Item(4, "d"), location="/jobs/4")

    @get("/untyped")
    async def untyped(self):  # type: ignore[no-untyped-def]
        return Created({"id": 5}, location="/x/5")


@module(controllers=[ItemController])
class ItemModule:
    pass


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(LaurenFactory.create(ItemModule))


def test_declared_status_is_sent(client: TestClient) -> None:
    response = client.post("/items/")

    assert (response.status_code, response.json()) == (201, {"id": 1, "name": "a"})


def test_none_with_declared_status_sends_an_empty_body(client: TestClient) -> None:
    response = client.post("/items/accept")

    assert (response.status_code, response.body) == (202, b"")


def test_tuple_form_overrides_the_declared_status(client: TestClient) -> None:
    assert client.post("/items/tuple").status_code == 299


def test_response_instance_passes_through(client: TestClient) -> None:
    response = client.post("/items/raw")

    assert (response.status_code, response.text) == (418, "teapot")


def test_none_without_declared_status_is_204(client: TestClient) -> None:
    assert client.delete("/items/7").status_code == 204


def test_created_result_sends_201_and_location(client: TestClient) -> None:
    response = client.post("/items/created")

    assert (response.status_code, response.header("location")) == (201, "/items/2")


@pytest.mark.parametrize(("item_id", "status"), [(1, 200), (9, 201)])
def test_union_of_results_picks_the_returned_status(client: TestClient, item_id: int, status: int) -> None:
    assert client.put(f"/items/{item_id}").status_code == status


def test_no_content_result_keeps_its_headers(client: TestClient) -> None:
    response = client.delete("/items/gone/3")

    assert (response.status_code, response.body, response.header("x-deleted")) == (204, b"", "3")


def test_status_marker_sets_the_status(client: TestClient) -> None:
    assert client.get("/items/partial").status_code == 206


def test_accepted_result_sends_202(client: TestClient) -> None:
    response = client.post("/items/job")

    assert (response.status_code, response.header("location")) == (202, "/jobs/4")


def test_typed_results_work_on_untyped_routes(client: TestClient) -> None:
    response = client.get("/items/untyped")

    assert (response.status_code, response.header("location")) == (201, "/x/5")


# -- OpenAPI ------------------------------------------------------------------


@pytest.fixture(scope="module")
def paths() -> dict:
    return LaurenFactory.create(ItemModule).openapi()["paths"]


def test_openapi_documents_the_declared_status(paths: dict) -> None:
    responses = paths["/items"]["post"]["responses"]

    assert list(responses) == ["201"]
    assert responses["201"]["content"]["application/json"]["schema"] == {"$ref": "#/components/schemas/Item"}


def test_openapi_documents_none_as_204_without_content(paths: dict) -> None:
    assert paths["/items/{id}"]["delete"]["responses"] == {"204": {"description": "No Content"}}


def test_openapi_documents_every_member_of_a_result_union(paths: dict) -> None:
    responses = paths["/items/{id}"]["put"]["responses"]

    assert sorted(responses) == ["200", "201"]
    assert "Location" in responses["201"]["headers"]


def test_openapi_uses_the_status_description(paths: dict) -> None:
    assert paths["/items/partial"]["get"]["responses"]["206"]["description"] == "Partial"


def test_openapi_documents_no_content_results(paths: dict) -> None:
    assert paths["/items/gone/{id}"]["delete"]["responses"] == {"204": {"description": "No Content"}}


@controller("/docs-extra")
class DocsExtraController:
    @post(
        "/",
        status_code=201,
        response_headers={
            "Location": "URL of the thing",
            "X-Quota": {"description": "Requests left", "schema": {"type": "integer"}},
        },
    )
    async def create(self) -> dict:
        return {}

    @get("/unresolved")
    async def unresolved(self) -> "DoesNotExist":  # type: ignore[name-defined]  # noqa: F821
        return {}

    @get("/manual", responses={404: {"description": "Missing"}})
    async def manual(self) -> Item:
        return Item(1, "a")

    @get("/manual-untyped", responses={404: "Missing"})
    async def manual_untyped(self):  # type: ignore[no-untyped-def]
        return {}


@pytest.fixture(scope="module")
def extra_paths() -> dict:
    @module(controllers=[DocsExtraController])
    class DocsExtraModule:
        pass

    return LaurenFactory.create(DocsExtraModule).openapi()["paths"]


def test_openapi_documents_declared_response_headers(extra_paths: dict) -> None:
    headers = extra_paths["/docs-extra"]["post"]["responses"]["201"]["headers"]

    assert headers == {
        "Location": {"description": "URL of the thing", "schema": {"type": "string"}},
        "X-Quota": {"description": "Requests left", "schema": {"type": "integer"}},
    }


def test_openapi_documents_dict_bodies_as_objects(extra_paths: dict) -> None:
    content = extra_paths["/docs-extra"]["post"]["responses"]["201"]["content"]

    assert content == {"application/json": {"schema": {"type": "object"}}}


def test_openapi_leaves_unresolved_annotations_undescribed(extra_paths: dict) -> None:
    assert extra_paths["/docs-extra/unresolved"]["get"]["responses"] == {"200": {"description": "Success"}}


def test_openapi_adds_the_typed_success_next_to_manual_responses(extra_paths: dict) -> None:
    assert sorted(extra_paths["/docs-extra/manual"]["get"]["responses"]) == ["200", "404"]


def test_openapi_keeps_manual_responses_alone_for_untyped_routes(extra_paths: dict) -> None:
    assert extra_paths["/docs-extra/manual-untyped"]["get"]["responses"] == {
        "404": {"description": "Missing"}
    }


# -- reflect ------------------------------------------------------------------


def test_reflect_exposes_the_effective_status() -> None:
    by_name = {r.handler.__name__: r for r in reflect_routes(ItemController)}

    statuses = {name: by_name[name].status_code for name in ("create", "partial", "remove", "created")}

    assert statuses == {"create": 201, "partial": 206, "remove": 204, "created": 201}


def test_app_reflect_lists_typed_responses() -> None:
    app = LaurenFactory.create(ItemModule)
    TestClient(app)

    route = get_route_metadata(app, "PUT", "/items/{id}")

    assert route is not None
    assert [(r.status, r.body_type, r.source) for r in route.responses_typed] == [
        (200, Item, "declared"),
        (201, Item, "declared"),
    ]


# -- startup and decoration errors --------------------------------------------


def test_body_annotation_with_204_fails_at_startup() -> None:
    @controller("/bad")
    class BadController:
        @get("/", status_code=204)
        async def bad(self) -> Item:
            return Item(1, "a")

    @module(controllers=[BadController])
    class BadModule:
        pass

    with pytest.raises(DecoratorUsageError) as info:
        LaurenFactory.create(BadModule)

    assert info.value.detail["reason"] == "body_not_allowed"
    assert info.value.detail["target"].endswith("bad")


def test_status_contradicting_a_result_fails_at_startup() -> None:
    @controller("/bad")
    class BadController:
        @post("/", status_code=200)
        async def bad(self) -> Created[Item]:
            return Created(Item(1, "a"))

    @module(controllers=[BadController])
    class BadModule:
        pass

    with pytest.raises(DecoratorUsageError) as info:
        LaurenFactory.create(BadModule)

    assert info.value.detail["reason"] == "status_conflict"


# -- imperative Lauren API ----------------------------------------------------


def test_imperative_app_honours_status_code() -> None:
    app = Lauren(openapi_url=None, docs_url=None, redoc_url=None)

    @app.post("/things", status_code=201)
    async def create() -> dict:
        return {"ok": True}

    response = TestClient(app).post("/things")

    assert (response.status_code, response.json()) == (201, {"ok": True})
