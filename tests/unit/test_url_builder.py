"""URL templates, the ``URL`` value type, authority parsing and the ``UrlBuilder``."""

from __future__ import annotations

import enum
import functools

import pytest

from lauren import LaurenFactory, controller, get, module
from lauren._urls._registry import RouteSpec, RouteTable, split_base_url
from lauren._urls._template import UrlTemplate
from lauren._urls._url import compose_origin, join_root, parse_authority
from lauren.types import Request
from lauren.urls import URL, UrlBuilder
from lauren.websockets import WebSocket, on_connect, ws_controller


def _expand(template: str, **params: object) -> str:
    return UrlTemplate(template).expand(params, label="r")


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("template", "params", "expected"),
    [
        ("/", {}, "/"),
        ("/users", {}, "/users"),
        ("/users/{id}", {"id": 7}, "/users/7"),
        ("/users/{id}/posts/{post}", {"id": 1, "post": "a"}, "/users/1/posts/a"),
        ("/{id}", {"id": "x"}, "/x"),
    ],
)
def test_template_expands_parameters(template: str, params: dict, expected: str) -> None:
    assert UrlTemplate(template).expand(params, label="r") == expected


@pytest.mark.parametrize(
    ("value", "encoded"),
    [
        ("a/b", "a%2Fb"),
        ("100%", "100%25"),
        ("a b", "a%20b"),
        ("café", "caf%C3%A9"),
        ("a?b#c", "a%3Fb%23c"),
        (".", "%2E"),
        ("..", "%2E%2E"),
        ("a.b", "a.b"),
    ],
)
def test_segment_values_are_percent_encoded_per_segment(value: str, encoded: str) -> None:
    assert _expand("/files/{name}", name=value) == f"/files/{encoded}"


def test_wildcard_value_keeps_its_slashes() -> None:
    assert _expand("/static/{*path}", path="css/a b.css") == "/static/css/a%20b.css"


def test_wildcard_dot_segments_are_escaped() -> None:
    assert _expand("/static/{*path}", path="../etc") == "/static/%2E%2E/etc"


def test_empty_trailing_wildcard_drops_the_trailing_slash() -> None:
    assert _expand("/static/{*path}", path="") == "/static"


def test_empty_root_wildcard_is_the_root() -> None:
    assert _expand("/{*rest}", rest="") == "/"


def test_converter_suffix_and_path_converter_are_understood() -> None:
    template = UrlTemplate("/items/{id:int}/{rest:path}")

    assert template.param_names == frozenset({"id", "rest"})
    assert template.expand({"id": 3, "rest": "a/b"}, label="r") == "/items/3/a/b"


def test_literal_segments_are_encoded_once_at_compile_time() -> None:
    assert _expand("/café/{id}", id=1) == "/caf%C3%A9/1"


class Color(enum.Enum):
    RED = "red"


@pytest.mark.parametrize(
    ("value", "text"), [(True, "true"), (False, "false"), (Color.RED, "red"), (2.5, "2.5")]
)
def test_values_render_through_the_default_to_url(value: object, text: str) -> None:
    assert _expand("/v/{x}", x=value) == f"/v/{text}"


@pytest.mark.parametrize(
    ("params", "message"),
    [
        ({}, "missing 'id'"),
        ({"id": 1, "extra": 2}, "unexpected 'extra'"),
        ({"id": None}, "is None"),
        ({"id": ""}, "must not be empty"),
    ],
)
def test_wrong_parameters_raise_value_error_naming_the_route(params: dict, message: str) -> None:
    with pytest.raises(ValueError, match=message) as info:
        UrlTemplate("/users/{id}").expand(params, label="users.show")

    assert "users.show" in str(info.value)


# ---------------------------------------------------------------------------
# URL value type
# ---------------------------------------------------------------------------


def test_url_is_a_str_with_parsed_components() -> None:
    url = URL("https://api.example.com:8443/users/7?x=1#top")

    assert isinstance(url, str)
    assert (url.scheme, url.host, url.port, url.path, url.query, url.fragment) == (
        "https",
        "api.example.com",
        8443,
        "/users/7",
        "x=1",
        "top",
    )
    assert url.netloc == "api.example.com:8443"
    assert url.is_absolute
    assert repr(url) == "URL('https://api.example.com:8443/users/7?x=1#top')"


