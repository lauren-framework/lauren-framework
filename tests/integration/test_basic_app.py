"""End-to-end integration tests covering basic applications."""

from __future__ import annotations

import asyncio

from pydantic import BaseModel

from lauren import (
    Cookie,
    Header,
    Json,
    LaurenFactory,
    Path,
    Query,
    QueryField,
    Response,
    controller,
    delete,
    get,
    module,
    patch,
    post,
    put,
)
from lauren.testing import TestClient


# ---------------------------------------------------------------------------
# Fixtures / app builders
# ---------------------------------------------------------------------------


def build_app(root_module: type) -> TestClient:
    app = asyncio.run(LaurenFactory.create(root_module))
    return TestClient(app)


# ---------------------------------------------------------------------------
# Basic routing
# ---------------------------------------------------------------------------


@controller("/")
class RootController:
    @get("/")
    async def index(self) -> Response:
        return Response.text("hello")

    @get("/health")
    async def health(self) -> dict:
        return {"status": "ok"}


@module(controllers=[RootController])
class RootOnlyModule:
    pass


class TestBasicRouting:
    def test_root(self):
        client = build_app(RootOnlyModule)
        r = client.get("/")
        assert r.status_code == 200
        assert r.text == "hello"

    def test_nested_path(self):
        client = build_app(RootOnlyModule)
        r = client.get("/health")
        assert r.json() == {"status": "ok"}

    def test_404(self):
        client = build_app(RootOnlyModule)
        r = client.get("/nope")
        assert r.status_code == 404
        assert r.json()["error"]["code"] == "route_not_found"

    def test_method_not_allowed(self):
        client = build_app(RootOnlyModule)
        r = client.post("/health")
        assert r.status_code == 405
        assert "allow" in [k.lower() for k, _ in r.headers]


# ---------------------------------------------------------------------------
# Path / query / header / cookie extractors
# ---------------------------------------------------------------------------


@controller("/api")
class ApiController:
    @get("/users/{id}")
    async def get_user(self, id: Path[int]) -> Response:
        return Response.json({"id": id, "type": type(id).__name__})

    @get("/search")
    async def search(
        self,
        q: Query[str],
        page: Query[int] = QueryField(default=1, ge=1, le=100),
    ) -> Response:
        return Response.json({"q": q, "page": page})

    @get("/auth")
    async def auth(self, authorization: Header[str]) -> Response:
        return Response.json({"auth": authorization})

    @get("/session")
    async def session(self, session: Cookie[str]) -> Response:
        return Response.json({"session": session})

    @get("/tags")
    async def tags(self, tag: Query[list[str]]) -> Response:
        return Response.json({"tags": tag})


@module(controllers=[ApiController])
class ApiModule:
    pass


class TestExtractors:
    def test_path_int(self):
        client = build_app(ApiModule)
        r = client.get("/api/users/42")
        assert r.json() == {"id": 42, "type": "int"}

    def test_path_invalid_int_returns_422(self):
        client = build_app(ApiModule)
        r = client.get("/api/users/abc")
        assert r.status_code == 422

    def test_query_required_missing(self):
        client = build_app(ApiModule)
        r = client.get("/api/search")
        assert r.status_code == 422

    def test_query_valid(self):
        client = build_app(ApiModule)
        r = client.get("/api/search?q=python&page=3")
        assert r.json() == {"q": "python", "page": 3}

    def test_query_constraint_violation(self):
        client = build_app(ApiModule)
        r = client.get("/api/search?q=x&page=0")
        assert r.status_code == 422

    def test_query_default(self):
        client = build_app(ApiModule)
        r = client.get("/api/search?q=x")
        assert r.json() == {"q": "x", "page": 1}

    def test_query_list(self):
        client = build_app(ApiModule)
        r = client.get("/api/tags?tag=a&tag=b&tag=c")
        assert r.json() == {"tags": ["a", "b", "c"]}

    def test_header_extraction(self):
        client = build_app(ApiModule)
        r = client.get("/api/auth", headers={"Authorization": "Bearer xyz"})
        assert r.json() == {"auth": "Bearer xyz"}

    def test_cookie_extraction(self):
        client = build_app(ApiModule)
        r = client.get("/api/session", cookies={"session": "abc123"})
        assert r.json() == {"session": "abc123"}


