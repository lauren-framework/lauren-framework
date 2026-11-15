"""Named URLs use the same converters and host identities as dispatch."""

from __future__ import annotations

import pytest

from lauren import (
    LaurenFactory,
    PathConverter,
    Request,
    RoutingConfig,
    WebSocket,
    controller,
    get,
    module,
    on_connect,
    ws_controller,
)
from lauren.reflect import describe_app, get_all_routes
from lauren.testing import TestClient


@controller("/typed")
class Typed:
    @get("/{id:int(min=1)}", name="typed.item")
    async def show(self, id: int) -> dict:
        return {"id": id}


@controller("/strict")
class Strict:
    @get("/end/", name="strict.end")
    async def show(self) -> dict:
        return {"ok": True}


class Hex(PathConverter):
    name = "hex"
    python_type = int

    def matches(self, segment: str) -> bool:
        return (
            segment.startswith("x")
            and bool(segment[1:])
            and all(c in "0123456789abcdef" for c in segment[1:])
        )

    def to_python(self, segment: str) -> int:
        return int(segment[1:], 16)

    def to_url(self, value: int) -> str:
        return f"x{value:x}"


@controller("/custom")
class Custom:
    @get("/{id:hex}", name="custom.item")
    async def show(self, id) -> dict:
        return {"id": id}


@controller("/same", host="admin.example.test")
class Admin:
    @get("/", name="admin.home")
    async def home(self, request: Request) -> dict:
        return {"public": request.url_for("public.home")}


@controller("/same", host="public.example.test")
class Public:
    @get("/", name="public.home")
    async def home(self) -> dict:
        return {"ok": True}


@ws_controller("/ws/{room}", host="{tenant}.example.test", name="tenant.socket")
class Gateway:
    @on_connect
    async def join(self) -> None:
        pass


def _app(*controllers: type, **kwargs):
    @module(controllers=list(controllers))
    class Root:
        pass

    return LaurenFactory.create(Root, **kwargs)


def test_named_typed_path_rejects_values_the_router_would_reject() -> None:
    app = _app(Typed)
    assert app.url_path_for("typed.item", id=7) == "/typed/7"
    with pytest.raises(ValueError):
        app.url_path_for("typed.item", id=0)


def test_named_custom_path_uses_the_converter_formatter() -> None:
    app = _app(Custom, routing=RoutingConfig(converters={"hex": Hex()}))
    path = app.url_path_for("custom.item", id=17)
    with TestClient(app) as client:
        response = client.get(path)
    assert path == "/custom/x11"
    assert response.status_code == 200 and response.json() == {"id": 17}


def test_named_path_preserves_a_strict_trailing_slash() -> None:
    app = _app(Strict, routing=RoutingConfig(trailing_slash="strict"))
    path = app.url_path_for("strict.end")
    with TestClient(app) as client:
        response = client.get(path)
    assert path == "/strict/end/"
    assert response.status_code == 200


def test_absolute_named_urls_use_the_target_route_host() -> None:
    app = _app(Admin, Public, public_base_url="https://public.example.test")
    assert app.urls.url_for("admin.home") == "https://admin.example.test/same"
    with TestClient(app, base_url="https://admin.example.test") as client:
        response = client.get("/same")
    assert response.json()["public"] == "https://public.example.test/same"


def test_host_routed_websocket_urls_use_host_and_path_parameters() -> None:
    app = _app(Gateway, public_base_url="https://public.example.test")
    assert app.urls.ws_url_for("tenant.socket", tenant="acme", room="a") == "wss://acme.example.test/ws/a"
    with pytest.raises(ValueError):
        app.urls.ws_url_for("tenant.socket", room="a")


def test_named_host_routes_remain_distinct_in_reflection_and_manifest() -> None:
    app = _app(Admin, Public)
    names = {route.host: route.name for route in describe_app(app).routes}
    assert names == {"admin.example.test": "admin.home", "public.example.test": "public.home"}
    with TestClient(app):
        assert {route.host: route.name for route in get_all_routes(app)} == names