def test_relative_url_is_not_absolute_and_has_no_host() -> None:
    url = URL("/users/7")

    assert not url.is_absolute
    assert (url.host, url.port) == ("", None)


def test_ipv6_host_keeps_its_brackets() -> None:
    assert URL("http://[::1]:8000/").host == "[::1]"


def test_with_query_appends_in_argument_order_and_encodes() -> None:
    url = URL("/search").with_query(q="a b&c", page=2)

    assert url == "/search?q=a%20b%26c&page=2"


def test_with_query_repeats_list_values_and_drops_none() -> None:
    assert URL("/s").with_query(tag=["a", "b"], skip=None, flag=True) == "/s?tag=a&tag=b&flag=true"


def test_with_query_replaces_existing_keys_and_keeps_the_others() -> None:
    assert URL("/s?a=1&b=2&a=3").with_query(a=9) == "/s?b=2&a=9"


def test_with_query_accepts_a_mapping_for_names_that_are_not_identifiers() -> None:
    assert URL("/s").with_query({"page[size]": 10}) == "/s?page%5Bsize%5D=10"


def test_with_query_none_removes_a_key() -> None:
    assert URL("/s?a=1&b=2").with_query(a=None) == "/s?b=2"


def test_with_fragment_sets_and_removes_the_fragment() -> None:
    url = URL("/doc?x=1").with_fragment("sec 2")

    assert url == "/doc?x=1#sec%202"
    assert url.with_fragment(None) == "/doc?x=1"


def test_replace_swaps_components() -> None:
    url = URL("http://a.example:8080/x?y=1")

    assert url.replace(scheme="https", port=None) == "https://a.example/x?y=1"
    assert url.replace(host="b.example") == "http://b.example:8080/x?y=1"
    assert url.replace(path="/z", query="", fragment="f") == "http://a.example:8080/z#f"


def test_replace_with_positional_arguments_is_str_replace() -> None:
    result = URL("/a/b").replace("/b", "/c")

    assert result == "/a/c"
    assert type(result) is str


@pytest.mark.parametrize("call", [lambda u: u.replace("a", "b", path="/"), lambda u: u.replace(user="x")])
def test_replace_rejects_mixed_or_unknown_arguments(call) -> None:
    with pytest.raises(TypeError):
        call(URL("/a"))


# ---------------------------------------------------------------------------
# Authority parsing / composition
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "parsed"),
    [
        ("example.com", ("example.com", None)),
        ("example.com:8080", ("example.com", 8080)),
        ("127.0.0.1:80", ("127.0.0.1", 80)),
        ("[::1]", ("[::1]", None)),
        ("[::1]:8000", ("[::1]", 8000)),
        ("example.com.", ("example.com.", None)),
    ],
)
def test_parse_authority_accepts_hosts(value: str, parsed: tuple) -> None:
    assert parse_authority(value) == parsed


@pytest.mark.parametrize(
    "value",
    [
        "",
        "evil.com/x",
        "a@b",
        "a b",
        "host:0",
        "host:65536",
        "host:x",
        "[::1",
        "[zz]",
        "-bad",
        "a:1:2",
        "[::1]x",
    ],
)
def test_parse_authority_rejects_invalid_values(value: str) -> None:
    assert parse_authority(value) is None


@pytest.mark.parametrize(
    ("scheme", "port", "origin"),
    [
        ("http", 80, "http://h"),
        ("https", 443, "https://h"),
        ("https", 8443, "https://h:8443"),
        ("ws", None, "ws://h"),
    ],
)
def test_compose_origin_omits_default_ports(scheme: str, port: int | None, origin: str) -> None:
    assert compose_origin(scheme, "h", port) == origin


@pytest.mark.parametrize(
    ("root", "joined"), [("", "/x"), ("/api", "/api/x"), ("/api/", "/api/x"), ("/a b", "/a%20b/x")]
)
def test_join_root_prefixes_the_encoded_root(root: str, joined: str) -> None:
    assert join_root(root, "/x") == joined


@pytest.mark.parametrize(
    ("value", "split"),
    [
        ("https://api.example.com", ("https://api.example.com", "")),
        ("https://api.example.com/backend/", ("https://api.example.com", "/backend")),
        ("http://localhost:8000", ("http://localhost:8000", "")),
        ("https://h:443/x", ("https://h", "/x")),
    ],
)
def test_split_base_url(value: str, split: tuple) -> None:
    assert split_base_url(value) == split


