"""Router HTTP semantics end to end (F11): HEAD / OPTIONS, routing errors, converters, slashes, hosts."""

from __future__ import annotations

import pathlib
import uuid
from typing import Any

import pytest

from lauren import (
    BackgroundTasks,
    HostParam,
    Lauren,
    LaurenFactory,
    MethodNotAllowedError,
    PathConverter,
    Request,
    Response,
    RouteNotFoundError,
    RoutingConfig,
    any_method,
    controller,
    exception_handler,
    get,
    head,
    middleware,
    module,
    post,
    route,
    use_value,
)
from lauren._staticfiles import StaticFilesModule
from lauren.cache import CacheModule, cache_response
from lauren.exceptions import DecoratorUsageError, StartupError, UnresolvableParameterError
from lauren.pagination import OffsetPagination, Page, paginate_sequence
from lauren.sse import EventStream
from lauren.testing import TestClient
from lauren.websockets import WebSocket, on_connect, ws_controller

# ---------------------------------------------------------------------------
# Implicit HEAD
# ---------------------------------------------------------------------------


class Seen:
    def __init__(self) -> None:
        self.methods: list[str] = []
        self.background: list[str] = []
        self.calls = 0
        self.stream_started = False


@controller("/reports")
class Reports:
    def __init__(self, seen: Seen) -> None:
        self.seen = seen

    @get("/{id:int}")
    async def show(self, id: int, request: Request, tasks: BackgroundTasks) -> dict:
        self.seen.methods.append(request.method)
        tasks.add_task(self.seen.background.append, request.method)
        return {"id": id, "title": "quarterly"}

    @get("/accepted", status_code=202)
    async def accepted(self) -> dict:
        return {"queued": True}

    @get("/explicit")
    async def explicit_get(self) -> dict:
        return {"body": "from GET"}

    @head("/explicit")
    async def explicit_head(self) -> Response:
        return Response(b"head body must be stripped", headers=None).with_header("x-from", "head")

    @get("/events")
    async def events(self) -> EventStream:
        seen = self.seen

        async def gen() -> Any:
            seen.stream_started = True
            yield "tick"

        return EventStream(gen())

    @get("/cached")
    @cache_response(ttl=30)
    async def cached(self) -> dict:
        self.seen.calls += 1
        return {"n": self.seen.calls}

    @get("/page")
    async def page(self, paging: OffsetPagination) -> Page[int]:
        return paginate_sequence(list(range(50)), paging)


@module(controllers=[Reports], imports=[CacheModule.for_root()])
class HeadModule:
    pass


def _head_app(seen: Seen | None = None, **routing: Any) -> Any:
    return LaurenFactory.create(
        HeadModule,
        routing=RoutingConfig(**routing),
        global_providers=[use_value(provide=Seen, value=seen or Seen())],
    )


def test_head_runs_the_get_handler_and_sends_its_headers_only() -> None:
    app = _head_app()
    with TestClient(app) as client:
        get_response = client.get("/reports/7")
        head_response = client.head("/reports/7")

    assert head_response.status_code == 200
    assert head_response.content == b""
    assert head_response.header("content-length") == get_response.header("content-length")
    assert head_response.header("content-type") == "application/json"


def test_head_keeps_request_method_and_runs_background_tasks() -> None:
    seen = Seen()
    with TestClient(_head_app(seen)) as client:
        client.head("/reports/1")
        client.get("/reports/1")

    assert seen.methods == ["HEAD", "GET"]
    assert seen.background == ["HEAD", "GET"]


def test_head_keeps_the_declared_status() -> None:
    with TestClient(_head_app()) as client:
        response = client.head("/reports/accepted")

    assert response.status_code == 202
    assert response.content == b""


def test_explicit_head_handler_wins_and_its_body_is_stripped() -> None:
    with TestClient(_head_app()) as client:
        response = client.head("/reports/explicit")

    assert response.header("x-from") == "head"
    assert response.content == b""


