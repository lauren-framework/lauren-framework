"""E2E: an ``error_format="rfc9457"`` app across validation backends, domain errors and auth (F05).

One app exercises the dataclass, TypedDict and (when installed) pydantic body
validators, a domain error family with ``problem_type_base`` URLs, a guard that
raises ``UnauthorizedError(challenge=...)`` and a guard that declares
``deny_error`` — all rendered as RFC 9457 problem documents.
"""

from __future__ import annotations

import base64
import dataclasses
from typing import Any, TypedDict

import pytest

from lauren import (
    ConflictError,
    HTTPError,
    Json,
    LaurenFactory,
    NotFoundError,
    UnauthorizedError,
    controller,
    get,
    module,
    post,
    use_guards,
)
from lauren.testing import TestClient

BASE = "https://errors.example.com/"


@dataclasses.dataclass
class Book:
    title: str
    pages: int = 0


class Search(TypedDict):
    q: str
    limit: int


class BookNotFound(NotFoundError):
    code = "book_not_found"


class DuplicateTitle(ConflictError):
    code = "duplicate_title"
    problem_type = "https://docs.example.com/problems/duplicate-title"
    problem_title = "A book with this title already exists"


class BasicAuth:
    """Raises a 401 with its challenge — no extra exception handler needed."""

    async def can_activate(self, ctx: Any) -> bool:
        header = ctx.request.headers.get("authorization", "")
        if header == "Basic " + base64.b64encode(b"admin:pw").decode():
            return True
        raise UnauthorizedError("credentials required", challenge='Basic realm="library"')


class ApiKey:
    deny_error = UnauthorizedError

    async def can_activate(self, ctx: Any) -> bool:
        return ctx.request.headers.get("x-api-key") == "k"


@controller("/books")
class BookController:
    @post("/")
    async def create(self, body: Json[Book]) -> dict:
        if body.title == "Dune":
            raise DuplicateTitle("Dune is already in the catalog", detail={"title": body.title})
        return {"title": body.title}

    @post("/search")
    async def search(self, body: Json[Search]) -> dict:
        return dict(body)

    @get("/{book_id}")
    async def show(self, book_id: int) -> dict:
        raise BookNotFound(f"book {book_id} does not exist", detail={"id": book_id})

    @get("/admin/stats")
    @use_guards(BasicAuth)
    async def stats(self) -> dict:
        return {"books": 1}

    @get("/keyed/stats")
    @use_guards(ApiKey)
    async def keyed(self) -> dict:
        return {"books": 1}


@module(controllers=[BookController])
class LibraryModule:
    pass


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(LaurenFactory.create(LibraryModule, error_format="rfc9457", problem_type_base=BASE))


def _assert_problem(r: Any, status: int) -> dict[str, Any]:
    assert r.status_code == status
    assert r.header("content-type") == "application/problem+json"
    body = r.json()
    assert {"type", "title", "status", "detail", "instance", "code"} <= set(body)
    assert body["status"] == status
    return body


def test_dataclass_validation_failure_has_an_errors_array(client: TestClient) -> None:
    body = _assert_problem(client.post("/books/", json={"pages": 3}), 422)

    assert body["type"] == BASE + "extractor_error"
    assert body["errors"] and body["errors"][0]["parameter"] == "body"
    assert "title" in body["errors"][0]["detail"]


def test_typeddict_validation_failure_has_an_errors_array(client: TestClient) -> None:
    body = _assert_problem(client.post("/books/search", json={"q": "dune"}), 422)

    assert body["errors"] == [{"detail": "Missing required keys: ['limit']", "parameter": "body"}]


@pytest.mark.pydantic
def test_pydantic_validation_failure_has_an_errors_array() -> None:
    pydantic = pytest.importorskip("pydantic")

    class Pet(pydantic.BaseModel):
        name: str
        age: int

    @controller("/pets")
    class PetController:
        @post("/")
        async def create(self, body: Json[Pet]) -> dict:
            return body.model_dump()

    @module(controllers=[PetController])
    class PetModule:
        pass

    client = TestClient(LaurenFactory.create(PetModule, error_format="rfc9457"))

    body = _assert_problem(client.post("/pets/", json={"name": "Rex", "age": "old"}), 422)

    assert body["errors"][0]["parameter"] == "body"
    assert "int_parsing" in body["errors"][0]["detail"]


def test_domain_error_uses_the_problem_type_base(client: TestClient) -> None:
    body = _assert_problem(client.get("/books/42"), 404)

    assert body["type"] == BASE + "book_not_found"
    assert body["instance"] == "/books/42"
    assert body["context"] == {"id": 42}
    assert "errors" not in body


def test_problem_type_override_wins_over_the_base(client: TestClient) -> None:
    body = _assert_problem(client.post("/books/", json={"title": "Dune"}), 409)

    assert body["type"] == "https://docs.example.com/problems/duplicate-title"
    assert body["title"] == "A book with this title already exists"


def test_basic_auth_challenge_needs_no_exception_handler(client: TestClient) -> None:
    r = client.get("/books/admin/stats")

    body = _assert_problem(r, 401)
    assert r.header("www-authenticate") == 'Basic realm="library"'
    assert body["code"] == "unauthorized"


def test_basic_auth_admits_valid_credentials(client: TestClient) -> None:
    token = base64.b64encode(b"admin:pw").decode()

    assert client.get("/books/admin/stats", headers={"authorization": f"Basic {token}"}).json() == {
        "books": 1
    }


def test_deny_error_guard_answers_401_problem(client: TestClient) -> None:
    body = _assert_problem(client.get("/books/keyed/stats"), 401)

    assert body["type"] == BASE + "unauthorized"
    assert body["context"] == {"guard": "ApiKey"}


def test_routing_miss_is_a_problem_document(client: TestClient) -> None:
    body = _assert_problem(client.get("/nowhere"), 404)

    assert body["code"] == "route_not_found"


def test_instance_override_of_status_and_code(client: TestClient) -> None:
    @controller("/teapot")
    class Teapot:
        @get("/")
        async def brew(self) -> dict:
            raise HTTPError("I'm a teapot", status_code=418, code="teapot")

    @module(controllers=[Teapot])
    class TeapotModule:
        pass

    r = TestClient(LaurenFactory.create(TeapotModule, error_format="rfc9457")).get("/teapot/")

    body = _assert_problem(r, 418)
    assert (body["title"], body["code"]) == ("I'm a Teapot", "teapot")