def test_manifest_uses_compiled_names_including_namespaces_and_derived_names() -> None:
    @controller("/names", name="users")
    class Names:
        @get("/explicit", name="show")
        async def explicit(self) -> dict:
            return {}

        @get("/derived")
        async def derived(self) -> dict:
            return {}

    @module(controllers=[Names], name="accounts")
    class Root:
        pass

    routes = {route.path: route.name for route in describe_app(LaurenFactory.create(Root)).routes}
    assert routes["/names/explicit"] == "accounts.users.show"
    assert routes["/names/derived"] == "Names.derived"


def test_host_bound_signed_url_uses_the_target_origin() -> None:
    from lauren import UrlSigningConfig, ValidSignature, use_guards

    @controller("/secured", host="admin.example.test")
    class Secured:
        @get("/", name="secured")
        @use_guards(ValidSignature)
        async def show(self) -> dict:
            return {"ok": True}

    app = _app(
        Secured,
        public_base_url="https://public.example.test",
        url_signing=UrlSigningConfig(secret="test-secret", bind_host=True, clock=lambda: 1000),
    )
    link = app.urls.signed_url_for("secured")
    with TestClient(app) as client:
        response = client.get(link)
    assert link.startswith("https://admin.example.test/secured?")
    assert response.status_code == 200


@pytest.mark.parametrize("trusted,expected", [(True, 200), (False, 404)])
def test_host_routing_uses_forwarded_host_only_from_trusted_peers(trusted: bool, expected: int) -> None:
    from lauren import ProxyConfig

    app = _app(Admin, Public, proxy=ProxyConfig(trusted=["10.0.0.0/8"]))
    peer = "10.0.0.5" if trusted else "198.51.100.1"
    with TestClient(app, client=(peer, 4000)) as client:
        response = client.get(
            "/same",
            headers={"host": "backend.internal", "x-forwarded-host": "admin.example.test"},
        )
    assert response.status_code == expected


def test_websocket_host_routing_uses_the_same_trusted_forwarding_as_urls() -> None:
    from lauren import ProxyConfig

    @ws_controller("/proxied-socket", host="admin.example.test", name="proxied.socket")
    class Socket:
        @on_connect
        async def join(self, ws: WebSocket) -> None:
            await ws.accept()
            await ws.send_json({"link": ws.url_for("admin.home")})

    app = _app(Admin, Socket, proxy=ProxyConfig(trusted=["10.0.0.0/8"]))
    with TestClient(app, client=("10.0.0.5", 4000)) as client:
        with client.websocket_connect(
            "/proxied-socket",
            headers={
                "host": "backend.internal",
                "x-forwarded-host": "admin.example.test",
                "x-forwarded-proto": "https",
            },
        ) as session:
            body = session.receive_json()
    assert body["link"] == "https://admin.example.test/same"


def test_signed_any_method_url_is_a_get_link() -> None:
    from lauren import UrlSigningConfig, ValidSignature, any_method, use_guards

    @controller("/any-signed")
    class AnySigned:
        @any_method("/", name="any-signed")
        @use_guards(ValidSignature)
        async def show(self) -> dict:
            return {"ok": True}

    app = _app(
        AnySigned, url_signing=UrlSigningConfig(secret="test-secret", bind_method=True, clock=lambda: 1000)
    )
    with TestClient(app) as client:
        response = client.get(app.urls.signed_url_for("any-signed"))
    assert response.status_code == 200


def test_signed_query_keeps_duplicate_value_order_bound() -> None:
    from lauren import UrlSigningConfig, ValidSignature, use_guards

    @controller("/signed-query")
    class Query:
        @get("/", name="signed.query")
        @use_guards(ValidSignature)
        async def show(self, account: str) -> dict:
            return {"account": account}

    app = _app(Query, url_signing=UrlSigningConfig(secret="test-secret", clock=lambda: 1000))
    link = app.urls.signed_url_for("signed.query", query=[("account", "first"), ("account", "second")])
    swapped = link.replace("account=first&account=second", "account=second&account=first")
    with TestClient(app) as client:
        original = client.get(link)
        tampered = client.get(swapped)
    assert original.status_code == 200 and original.json() == {"account": "first"}
    assert tampered.status_code == 403