def test_head_does_not_start_a_stream() -> None:
    seen = Seen()
    with TestClient(_head_app(seen)) as client:
        response = client.head("/reports/events")

    assert seen.stream_started is False
    assert response.status_code == 200
    assert response.content == b""
    assert "text/event-stream" in (response.header("content-type") or "")


def test_head_reads_the_get_cache_entry() -> None:
    with TestClient(_head_app()) as client:
        first = client.get("/reports/cached")
        head_response = client.head("/reports/cached")

    assert first.json() == {"n": 1}
    assert head_response.header("age") is not None
    assert head_response.content == b""


def test_head_on_a_paginated_route_keeps_link_headers() -> None:
    with TestClient(_head_app()) as client:
        response = client.head("/reports/page?limit=10")

    assert 'rel="next"' in (response.header("link") or "")
    assert response.content == b""


def test_head_error_envelopes_have_no_body() -> None:
    with TestClient(_head_app()) as client:
        response = client.head("/missing")

    assert response.status_code == 404
    assert int(response.header("content-length") or 0) > 0
    assert response.content == b""


def test_auto_head_off_restores_405() -> None:
    with TestClient(_head_app(auto_head=False)) as client:
        response = client.head("/reports/7")

    assert response.status_code == 405
    assert response.header("allow") == "GET"


def test_head_on_static_files(tmp_path: pathlib.Path) -> None:
    (tmp_path / "app.css").write_text("body { color: red }", encoding="utf-8")

    @module(imports=[StaticFilesModule.for_root("/static", directory=tmp_path)])
    class StaticApp:
        pass

    with TestClient(LaurenFactory.create(StaticApp)) as client:
        response = client.head("/static/app.css")

    assert response.status_code == 200
    assert response.header("content-type") == "text/css"
    assert response.header("content-length") == "19"
    assert response.header("etag")
    assert response.content == b""


def test_openapi_documents_only_explicit_head_operations() -> None:
    paths = _head_app().openapi()["paths"]

    assert "head" not in paths["/reports/{id}"]
    assert "head" in paths["/reports/explicit"]


# ---------------------------------------------------------------------------
# Implicit OPTIONS and CORS-style global middleware
# ---------------------------------------------------------------------------


@middleware()
class PreflightAnswerer:
    async def dispatch(self, request: Request, call_next: Any) -> Response:
        if request.method == "OPTIONS" and request.headers.get("access-control-request-method"):
            return Response(b"", status=204).with_header("access-control-allow-origin", "*")
        return await call_next(request)


@controller("/things")
class Things:
    @get("/{id}")
    async def show(self, id: str) -> dict:
        return {"id": id}

    @post("/{id}")
    async def update(self, id: str) -> dict:
        return {"id": id}


@module(controllers=[Things])
class ThingsModule:
    pass


def test_auto_options_answers_204_with_allow() -> None:
    app = LaurenFactory.create(ThingsModule, routing=RoutingConfig(auto_options=True))
    with TestClient(app) as client:
        response = client.options("/things/1")

    assert response.status_code == 204
    assert response.header("allow") == "GET, HEAD, OPTIONS, POST"


def test_auto_options_is_off_by_default() -> None:
    with TestClient(LaurenFactory.create(ThingsModule)) as client:
        response = client.options("/things/1")

    assert response.status_code == 405
    assert response.header("allow") == "GET, HEAD, POST"


def test_auto_options_leaves_unknown_paths_404() -> None:
    app = LaurenFactory.create(ThingsModule, routing=RoutingConfig(auto_options=True))
    with TestClient(app) as client:
        assert client.options("/nope").status_code == 404


