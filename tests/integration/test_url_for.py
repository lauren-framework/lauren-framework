"""URL generation through a running app: Location headers, root paths, mounts, WebSockets."""

from __future__ import annotations

import pytest

from lauren import LaurenFactory, controller, get, injectable, module, post
from lauren.pagination import OffsetPagination, Page, paginate_sequence
from lauren.responses import Created
from lauren.testing import TestClient
from lauren.types import Request
from lauren.urls import ProxyConfig, UrlBuilder
from lauren.websockets import WebSocket, on_connect, ws_controller


@injectable()
class Mailer:
    def __init__(self, urls: UrlBuilder) -> None:
        self.urls = urls

    def welcome_link(self, user_id: int) -> str:
        return self.urls.url_for("users.show", id=user_id)


@controller("/users", name="users")
class UserController:
    def __init__(self, mailer: Mailer) -> None:
        self.mailer = mailer

    @post("/")
    async def create(self, request: Request) -> Created[dict]:
        return Created({"id": 7}, location=request.url_path_for(UserController.show, id=7))

    @get("/{id}", name="show")
    async def show(self, id: int, request: Request) -> dict:
        return {
            "id": id,
            "self": request.url_for("users.show", id=id),
            "path": request.url_path_for("users.show", id=id),
            "files": request.url_for("files", path="a b/c.txt"),
            "chat": request.ws_url_for(ChatGateway, room="lobby"),
        }

    @get("/{id}/welcome")
    async def welcome(self, id: int) -> dict:
        return {"link": self.mailer.welcome_link(id)}

    @get("/", name="list")
    async def list(self, paging: OffsetPagination) -> Page[int]:
        return paginate_sequence(range(50), paging)


@controller("/files")
class FileController:
    @get("/{*path}", name="files")
    async def read(self, path: str) -> dict:
        return {"path": path}


@ws_controller("/ws/chat/{room}", name="chat")
class ChatGateway:
    @on_connect
    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        await ws.send_json(
            {
                "self": ws.ws_url_for("chat", room="other"),
                "http": ws.url_for("users.show", id=1),
                "base": ws.base_url,
                "scheme": ws.scheme,
                "client": ws.client.host,
            }
        )


@module(controllers=[UserController, FileController, ChatGateway], providers=[Mailer])
class AppModule:
    pass


def test_created_location_comes_from_a_handler_reference() -> None:
    with TestClient(LaurenFactory.create(AppModule)) as client:
        response = client.post("/users/")

    assert response.status_code == 201
    assert response.header("location") == "/users/7"


def test_absolute_url_uses_the_request_scheme_and_host() -> None:
    app = LaurenFactory.create(AppModule)

    with TestClient(app, base_url="https://shop.example:8443") as client:
        body = client.get("/users/5").json()

    assert body["self"] == "https://shop.example:8443/users/5"
    assert body["path"] == "/users/5"
    assert body["files"] == "https://shop.example:8443/files/a%20b/c.txt"
    assert body["chat"] == "wss://shop.example:8443/ws/chat/lobby"


def test_generated_links_resolve_to_their_handlers() -> None:
    with TestClient(LaurenFactory.create(AppModule)) as client:
        body = client.get("/users/5").json()
        followed = client.get(body["self"]).json()

    assert followed["id"] == 5


def test_server_root_path_is_part_of_request_urls() -> None:
    with TestClient(LaurenFactory.create(AppModule), root_path="/backend") as client:
        body = client.get("/users/5").json()

    assert body["path"] == "/backend/users/5"
    assert body["self"] == "http://testserver/backend/users/5"


def test_configured_root_path_is_part_of_request_urls() -> None:
    with TestClient(LaurenFactory.create(AppModule, root_path="/api")) as client:
        response = client.post("/api/users/")

    assert response.header("location") == "/api/users/7"


def test_mounted_app_urls_include_the_mount_prefix() -> None:
    child = LaurenFactory.create(AppModule)

    @module()
    class ParentModule:
        pass

    parent = LaurenFactory.create(ParentModule, mounts={"/v1": child})
    with TestClient(parent) as client:
        body = client.get("/v1/users/5").json()
        location = client.post("/v1/users/").header("location")

    assert body["path"] == "/v1/users/5"
    assert location == "/v1/users/7"


def test_url_builder_uses_public_base_url_outside_requests() -> None:
    app = LaurenFactory.create(AppModule, public_base_url="https://api.example.com/backend")

    with TestClient(app) as client:
        link = client.get("/users/3/welcome").json()["link"]

    assert link == "https://api.example.com/backend/users/3"


def test_url_builder_is_injectable_from_the_container() -> None:
    app = LaurenFactory.create(AppModule)

    with TestClient(app) as client:
        urls = client.resolve(UrlBuilder)

    assert urls is app.urls
    assert urls.path_for("users.show", id=1) == "/users/1"


def test_pagination_links_use_public_base_url() -> None:
    app = LaurenFactory.create(AppModule, public_base_url="https://api.example.com")

    with TestClient(app) as client:
        response = client.get("/users/?limit=10")

    assert '<https://api.example.com/users/?limit=10&offset=10>; rel="next"' in response.header("link")


