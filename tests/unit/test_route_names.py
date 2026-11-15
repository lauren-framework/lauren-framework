"""Route names: explicit and derived names, namespaces, syntax and uniqueness."""

from __future__ import annotations

import pytest

from lauren import Lauren, LaurenFactory, controller, get, module, post
from lauren.exceptions import DecoratorUsageError, RouterConflictError
from lauren.reflect import get_all_routes, get_all_ws_gateways
from lauren.testing import TestClient
from lauren.websockets import WebSocket, on_connect, ws_controller


def _names(app) -> dict[str, str]:
    return {r.name: f"{r.method} {r.path_template}" for r in app._urls._routes.http.routes if r.name}


# ---------------------------------------------------------------------------
# Derived and explicit names
# ---------------------------------------------------------------------------


@controller("/users", name="users")
class UserController:
    @get("/{id}", name="show")
    async def show(self, id: int) -> dict:
        return {"id": id}

    @post("/")
    async def create(self) -> dict:
        return {}


@controller("/orders")
class OrderController:
    @get("/", name="orders_list")
    async def list(self) -> list:
        return []

    @get("/{id}")
    async def show(self, id: int) -> dict:
        return {"id": id}


@module(controllers=[UserController, OrderController])
class NamesModule:
    pass


def test_explicit_route_name_is_namespaced_by_the_controller_name() -> None:
    app = LaurenFactory.create(NamesModule)

    assert _names(app)["users.show"] == "GET /users/{id}"


def test_route_without_name_gets_the_derived_class_dot_method_name() -> None:
    app = LaurenFactory.create(NamesModule)

    assert _names(app)["UserController.create"] == "POST /users"


def test_explicit_name_on_an_unnamed_controller_is_not_namespaced() -> None:
    app = LaurenFactory.create(NamesModule)

    assert _names(app)["orders_list"] == "GET /orders"


def test_a_named_route_has_no_derived_name() -> None:
    app = LaurenFactory.create(NamesModule)

    assert "UserController.show" not in _names(app)


@module(controllers=[UserController], name="accounts")
class AccountsModule:
    pass


def test_module_name_prefixes_explicit_names_only() -> None:
    names = _names(LaurenFactory.create(AccountsModule))

    assert "accounts.users.show" in names
    assert "UserController.create" in names


# ---------------------------------------------------------------------------
# Name syntax (decoration time)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad", ["", "1abc", "users.", ".users", "a b", "users/show", 3])
def test_malformed_route_name_is_rejected_when_decorating(bad: object) -> None:
    with pytest.raises(DecoratorUsageError) as info:

        @get("/x", name=bad)  # type: ignore[arg-type]
        async def handler() -> None: ...

    assert info.value.detail["reason"] == "invalid_route_name"
    assert info.value.detail["option"] == "name"


@pytest.mark.parametrize("good", ["show", "users.show", "v2_users.show-all", "_private"])
def test_well_formed_route_name_is_accepted(good: str) -> None:
    @get("/x", name=good)
    async def handler() -> None: ...

    assert handler.__lauren_route__[-1].name == good


@pytest.mark.parametrize(
    ("decorate", "decorator"),
    [
        (lambda: controller("/x", name="bad name"), "controller"),
        (lambda: module(name="bad name"), "module"),
        (lambda: ws_controller("/ws", name="bad name"), "ws_controller"),
    ],
)
def test_malformed_class_level_name_is_rejected(decorate, decorator: str) -> None:
    with pytest.raises(DecoratorUsageError) as info:

        @decorate()
        class Target:
            pass

    assert info.value.detail == {
        "decorator": decorator,
        "target": info.value.detail["target"],
        "option": "name",
        "reason": "invalid_route_name",
    }


# ---------------------------------------------------------------------------
# Uniqueness
# ---------------------------------------------------------------------------


@controller("/a")
class _DupA:
    @get("/", name="same")
    async def a(self) -> dict:
        return {}


@controller("/b")
class _DupB:
    @get("/", name="same")
    async def b(self) -> dict:
        return {}


@module(controllers=[_DupA, _DupB])
class _DupModule:
    pass


def test_duplicate_explicit_names_fail_at_startup() -> None:
    with pytest.raises(RouterConflictError) as info:
        LaurenFactory.create(_DupModule)

    detail = info.value.detail
    assert detail["reason"] == "duplicate_route_name"
    assert detail["name"] == "same"
    assert len(detail["handlers"]) == 2


def _same_named_controller(prefix: str) -> type:
    @controller(prefix)
    class Controller:
        @get("/")
        async def index(self) -> dict:
            return {"prefix": prefix}

    return Controller