def test_cors_preflight_is_answered_by_the_global_middleware_first() -> None:
    app = LaurenFactory.create(
        ThingsModule,
        global_middlewares=[PreflightAnswerer],
        routing=RoutingConfig(auto_options=True),
    )
    with TestClient(app) as client:
        preflight = client.options("/things/1", headers={"access-control-request-method": "POST"})
        plain = client.options("/things/1")

    assert preflight.header("access-control-allow-origin") == "*"
    assert preflight.header("allow") is None
    assert plain.status_code == 204
    assert plain.header("allow") == "GET, HEAD, OPTIONS, POST"


# ---------------------------------------------------------------------------
# Routing errors reach the global exception handlers
# ---------------------------------------------------------------------------


@exception_handler(RouteNotFoundError)
class BrandedNotFound:
    async def catch(self, exc: RouteNotFoundError, request: Request) -> Response:
        return Response.html(f"<h1>no page at {request.path}</h1>", status=404)


@exception_handler(MethodNotAllowedError)
class PlainMethodNotAllowed:
    async def catch(self, exc: MethodNotAllowedError, request: Request) -> Response:
        return Response.text("nope", status=405)


@exception_handler(RouteNotFoundError)
class BrokenHandler:
    async def catch(self, exc: RouteNotFoundError, request: Request) -> Response:
        raise RuntimeError("handler bug")


def _handled_app(*handlers: Any) -> Any:
    return LaurenFactory.create(ThingsModule, global_exception_handlers=list(handlers))


def test_global_handler_customizes_404() -> None:
    with TestClient(_handled_app(BrandedNotFound)) as client:
        response = client.get("/missing")

    assert response.status_code == 404
    assert response.text == "<h1>no page at /missing</h1>"


def test_global_handler_405_keeps_allow() -> None:
    with TestClient(_handled_app(PlainMethodNotAllowed)) as client:
        response = client.delete("/things/1")

    assert response.status_code == 405
    assert response.text == "nope"
    assert response.header("allow") == "GET, HEAD, POST"


def test_unclaimed_routing_errors_render_the_built_in_envelope() -> None:
    with TestClient(_handled_app(BrandedNotFound)) as client:
        response = client.delete("/things/1")

    assert response.json()["error"]["code"] == "method_not_allowed"


def test_failing_routing_error_handler_is_a_500() -> None:
    with TestClient(_handled_app(BrokenHandler), raise_server_exceptions=False) as client:
        response = client.get("/missing")

    assert response.status_code == 500


# ---------------------------------------------------------------------------
# Converters and per-route parameter names
# ---------------------------------------------------------------------------


class IsbnConverter(PathConverter):
    def matches(self, segment: str) -> bool:
        return len(segment) == 13 and segment.isdigit()

    def openapi_schema(self) -> dict[str, Any]:
        return {"type": "string", "pattern": "^[0-9]{13}$"}


@controller("/items")
class Items:
    @get("/{id:int}")
    async def show(self, id) -> dict:  # type: ignore[no-untyped-def]
        return {"id": id, "kind": type(id).__name__}

    @get("/special")
    async def special(self) -> dict:
        return {"special": True}

    @get("/{slug:slug}")
    async def by_slug(self, slug: str) -> dict:
        return {"slug": slug}

    @get("/by-key/{key:uuid}")
    async def by_key(self, key) -> dict:  # type: ignore[no-untyped-def]
        return {"kind": type(key).__name__}

    @get("/isbn/{code:isbn}")
    async def by_isbn(self, code: str) -> dict:
        return {"isbn": code}


@controller("/legacy")
class Legacy:
    @get("/{id}")
    async def show(self, id: int) -> dict:
        return {"id": id}


@controller("/users")
class TeamA:
    @get("/{id}")
    async def one(self, id: str) -> dict:
        return {"id": id}


@controller("/users")
class TeamB:
    @get("/{user_id}/posts")
    async def posts(self, user_id: str) -> dict:
        return {"user_id": user_id}


@module(controllers=[Items, Legacy, TeamA, TeamB])
class ConverterModule:
    pass