# ---------------------------------------------------------------------------
# JSON body / Pydantic validation
# ---------------------------------------------------------------------------


class CreateUser(BaseModel):
    name: str
    age: int


@controller("/users")
class UserController:
    @post("/")
    async def create(self, user: Json[CreateUser]) -> Response:
        return Response.json({"name": user.name, "age": user.age}, status=201)

    @put("/{id}")
    async def update(self, id: Path[int], user: Json[CreateUser]) -> Response:
        return Response.json({"id": id, "name": user.name})

    @delete("/{id}")
    async def delete_user(self, id: Path[int]) -> Response:
        return Response.no_content()

    @patch("/{id}")
    async def patch_user(self, id: Path[int], body: Json[dict]) -> Response:
        return Response.json({"id": id, "patch": body})


@module(controllers=[UserController])
class UserModule:
    pass


class TestJsonBody:
    def test_post_valid(self):
        client = build_app(UserModule)
        r = client.post("/users/", json={"name": "Alice", "age": 30})
        assert r.status_code == 201
        assert r.json() == {"name": "Alice", "age": 30}

    def test_post_invalid(self):
        client = build_app(UserModule)
        r = client.post("/users/", json={"name": "Alice", "age": "thirty"})
        assert r.status_code == 422
        assert r.json()["error"]["code"].startswith("extractor")

    def test_post_missing_field(self):
        client = build_app(UserModule)
        r = client.post("/users/", json={"name": "Alice"})
        assert r.status_code == 422

    def test_put(self):
        client = build_app(UserModule)
        r = client.put("/users/5", json={"name": "Bob", "age": 25})
        assert r.status_code == 200
        assert r.json() == {"id": 5, "name": "Bob"}

    def test_patch(self):
        client = build_app(UserModule)
        r = client.patch("/users/7", json={"age": 50})
        assert r.json() == {"id": 7, "patch": {"age": 50}}

    def test_delete(self):
        client = build_app(UserModule)
        r = client.delete("/users/1")
        assert r.status_code == 204


# ---------------------------------------------------------------------------
# Response helpers
# ---------------------------------------------------------------------------


@controller("/resp")
class RespController:
    @get("/html")
    async def html(self) -> Response:
        return Response.html("<h1>hi</h1>")

    @get("/redirect")
    async def redir(self) -> Response:
        return Response.redirect("/dest", status=302)

    @get("/cookie")
    async def cookie(self) -> Response:
        return Response.json({"ok": True}).with_cookie(
            "session", "tok", http_only=True, secure=True
        )

    @get("/created")
    async def created(self) -> Response:
        return Response.created({"id": 1}, location="/resp/created/1")

    @get("/return-string")
    async def return_string(self) -> str:
        return "plain text"

    @get("/return-dict")
    async def return_dict(self) -> dict:
        return {"auto": "json"}

    @get("/return-none")
    async def return_none(self) -> None:
        return None


@module(controllers=[RespController])
class RespModule:
    pass


class TestResponses:
    def test_html(self):
        client = build_app(RespModule)
        r = client.get("/resp/html")
        assert r.header("content-type") and "text/html" in r.header("content-type")

    def test_redirect(self):
        client = build_app(RespModule)
        r = client.get("/resp/redirect")
        assert r.status_code == 302
        assert r.header("location") == "/dest"

    def test_cookie_set(self):
        client = build_app(RespModule)
        r = client.get("/resp/cookie")
        sc = r.header("set-cookie")
        assert sc and "session=tok" in sc and "HttpOnly" in sc

    def test_created_with_location(self):
        client = build_app(RespModule)
        r = client.get("/resp/created")
        assert r.status_code == 201
        assert r.header("location") == "/resp/created/1"

    def test_auto_string_response(self):
        client = build_app(RespModule)
        r = client.get("/resp/return-string")
        assert r.text == "plain text"

    def test_auto_dict_response(self):
        client = build_app(RespModule)
        r = client.get("/resp/return-dict")
        assert r.json() == {"auto": "json"}

    def test_auto_none_response(self):
        client = build_app(RespModule)
        r = client.get("/resp/return-none")
        assert r.status_code == 204