@pytest.mark.parametrize("root_path", ["", "/prefix"])
def test_raw_signed_path_binds_encoded_segment_boundaries(root_path: str) -> None:
    from lauren import UrlSigningConfig, ValidSignature, use_guards

    @controller("/raw-signed")
    class Raw:
        @get("/{value}", name="raw.single")
        @use_guards(ValidSignature)
        async def single(self, value: str) -> dict:
            return {"route": "single", "value": value}

        @get("/{first}/{second}", name="raw.pair")
        @use_guards(ValidSignature)
        async def pair(self, first: str, second: str) -> dict:
            return {"route": "pair"}

    app = _app(
        Raw,
        root_path=root_path,
        routing=RoutingConfig(match_raw_path=True),
        url_signing=UrlSigningConfig(secret="test-secret", clock=lambda: 1000),
    )
    link = app.urls.signed_url_for("raw.single", value="a/b")
    with TestClient(app) as client:
        original = client.get(link)
        changed = client.get(link.replace("%2F", "/"))
    assert original.status_code == 200 and original.json() == {"route": "single", "value": "a/b"}
    assert changed.status_code == 403


def test_signed_get_url_also_allows_automatic_head() -> None:
    from lauren import UrlSigningConfig, ValidSignature, use_guards

    @controller("/signed-head")
    class Head:
        @get("/", name="head")
        @use_guards(ValidSignature)
        async def show(self) -> dict:
            return {"ok": True}

    app = _app(Head, url_signing=UrlSigningConfig(secret="test-secret", bind_method=True, clock=lambda: 1000))
    with TestClient(app) as client:
        response = client.head(app.urls.signed_url_for("head"))
    assert response.status_code == 200
    assert response.body == b""


async def test_raw_signed_websocket_path_keeps_segment_boundaries() -> None:
    from lauren import UrlSigningConfig, ValidSignature, use_guards
    from lauren.testing import WsTestClient

    @ws_controller("/raw-signed-ws/{value}", name="raw.ws.single")
    @use_guards(ValidSignature)
    class Single:
        @on_connect
        async def join(self, ws: WebSocket) -> None:
            await ws.accept()
            await ws.send_json({"route": "single"})

    @ws_controller("/raw-signed-ws/{first}/{second}", name="raw.ws.pair")
    @use_guards(ValidSignature)
    class Pair:
        @on_connect
        async def join(self, ws: WebSocket) -> None:
            await ws.accept()
            await ws.send_json({"route": "pair"})

    app = _app(
        Single,
        Pair,
        routing=RoutingConfig(match_raw_path=True),
        public_base_url="https://example.test",
        url_signing=UrlSigningConfig(secret="test-secret", clock=lambda: 1000),
    )
    link = app.urls.signed_ws_url_for("raw.ws.single", value="a/b")
    await app.startup()
    try:
        async with WsTestClient(app).connect(link.path, query_string=link.query) as original:
            body = await original.receive_json()
        async with WsTestClient(app).connect(
            link.path.replace("%2F", "/"), query_string=link.query
        ) as changed:
            pass
    finally:
        await app.shutdown()
    assert body == {"route": "single"}
    assert (changed.close_code, changed.close_reason) == (4403, "signature_invalid")


def test_host_bound_websocket_url_uses_its_compiled_host() -> None:
    from lauren import UrlSigningConfig, ValidSignature, use_guards

    @ws_controller("/host-bound/{room}", host="{tenant}.example.test", name="bound.socket")
    @use_guards(ValidSignature)
    class Bound:
        @on_connect
        async def join(self, ws: WebSocket) -> None:
            await ws.accept()
            await ws.send_json({"ok": True})

    app = _app(
        Bound,
        public_base_url="https://public.example.test",
        url_signing=UrlSigningConfig(secret="test-secret", bind_host=True, clock=lambda: 1000),
    )
    link = app.urls.signed_ws_url_for("bound.socket", tenant="acme", room="a")
    with TestClient(app) as client:
        with client.websocket_connect(link) as session:
            body = session.receive_json()
    assert link.startswith("wss://acme.example.test/host-bound/a?")
    assert body == {"ok": True}