def _converter_client(**routing: Any) -> TestClient:
    routing.setdefault("converters", {"isbn": IsbnConverter()})
    return TestClient(LaurenFactory.create(ConverterModule, routing=RoutingConfig(**routing)))


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/items/42", {"id": 42, "kind": "int"}),
        ("/items/special", {"special": True}),
        ("/items/blue-mug", {"slug": "blue-mug"}),
        (f"/items/by-key/{uuid.UUID(int=7)}", {"kind": "UUID"}),
        ("/items/isbn/9780306406157", {"isbn": "9780306406157"}),
        ("/users/7", {"id": "7"}),
        ("/users/7/posts", {"user_id": "7"}),
    ],
)
def test_converter_routes(path: str, body: dict[str, Any]) -> None:
    with _converter_client() as client:
        response = client.get(path)

    assert response.status_code == 200
    assert response.json() == body


def test_converter_mismatch_is_404_not_422() -> None:
    with _converter_client() as client:
        typed = client.get("/items/Not_A_Slug")
        untyped = client.get("/legacy/abc")

    assert typed.status_code == 404
    assert untyped.status_code == 422


def test_infer_converters_turns_bad_ids_into_404() -> None:
    with _converter_client(infer_converters=True) as client:
        assert client.get("/legacy/abc").status_code == 404
        assert client.get("/legacy/-5").json() == {"id": -5}


def test_converter_routes_expose_their_template() -> None:
    app = LaurenFactory.create(ConverterModule, routing=RoutingConfig(converters={"isbn": IsbnConverter()}))
    with TestClient(app) as client:
        templates = {e.path_template for e in app.routes()}

    assert "/items/{id:int}" in templates
    assert client is not None


def test_openapi_uses_converter_schemas_and_plain_templates() -> None:
    app = LaurenFactory.create(ConverterModule, routing=RoutingConfig(converters={"isbn": IsbnConverter()}))

    paths = app.openapi()["paths"]

    (id_param,) = paths["/items/{id}"]["get"]["parameters"]
    assert id_param["schema"] == {"type": "integer", "minimum": 0}
    (slug_param,) = paths["/items/{slug}"]["get"]["parameters"]
    assert slug_param["schema"]["pattern"].startswith("^")
    (isbn_param,) = paths["/items/isbn/{code}"]["get"]["parameters"]
    assert isbn_param["schema"] == {"type": "string", "pattern": "^[0-9]{13}$"}
    (legacy_param,) = paths["/legacy/{id}"]["get"]["parameters"]
    assert legacy_param["schema"]["type"] == "integer"


def test_incompatible_converter_and_annotation_fail_startup() -> None:
    @controller("/bad")
    class Bad:
        @get("/{id:uuid}")
        async def show(self, id: int) -> dict:
            return {}

    @module(controllers=[Bad])
    class BadModule:
        pass

    with pytest.raises(UnresolvableParameterError) as exc_info:
        LaurenFactory.create(BadModule)

    assert exc_info.value.detail["reason"] == "converter_mismatch"
    assert exc_info.value.detail["param"] == "id"


def test_unknown_converter_fails_startup() -> None:
    @controller("/bad")
    class Unknown:
        @get("/{code:isbn}")
        async def show(self, code: str) -> dict:
            return {}

    @module(controllers=[Unknown])
    class UnknownModule:
        pass

    with pytest.raises(DecoratorUsageError) as exc_info:
        LaurenFactory.create(UnknownModule)

    assert exc_info.value.detail["reason"] == "unknown_converter"


def test_routing_must_be_a_routing_config() -> None:
    with pytest.raises(StartupError) as exc_info:
        LaurenFactory.create(ThingsModule, routing={"auto_head": False})  # type: ignore[arg-type]

    assert exc_info.value.detail["reason"] == "invalid_routing_config"


# ---------------------------------------------------------------------------
# Trailing slashes and percent-decoding
# ---------------------------------------------------------------------------