def test_explicit_pagination_base_url_wins_over_public_base_url() -> None:
    from lauren.pagination import PaginationConfig

    app = LaurenFactory.create(
        AppModule,
        public_base_url="https://api.example.com",
        pagination=PaginationConfig(base_url="https://cdn.example.com"),
    )

    with TestClient(app) as client:
        response = client.get("/users/?limit=10")

    assert "<https://cdn.example.com/users/" in response.header("link")


def test_pagination_links_follow_a_trusted_proxy_prefix() -> None:
    app = LaurenFactory.create(AppModule, proxy=ProxyConfig(trusted=["127.0.0.1"], trust_prefix=True))

    with TestClient(app) as client:
        response = client.get("/users/?limit=10", headers={"x-forwarded-prefix": "/edge"})

    assert "</edge/users/?limit=10&offset=10>" in response.header("link")


def test_websocket_urls_from_a_connection() -> None:
    app = LaurenFactory.create(AppModule)

    with TestClient(app, base_url="https://shop.example") as client:
        with client.websocket_connect("/ws/chat/lobby") as ws:
            body = ws.receive_json()

    assert body == {
        "self": "wss://shop.example/ws/chat/other",
        "http": "https://shop.example/users/1",
        "base": "wss://shop.example/",
        "scheme": "wss",
        "client": "127.0.0.1",
    }


def test_websocket_urls_behind_a_trusted_tls_proxy() -> None:
    app = LaurenFactory.create(AppModule, proxy=ProxyConfig(trusted=["10.0.0.0/8"]))
    headers = {
        "x-forwarded-for": "203.0.113.9",
        "x-forwarded-proto": "https",
        "x-forwarded-host": "chat.example.com",
    }

    with TestClient(app, client=("10.0.0.5", 4000)) as client:
        with client.websocket_connect("/ws/chat/lobby", headers=headers) as ws:
            body = ws.receive_json()

    assert body["self"] == "wss://chat.example.com/ws/chat/other"
    assert body["scheme"] == "wss"
    assert body["client"] == "203.0.113.9"


def test_unknown_route_name_is_a_server_error_not_a_wrong_url() -> None:
    @controller("/broken")
    class Broken:
        @get("/")
        async def index(self, request: Request) -> dict:
            return {"url": request.url_path_for("nope")}

    @module(controllers=[Broken])
    class BrokenModule:
        pass

    with TestClient(LaurenFactory.create(BrokenModule)) as client:
        response = client.get("/broken/")

    assert response.status_code == 500


@pytest.mark.parametrize(
    ("target", "params", "expected"),
    [
        ("users.show", {"id": 3}, "/users/3"),
        (UserController.show, {"id": 4}, "/users/4"),
        ("UserController.create", {}, "/users"),
        ("files", {"path": "x/y"}, "/files/x/y"),
    ],
)
def test_app_url_path_for(target: object, params: dict, expected: str) -> None:
    assert LaurenFactory.create(AppModule).url_path_for(target, **params) == expected


def test_mounted_child_strips_its_own_root_path_and_preserves_mount() -> None:
    child = LaurenFactory.create(AppModule, root_path="/api")

    @module()
    class ParentModule:
        pass

    parent = LaurenFactory.create(ParentModule, mounts={"/v1": child})
    with TestClient(parent) as client:
        body = client.get("/v1/api/users/5").json()
        location = client.post("/v1/api/users/").header("location")

    assert body["self"] == "http://testserver/v1/api/users/5"
    assert body["path"] == "/v1/api/users/5"
    assert location == "/v1/api/users/7"


def test_mounted_child_inherits_proxy_prefix_without_double_application() -> None:
    trust = ProxyConfig(trusted=["10.0.0.0/8"], trust_prefix=True)
    child = LaurenFactory.create(AppModule, proxy=trust)

    @module()
    class ParentModule:
        pass

    parent = LaurenFactory.create(ParentModule, proxy=trust, mounts={"/v1": child})
    forwarded = {
        "x-forwarded-for": "10.0.0.25",  # real client happens to be in a trusted CIDR
        "x-forwarded-proto": "https",
        "x-forwarded-host": "api.example.com",
        "x-forwarded-prefix": "/edge",
    }
    with TestClient(parent, client=("10.0.0.5", 4000)) as client:
        body = client.get("/v1/users/5", headers=forwarded).json()

    assert body["self"] == "https://api.example.com/edge/v1/users/5"


def test_parent_root_path_is_stripped_before_mount_matching() -> None:
    child = LaurenFactory.create(AppModule)

    @module()
    class ParentModule:
        pass

    parent = LaurenFactory.create(ParentModule, root_path="/edge", mounts={"/v1": child})
    with TestClient(parent) as client:
        body = client.get("/edge/v1/users/5").json()

    assert body["self"] == "http://testserver/edge/v1/users/5"
    assert body["path"] == "/edge/v1/users/5"