@pytest.mark.parametrize(
    "value",
    [
        "/relative",
        "ftp://h",
        "https://",
        "https://u:p@h",
        "https://h/?q=1",
        "https://h/#f",
        "https://bad host",
        3,
    ],
)
def test_split_base_url_rejects_invalid_values(value: object) -> None:
    with pytest.raises(ValueError):
        split_base_url(value)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Route table lookups
# ---------------------------------------------------------------------------


async def _show(self, id: int) -> None: ...


class _Owner:
    pass


def _table() -> RouteTable:
    return RouteTable(
        [
            RouteSpec("GET", "/users/{id}", _show, _Owner, "users.show", "show", None),
            RouteSpec("GET", "/users", _Owner, _Owner, None, None, "Owner.list"),
        ],
        [RouteSpec("WEBSOCKET", "/ws/{room}", _Owner, _Owner, None, None, "_Owner")],
    )


def test_unknown_name_suggests_close_matches() -> None:
    with pytest.raises(LookupError, match="did you mean 'users.show'"):
        _table().http_route("users.shw")


def test_unknown_name_without_close_matches_has_no_hint() -> None:
    with pytest.raises(LookupError) as info:
        _table().http_route("zzz")

    assert "did you mean" not in str(info.value)


def test_handler_reference_resolves_through_wrappers_and_bound_methods() -> None:
    @functools.wraps(_show)
    async def wrapper(*a: object, **k: object) -> None: ...

    class Holder:
        show = _show

    table = _table()

    assert table.http_route(wrapper).name == "users.show"
    assert table.http_route(Holder().show).name == "users.show"


def test_callable_that_is_not_a_handler_is_a_lookup_error() -> None:
    with pytest.raises(LookupError, match="not a route handler"):
        _table().http_route(lambda: None)


def test_non_callable_target_is_a_type_error() -> None:
    with pytest.raises(TypeError, match="route name"):
        _table().http_route(42)


def test_gateway_lookup_by_class_and_unknown_gateway() -> None:
    table = _table()

    assert table.ws_route(_Owner).path_template == "/ws/{room}"
    with pytest.raises(LookupError, match="not a WebSocket gateway"):
        table.ws_route(int)


# ---------------------------------------------------------------------------
# UrlBuilder
# ---------------------------------------------------------------------------


@controller("/users", name="users")
class _Users:
    @get("/{id}", name="show")
    async def show(self, id: int) -> dict:
        return {"id": id}


@module(controllers=[_Users])
class _UsersModule:
    pass


def test_unbound_builder_raises() -> None:
    with pytest.raises(RuntimeError, match="not bound"):
        UrlBuilder().path_for("users.show", id=1)


def test_builder_paths_include_the_public_root_path() -> None:
    app = LaurenFactory.create(_UsersModule, public_base_url="https://api.example.com/backend")

    assert app.urls.path_for("users.show", id=3) == "/backend/users/3"
    assert app.urls.url_for(_Users.show, id=3) == "https://api.example.com/backend/users/3"
    assert app.urls.public_base_url == "https://api.example.com/backend"


def test_builder_falls_back_to_root_path_when_the_base_url_has_no_path() -> None:
    app = LaurenFactory.create(_UsersModule, public_base_url="https://api.example.com", root_path="/api")

    assert app.urls.root_path == "/api"
    assert app.urls.url_for("users.show", id=1) == "https://api.example.com/api/users/1"


def test_builder_url_for_without_public_base_url_raises() -> None:
    app = LaurenFactory.create(_UsersModule)

    assert app.urls.public_base_url is None
    with pytest.raises(RuntimeError, match="public_base_url"):
        app.urls.url_for("users.show", id=1)


def test_signed_url_without_signing_config_raises() -> None:
    app = LaurenFactory.create(_UsersModule)

    with pytest.raises(RuntimeError, match="url_signing is not configured"):
        app.urls.signed_url_for("users.show", id=1)


def test_app_url_path_for_is_app_relative() -> None:
    app = LaurenFactory.create(_UsersModule, root_path="/api")

    assert app.url_path_for("users.show", id=9) == "/users/9"