@controller("/docs")
class Docs:
    @get("/")
    async def index(self) -> dict:
        return {"index": True}

    @get("/{page}")
    async def page(self, page: str) -> dict:
        return {"page": page}

    @get("/guides/")
    async def guides(self) -> dict:
        return {"guides": True}


@controller("/handbook")
class Handbook:
    @get("/toc/")
    async def toc(self) -> dict:
        return {"toc": True}


@module(controllers=[Docs, Handbook])
class DocsModule:
    pass


@pytest.mark.parametrize(
    ("path", "location"),
    [
        ("/docs/intro/?lang=en", "/docs/intro?lang=en"),
        ("/docs/", "/docs"),
        ("/handbook/toc?x=1", "/handbook/toc/?x=1"),
    ],
)
def test_redirect_policy_sends_308_to_the_declared_spelling(path: str, location: str) -> None:
    app = LaurenFactory.create(DocsModule, routing=RoutingConfig(trailing_slash="redirect"))
    with TestClient(app) as client:
        response = client.post(path, follow_redirects=False)

    assert response.status_code == 308
    assert response.header("location") == location


def test_redirect_location_includes_the_root_path() -> None:
    app = LaurenFactory.create(DocsModule, routing=RoutingConfig(trailing_slash="redirect"), root_path="/api")
    with TestClient(app) as client:
        response = client.get("/api/docs/intro/", follow_redirects=False)

    assert response.header("location") == "/api/docs/intro"


def test_redirect_location_includes_a_mount_prefix() -> None:
    inner = LaurenFactory.create(DocsModule, routing=RoutingConfig(trailing_slash="redirect"))
    outer = LaurenFactory.create(ThingsModule, mounts={"/v1": inner})
    with TestClient(outer) as client:
        response = client.get("/v1/docs/caf%C3%A9/", follow_redirects=False)

    assert response.status_code == 308
    assert response.header("location") == "/v1/docs/caf%C3%A9"


def test_redirect_is_followed_to_the_canonical_route() -> None:
    app = LaurenFactory.create(DocsModule, routing=RoutingConfig(trailing_slash="redirect"))
    with TestClient(app) as client:
        response = client.get("/docs/intro/", follow_redirects=True)

    assert response.json() == {"page": "intro"}


def test_strict_policy_404s_the_other_spelling() -> None:
    app = LaurenFactory.create(DocsModule, routing=RoutingConfig(trailing_slash="strict"))
    with TestClient(app) as client:
        assert client.get("/docs/guides/").status_code == 200
        assert client.get("/docs/guides").json() == {"page": "guides"}
        assert client.get("/docs/intro/").status_code == 404


def test_merge_slashes() -> None:
    app = LaurenFactory.create(DocsModule, routing=RoutingConfig(merge_slashes=True))
    with TestClient(app) as client:
        assert client.get("/docs///intro").json() == {"page": "intro"}


def test_testclient_percent_decodes_the_path_like_a_server() -> None:
    with TestClient(LaurenFactory.create(DocsModule)) as client:
        decoded = client.get("/docs/caf%C3%A9")
        split = client.get("/docs/a%2Fb")

    assert decoded.json() == {"page": "café"}
    assert split.status_code == 404


def test_raw_path_mode_keeps_encoded_slashes_inside_a_segment() -> None:
    app = LaurenFactory.create(DocsModule, routing=RoutingConfig(match_raw_path=True))
    with TestClient(app) as client:
        response = client.get("/docs/a%2Fb")

    assert response.json() == {"page": "a/b"}


def test_raw_path_mode_answers_400_for_malformed_encoding() -> None:
    app = LaurenFactory.create(DocsModule, routing=RoutingConfig(match_raw_path=True))
    with TestClient(app) as client:
        response = client.get("/docs/%ZZ")

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "bad_request"


def test_raw_path_mode_under_a_root_path() -> None:
    app = LaurenFactory.create(DocsModule, routing=RoutingConfig(match_raw_path=True), root_path="/api")
    with TestClient(app) as client:
        response = client.get("/api/docs/a%2Fb")

    assert response.json() == {"page": "a/b"}


