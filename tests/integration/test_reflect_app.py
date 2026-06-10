"""Integration tests for lauren.reflect app-level readers.

These tests build full LaurenFactory apps and verify get_all_routes,
get_all_ws_gateways, and get_route_metadata against the compiled dispatch table.
"""

from __future__ import annotations

import pytest

from lauren import (
    LaurenFactory,
    Scope,
    WebSocket,
    controller,
    get,
    injectable,
    module,
    on_connect,
    on_message,
    post,
    put,
    use_guards,
    ws_controller,
)
from lauren.reflect import (
    ReflectedRoute,
    ReflectedWsGateway,
    get_all_routes,
    get_all_ws_gateways,
    get_route_metadata,
)
from lauren.testing import TestClient


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@injectable(scope=Scope.SINGLETON)
class _AnyGuard:
    async def can_activate(self, ctx):
        return True


@controller("/users")
class _UserController:
    @get("/{id}")
    async def get_user(self):
        return {}

    @post("/")
    async def create_user(self):
        return {}


@controller("/items")
class _ItemController:
    @get("/")
    async def list_items(self):
        return []

    @put("/{id}")
    async def update_item(self):
        return {}


@module(controllers=[_UserController, _ItemController])
class _HttpAppModule:
    pass


@ws_controller("/ws/chat")
class _ChatGateway:
    @on_connect
    async def connected(self, ws: WebSocket):
        await ws.accept()

    @on_message("ping")
    async def ping(self, ws: WebSocket):
        await ws.send_json({"event": "pong"})


@module(controllers=[_ChatGateway])
class _WsAppModule:
    pass


@use_guards(_AnyGuard)
@ws_controller("/ws/secure")
class _SecureGateway:
    @on_connect
    async def connected(self, ws: WebSocket):
        await ws.accept()


@module(controllers=[_SecureGateway])
class _SecureWsModule:
    pass


# ---------------------------------------------------------------------------
# HTTP route tests
# ---------------------------------------------------------------------------


class TestGetAllRoutesPreStartup:
    def test_returns_empty_before_startup(self):
        app = LaurenFactory.create(_HttpAppModule)
        # Do NOT call TestClient — that triggers startup
        result = get_all_routes(app)
        assert result == ()


class TestGetAllRoutesAfterStartup:
    @pytest.fixture
    def app(self):
        _app = LaurenFactory.create(_HttpAppModule)
        TestClient(_app)  # triggers startup
        return _app

    def test_returns_all_routes(self, app):
        routes = get_all_routes(app)
        assert len(routes) == 4

    def test_routes_are_reflected_route_instances(self, app):
        for r in get_all_routes(app):
            assert isinstance(r, ReflectedRoute)

    def test_methods_are_uppercase(self, app):
        methods = {r.method for r in get_all_routes(app)}
        assert methods == {"GET", "POST", "PUT"}

    def test_full_paths_include_prefix(self, app):
        full_paths = {r.full_path for r in get_all_routes(app)}
        assert "/users/{id}" in full_paths
        assert "/users" in full_paths
        assert "/items" in full_paths
        assert "/items/{id}" in full_paths

    def test_handler_is_callable(self, app):
        for r in get_all_routes(app):
            assert callable(r.handler)


# ---------------------------------------------------------------------------
# get_route_metadata tests
# ---------------------------------------------------------------------------


class TestGetRouteMetadata:
    @pytest.fixture
    def app(self):
        _app = LaurenFactory.create(_HttpAppModule)
        TestClient(_app)
        return _app

    def test_returns_none_before_startup(self):
        _app = LaurenFactory.create(_HttpAppModule)
        assert get_route_metadata(_app, "GET", "/users/{id}") is None

    def test_returns_route_for_known_path(self, app):
        route = get_route_metadata(app, "GET", "/users/{id}")
        assert route is not None
        assert route.method == "GET"
        assert "{id}" in route.full_path

    def test_returns_none_for_unknown_path(self, app):
        assert get_route_metadata(app, "GET", "/nonexistent") is None

    def test_method_is_case_insensitive(self, app):
        route = get_route_metadata(app, "get", "/users/{id}")
        assert route is not None

    def test_returns_none_for_wrong_method(self, app):
        assert get_route_metadata(app, "DELETE", "/users/{id}") is None

    def test_handler_callable(self, app):
        route = get_route_metadata(app, "POST", "/users")
        assert route is not None
        assert callable(route.handler)


# ---------------------------------------------------------------------------
# WebSocket gateway tests
# ---------------------------------------------------------------------------


class TestGetAllWsGatewaysPreStartup:
    def test_returns_empty_before_startup(self):
        app = LaurenFactory.create(_WsAppModule)
        assert get_all_ws_gateways(app) == ()


class TestGetAllWsGateways:
    @pytest.fixture
    def app(self):
        _app = LaurenFactory.create(_WsAppModule)
        TestClient(_app)
        return _app

    def test_returns_gateways(self, app):
        gateways = get_all_ws_gateways(app)
        assert len(gateways) == 1

    def test_gateway_is_reflected_ws_gateway(self, app):
        gw = get_all_ws_gateways(app)[0]
        assert isinstance(gw, ReflectedWsGateway)

    def test_path_template(self, app):
        gw = get_all_ws_gateways(app)[0]
        assert gw.path_template == "/ws/chat"

    def test_cls_is_gateway_class(self, app):
        gw = get_all_ws_gateways(app)[0]
        assert gw.cls is _ChatGateway

    def test_messages_include_ping(self, app):
        gw = get_all_ws_gateways(app)[0]
        events = {m.event for m in gw.messages}
        assert "ping" in events

    def test_message_handler_is_callable(self, app):
        gw = get_all_ws_gateways(app)[0]
        for msg in gw.messages:
            if msg.handler is not None:
                assert callable(msg.handler)


class TestGetAllWsGatewaysWithGuards:
    @pytest.fixture
    def app(self):
        _app = LaurenFactory.create(_SecureWsModule)
        TestClient(_app)
        return _app

    def test_guards_reflected(self, app):
        gw = get_all_ws_gateways(app)[0]
        assert _AnyGuard in gw.guards

    def test_path_template(self, app):
        gw = get_all_ws_gateways(app)[0]
        assert gw.path_template == "/ws/secure"


# ---------------------------------------------------------------------------
# Combined app — HTTP + WS in one module
# ---------------------------------------------------------------------------


@controller("/api")
class _ApiCtrl:
    @get("/ping")
    async def ping(self):
        return {"pong": True}


@ws_controller("/ws/events")
class _EventGateway:
    @on_connect
    async def on_open(self, ws: WebSocket):
        await ws.accept()


@module(controllers=[_ApiCtrl, _EventGateway])
class _CombinedModule:
    pass


class TestCombinedApp:
    @pytest.fixture
    def app(self):
        _app = LaurenFactory.create(_CombinedModule)
        TestClient(_app)
        return _app

    def test_get_all_routes_finds_http_only(self, app):
        routes = get_all_routes(app)
        assert any(r.full_path == "/api/ping" for r in routes)

    def test_get_all_ws_gateways_finds_ws_only(self, app):
        gateways = get_all_ws_gateways(app)
        assert any(gw.path_template == "/ws/events" for gw in gateways)

    def test_no_overlap(self, app):
        route_paths = {r.full_path for r in get_all_routes(app)}
        gw_paths = {gw.path_template for gw in get_all_ws_gateways(app)}
        assert route_paths.isdisjoint(gw_paths)