def test_request_outside_an_app_cannot_build_urls() -> None:
    request = Request(method="GET", path="/")

    with pytest.raises(RuntimeError, match="Lauren app"):
        request.url_path_for("users.show", id=1)
    with pytest.raises(RuntimeError):
        request.url_for("users.show", id=1)
    with pytest.raises(RuntimeError):
        request.ws_url_for("chat")


@pytest.mark.parametrize(
    ("headers", "server", "host", "port"),
    [
        ([("host", "api.example.com")], ("10.0.0.1", 8000), "api.example.com", 80),
        ([("host", "api.example.com:8080")], ("10.0.0.1", 8000), "api.example.com", 8080),
        ([("host", "evil.com/x")], ("10.0.0.1", 8000), "10.0.0.1", 8000),
        ([], ("::1", 8000), "[::1]", 8000),
        ([], (None, None), "", 80),
    ],
)
def test_request_host_and_port(headers: list, server: tuple, host: str, port: int) -> None:
    from lauren.types import Headers, ServerInfo

    request = Request(method="GET", path="/", headers=Headers(headers), server=ServerInfo(*server))

    assert (request.host, request.port) == (host, port)


def test_request_full_url_and_base_url() -> None:
    from lauren.types import Headers

    request = Request(
        method="GET",
        path="/a b",
        raw_query_string=b"x=1",
        headers=Headers([("host", "h.example:8443")]),
        scheme="https",
        root_path="/api",
    )

    assert request.full_url == "https://h.example:8443/api/a%20b?x=1"
    assert request.base_url == "https://h.example:8443/api/"
    assert request.path_with_query == "/a b?x=1"


def test_unhashable_target_is_a_type_error() -> None:
    with pytest.raises(TypeError, match="route name"):
        _table().http_route([])


@ws_controller("/ws/{room}", name="room")
class Room:
    @on_connect
    async def connect(self, ws: WebSocket) -> None: ...


@module(controllers=[Room])
class RoomModule:
    pass


def test_builder_ws_url_for_maps_the_scheme() -> None:
    app = LaurenFactory.create(RoomModule, public_base_url="https://chat.example.com/rt")

    assert app.urls.ws_url_for("room", room="a") == "wss://chat.example.com/rt/ws/a"
    assert app.urls.ws_url_for(Room, room="b") == "wss://chat.example.com/rt/ws/b"


def test_split_base_url_rejects_an_invalid_host() -> None:
    with pytest.raises(ValueError, match="host"):
        split_base_url("https://-bad.example")


def test_verify_reads_a_bytes_query_string() -> None:
    from types import SimpleNamespace

    from lauren._urls._signing import UrlSigner
    from lauren.urls import UrlSigningConfig

    signer = UrlSigner(UrlSigningConfig(secret="k"), has_origin=False)
    builder = UrlBuilder(signer=signer)
    query = signer.sign(method="GET", origin="", path="/x", query=[], expires_in=60)
    request = SimpleNamespace(method="GET", path="/x", query_string=query.encode("latin-1"))

    assert builder._verify(request) is None


def test_a_registered_url_builder_provider_is_rejected_at_startup() -> None:
    from lauren import use_value
    from lauren.exceptions import StartupError

    with pytest.raises(StartupError) as info:
        LaurenFactory.create(
            _UsersModule,
            global_providers=[use_value(provide=UrlBuilder, value=UrlBuilder())],
        )

    assert info.value.detail["reason"] == "url_builder_override"


def test_signature_guard_check_ignores_other_guards() -> None:
    from lauren._urls._module import check_signature_guards, prepare_urls
    from lauren.logging import NullLogger

    setup = prepare_urls(
        proxy=None, public_base_url=None, url_signing=None, root_path="", logger=NullLogger()
    )

    check_signature_guards([(object, "route GET /")], setup)


def test_request_uses_http2_authority_when_host_header_is_absent() -> None:
    from lauren.types import Headers

    request = Request(method="GET", path="/", headers=Headers([(":authority", "h.example:8443")]))

    assert (request.host, request.port) == ("h.example", 8443)


@pytest.mark.parametrize("bad", ["https://api.example.com//evil.example", "https://api.example.com/a/../b"])
def test_public_base_url_rejects_unsafe_root_path(bad: str) -> None:
    with pytest.raises(ValueError, match="public base URL path"):
        split_base_url(bad)