def test_raw_path_mode_without_raw_path_in_scope() -> None:
    app = LaurenFactory.create(DocsModule, routing=RoutingConfig(match_raw_path=True))
    request = Request(method="GET", path="/docs/café")

    import asyncio

    response = asyncio.run(app.handle(request))

    assert response.status == 200


# ---------------------------------------------------------------------------
# Method-agnostic routes
# ---------------------------------------------------------------------------


@controller("/hooks")
class Hooks:
    @route("/github", methods=["POST", "PUT"])
    async def github(self, request: Request) -> dict:
        return {"method": request.method}

    @any_method("/proxy/{*rest}")
    async def proxy(self, rest: str, request: Request) -> dict:
        return {"method": request.method, "rest": rest}

    @get("/proxy/status")
    async def status(self) -> dict:
        return {"status": "ok"}

    @any_method("/sink")
    async def sink(self, request: Request) -> dict:
        return {"sink": request.method}

    @get("/sink")
    async def sink_get(self) -> dict:
        return {"sink": "explicit GET"}


@module(controllers=[Hooks])
class HooksModule:
    pass


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("POST", "/hooks/github", {"method": "POST"}),
        ("PUT", "/hooks/github", {"method": "PUT"}),
        ("DELETE", "/hooks/proxy/a/b", {"method": "DELETE", "rest": "a/b"}),
        ("GET", "/hooks/proxy/status", {"status": "ok"}),
        ("GET", "/hooks/sink", {"sink": "explicit GET"}),
        ("PATCH", "/hooks/sink", {"sink": "PATCH"}),
    ],
)
def test_method_agnostic_routes(method: str, path: str, body: dict[str, Any]) -> None:
    with TestClient(LaurenFactory.create(HooksModule)) as client:
        response = client.request(method, path)

    assert response.json() == body


def test_route_with_a_method_list_answers_405_for_others() -> None:
    with TestClient(LaurenFactory.create(HooksModule)) as client:
        response = client.get("/hooks/github")

    assert response.status_code == 405
    assert response.header("allow") == "POST, PUT"


def test_any_method_routes_are_left_out_of_openapi() -> None:
    paths = LaurenFactory.create(HooksModule).openapi()["paths"]

    assert set(paths["/hooks/github"]) == {"post", "put"}
    assert "/hooks/proxy/{rest}" not in paths
    assert set(paths["/hooks/sink"]) == {"get"}


def test_lauren_route_helpers_and_routing_kwarg() -> None:
    app = Lauren(openapi_url=None, docs_url=None, redoc_url=None, routing=RoutingConfig(auto_options=True))

    @app.route("/multi", methods=["post", "PUT"])
    async def multi(request: Request) -> dict:
        return {"method": request.method}

    @app.any_method("/anything")
    async def anything(request: Request) -> dict:
        return {"method": request.method}

    with TestClient(app) as client:
        assert client.put("/multi").json() == {"method": "PUT"}
        assert client.options("/multi").header("allow") == "OPTIONS, POST, PUT"
        assert client.delete("/anything").json() == {"method": "DELETE"}


# ---------------------------------------------------------------------------
# Host routing
# ---------------------------------------------------------------------------


@controller("/", host="{tenant}.example.com")
class TenantSite:
    @get("/")
    async def home(self, tenant: HostParam[str]) -> dict:
        return {"tenant": tenant}


@controller("/", host="admin.example.com")
class AdminSite:
    @get("/")
    async def home(self) -> dict:
        return {"admin": True}


@controller("/shards")
class Shards:
    @get("/")
    async def home(self, shard: HostParam[int]) -> dict:
        return {"shard": shard}


@module(controllers=[Shards], host="{shard:int}.db.example.com")
class ShardModule:
    pass