def test_colliding_derived_names_are_ambiguous_not_fatal() -> None:
    first, second = _same_named_controller("/one"), _same_named_controller("/two")

    @module(controllers=[first, second])
    class TwoModule:
        pass

    app = LaurenFactory.create(TwoModule)

    with pytest.raises(LookupError, match="ambiguous"):
        app.url_path_for("Controller.index")
    assert app.url_path_for(first.index) == "/one"


@controller("/explicit")
class _Explicit:
    @get("/", name="Shadowed.index")
    async def index(self) -> dict:
        return {}


@controller("/derived")
class Shadowed:
    @get("/")
    async def index(self) -> dict:
        return {}


@module(controllers=[_Explicit, Shadowed])
class _ShadowModule:
    pass


def test_explicit_name_shadows_an_equal_derived_name() -> None:
    app = LaurenFactory.create(_ShadowModule)

    assert app.url_path_for("Shadowed.index") == "/explicit"
    assert app.url_path_for(Shadowed.index) == "/derived"


# ---------------------------------------------------------------------------
# Multi-route handlers
# ---------------------------------------------------------------------------


@controller("/")
class _MultiController:
    @get("/ping")
    @get("/health")
    async def ping(self) -> dict:
        return {}

    @get("/a", name="alpha")
    @get("/b", name="beta")
    async def both(self) -> dict:
        return {}


@module(controllers=[_MultiController])
class _MultiModule:
    pass


def test_unnamed_multi_route_handler_gets_no_derived_name() -> None:
    app = LaurenFactory.create(_MultiModule)

    assert "_MultiController.ping" not in _names(app)


def test_handler_reference_to_a_multi_route_handler_needs_route() -> None:
    app = LaurenFactory.create(_MultiModule)

    with pytest.raises(LookupError, match="pass route="):
        app.url_path_for(_MultiController.both)


@pytest.mark.parametrize(("route", "path"), [("alpha", "/a"), ("beta", "/b")])
def test_route_kwarg_picks_one_route_of_a_handler(route: str, path: str) -> None:
    app = LaurenFactory.create(_MultiModule)

    assert app.url_path_for(_MultiController.both, route=route) == path


def test_route_kwarg_naming_no_route_of_the_handler_is_a_lookup_error() -> None:
    app = LaurenFactory.create(_MultiModule)

    with pytest.raises(LookupError, match="<unnamed>"):
        app.url_path_for(_MultiController.ping, route="nope")


# ---------------------------------------------------------------------------
# WebSocket namespace, Lauren apps, reflection
# ---------------------------------------------------------------------------


@ws_controller("/ws/chat/{room}", name="chat")
class ChatGateway:
    @on_connect
    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()


@controller("/chat")
class _ChatHttp:
    @get("/", name="chat")
    async def index(self) -> dict:
        return {}


@module(controllers=[ChatGateway, _ChatHttp])
class _ChatModule:
    pass


def test_http_and_websocket_names_are_separate_namespaces() -> None:
    app = LaurenFactory.create(_ChatModule)

    assert app.url_path_for("chat") == "/chat"
    assert app._urls._gateway_path("chat", {"room": "r1"}) == "/ws/chat/r1"


def test_reflect_readers_report_route_and_gateway_names() -> None:
    app = LaurenFactory.create(_ChatModule)

    with TestClient(app):
        routes = {r.full_path: r.name for r in get_all_routes(app)}
        gateways = {g.path_template: g.name for g in get_all_ws_gateways(app)}

    assert routes == {"/chat": "chat"}
    assert gateways == {"/ws/chat/{room}": "chat"}


def test_lauren_app_routes_are_named_after_their_function() -> None:
    app = Lauren(openapi_url=None, docs_url=None, redoc_url=None)

    @app.get("/items/{item_id}")
    async def read_item(item_id: int) -> dict:
        return {"id": item_id}

    assert app.url_path_for("read_item", item_id=3) == "/items/3"
    assert app.url_path_for(read_item, item_id=4) == "/items/4"


def test_docs_routes_are_not_named() -> None:
    app = LaurenFactory.create(NamesModule, openapi_url="/openapi.json", docs_url="/docs")

    assert not any("Docs" in name for name in _names(app))


def test_router_entries_expose_compiled_names() -> None:
    app = LaurenFactory.create(_ChatModule)

    http, _ = app.router.find("GET", "/chat/")
    websocket, _ = app._ws_router.find("WEBSOCKET", "/ws/chat/room")

    assert http.name == "chat"
    assert websocket.name == "chat"