@ws_controller("/live", host="{tenant}.example.com")
class TenantLive:
    @on_connect
    async def connect(self, ws: WebSocket, tenant: HostParam[str]) -> None:
        await ws.accept()
        await ws.send_json({"tenant": tenant})


@module(controllers=[TenantSite, AdminSite, TenantLive], imports=[ShardModule])
class HostModule:
    pass


def _host_client() -> TestClient:
    return TestClient(LaurenFactory.create(HostModule))


@pytest.mark.parametrize(
    ("host", "body"),
    [
        ("acme.example.com", {"tenant": "acme"}),
        ("ACME.Example.com:8443", {"tenant": "acme"}),
        ("admin.example.com", {"admin": True}),
    ],
)
def test_host_routing(host: str, body: dict[str, Any]) -> None:
    with _host_client() as client:
        response = client.get("/", headers={"host": host})

    assert response.json() == body


def test_host_miss_is_404() -> None:
    with _host_client() as client:
        assert client.get("/", headers={"host": "other.org"}).status_code == 404


def test_module_host_and_typed_host_params() -> None:
    with _host_client() as client:
        ok = client.get("/shards/", headers={"host": "7.db.example.com"})
        miss = client.get("/shards/", headers={"host": "x.db.example.com"})

    assert ok.json() == {"shard": 7}
    assert miss.status_code == 404


def test_websocket_gateway_with_host() -> None:
    with _host_client() as client:
        with client.websocket_connect("/live", headers={"host": "acme.example.com"}) as ws:
            assert ws.receive_json() == {"tenant": "acme"}


def test_openapi_names_the_host_of_host_routed_operations() -> None:
    paths = LaurenFactory.create(HostModule).openapi()["paths"]

    servers = paths["/shards"]["get"]["servers"]
    assert servers == [{"url": "//{shard}.db.example.com", "variables": {"shard": {"default": "shard"}}}]


def test_host_param_must_name_a_host_label() -> None:
    @controller("/nohost")
    class NoHost:
        @get("/")
        async def home(self, tenant: HostParam[str]) -> dict:
            return {}

    @module(controllers=[NoHost])
    class NoHostModule:
        pass

    with pytest.raises(UnresolvableParameterError) as exc_info:
        LaurenFactory.create(NoHostModule)

    assert exc_info.value.detail["reason"] == "unknown_host_param"


def test_authority_header_routes_when_host_is_absent() -> None:
    # HTTP/2 uses :authority; the explicit empty Host suppresses the TestClient default.
    with _host_client() as client:
        response = client.get("/", headers={"Host": "", ":authority": "acme.example.com"})

    assert response.json() == {"tenant": "acme"}


def test_openapi_lists_both_hosts_for_a_shared_path_and_method() -> None:
    servers = LaurenFactory.create(HostModule).openapi()["paths"]["/"]["get"]["servers"]

    assert servers == [
        {"url": "//{tenant}.example.com", "variables": {"tenant": {"default": "tenant"}}},
        {"url": "//admin.example.com"},
    ]


def test_custom_converter_to_python_supplies_unannotated_handler_value() -> None:
    class Base36(PathConverter):
        python_type = int

        def matches(self, segment: str) -> bool:
            return len(segment) == 2 and segment.isascii() and segment.isalnum()

        def to_python(self, segment: str) -> int:
            return int(segment, 36)

        def to_url(self, value: int) -> str:
            return format(value, "x").zfill(2)

    @controller("/codes")
    class Codes:
        @get("/{code:base36}")
        async def show(self, code) -> dict:  # type: ignore[no-untyped-def]
            return {"value": code, "type": type(code).__name__}

    @module(controllers=[Codes])
    class CodesModule:
        pass

    config = RoutingConfig(converters={"base36": Base36()})
    with TestClient(LaurenFactory.create(CodesModule, routing=config)) as client:
        response = client.get("/codes/zz")

    assert response.json() == {"value": 1295, "type": "int"}
